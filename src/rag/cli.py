"""Command-line interface: `rag ingest` and `rag ask`."""

import logging
from pathlib import Path

import typer

from rag.db import repo
from rag.db.connection import connect, connect_app, lookup_user_id

app = typer.Typer(help="Local email RAG.", no_args_is_help=True)


@app.command()
def ingest(
    path: Path = typer.Argument(..., exists=True, help=".mbox file or directory of .eml files"),
    user: str = typer.Option(..., "--user", "-u", help="Mailbox owner's email address"),
) -> None:
    """Parse, chunk, embed and store a mailbox for a user."""
    from rag.embed.embedder import get_embedder
    from rag.ingest.mbox_loader import iter_emails
    from rag.ingest.pipeline import ingest_emails

    logging.basicConfig(level=logging.WARNING)
    embedder = get_embedder()
    with connect() as admin:  # creating a user needs the admin role; user data does not
        user_id = repo.get_or_create_user(admin, user)
    with connect_app(user_id) as conn:
        stats = ingest_emails(
            conn, user_id, iter_emails(path), embedder,
            on_progress=lambda s: typer.echo(f"  {s.emails_seen} emails processed...", err=True),
        )
    typer.echo(
        f"{user}: {stats.emails_ingested} ingested, {stats.emails_unchanged} unchanged, "
        f"{stats.attachments_indexed} attachments indexed ({stats.attachments_skipped} skipped), "
        f"{stats.chunks} chunks"
    )
    per_chunk = stats.embed_seconds / stats.chunks * 1000 if stats.chunks else 0
    typer.echo(f"embedding {stats.embed_seconds:.2f}s ({per_chunk:.1f} ms/chunk), total {stats.total_seconds:.2f}s")


@app.command()
def ask(
    question: str = typer.Argument(...),
    user: str = typer.Option(..., "--user", "-u", help="Mailbox owner's email address"),
    model: str = typer.Option(None, "--model", "-m", help="Ollama model (default: LLM_MODEL)"),
    k: int = typer.Option(None, "--k", help="Number of source documents to retrieve"),
    show_context: bool = typer.Option(False, "--show-context", help="Print the retrieved sources"),
) -> None:
    """Ask a question about a user's emails."""
    from rag.embed.embedder import get_embedder
    from rag.generate.llm import generate
    from rag.retrieve.retriever import retrieve

    embedder = get_embedder()
    user_id = lookup_user_id(user)
    if user_id is None:
        typer.echo(f"Unknown user {user!r}. Run `rag ingest` first.", err=True)
        raise typer.Exit(1)
    with connect_app(user_id) as conn:
        retrieval = retrieve(conn, embedder, user_id, question, own_email=user, k_docs=k)

    if show_context:
        typer.echo(f"[intent={retrieval.intent} sender_filter={retrieval.sender_filter}]")
        for s in retrieval.sources:
            typer.echo(f"  [{s.n}] {s.score:.4f} {s.subject} ({s.filename or 'email'})")
        typer.echo("")

    answer = generate(question, retrieval, model=model, on_token=lambda t: typer.echo(t, nl=False))
    typer.echo("\n\nSources:")
    for s in answer.sources:
        when = s.sent_at.strftime("%Y-%m-%d") if s.sent_at else "?"
        what = f"{s.subject} (attachment: {s.filename})" if s.filename else s.subject
        typer.echo(f"  [{s.n}] {when} {s.sender}: {what}\n      {s.web_link}")
    tps = answer.tokens / max(answer.generation_s - answer.ttft_s, 1e-9)
    typer.echo(
        f"\nembed {retrieval.embed_ms:.0f} ms | retrieve {retrieval.search_ms:.0f} ms | "
        f"first token {answer.ttft_s:.2f}s | generate {answer.generation_s:.2f}s "
        f"({answer.tokens} tokens, {tps:.1f} tok/s)"
    )


@app.command()
def users() -> None:
    """List users and how many documents each has."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT u.email, count(d.id) FROM users u LEFT JOIN documents d ON d.user_id = u.id "
            "GROUP BY u.email ORDER BY u.email"
        ).fetchall()
    for email, n in rows:
        typer.echo(f"{email}: {n} documents")


@app.command("init-db")
def init_db() -> None:
    """Apply the schema to an existing database (safe to re-run)."""
    schema = (Path(__file__).parent / "db" / "schema.sql").read_text()
    with connect() as conn:
        conn.execute(schema)
    typer.echo("schema applied")


@app.command("gmail-auth")
def gmail_auth() -> None:
    """Sign in to Google in the browser and save a read-only Gmail token."""
    from rag.ingest.gmail_client import CredentialsMissing, authorize

    try:
        address = authorize()
    except CredentialsMissing as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    typer.echo(f"authorized {address}. Now run: rag gmail-sync --user {address}")


@app.command("gmail-sync")
def gmail_sync(
    user: str = typer.Option(..., "--user", "-u", help="Gmail address that was authorized"),
    query: str = typer.Option(None, "--query", "-q", help='Gmail search, e.g. "newer_than:90d"'),
    max_results: int = typer.Option(None, "--max", help="Stop after this many messages"),
    full: bool = typer.Option(False, "--full", help="Ignore the saved cursor and re-list everything"),
) -> None:
    """Index a Gmail account. Re-running only fetches new and deleted mail."""
    from rag.embed.embedder import get_embedder
    from rag.ingest.gmail_client import CredentialsMissing, load_service, sync_mailbox

    logging.basicConfig(level=logging.WARNING)
    try:
        service = load_service(user)
    except CredentialsMissing as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    embedder = get_embedder()
    with connect() as admin:
        user_id = repo.get_or_create_user(admin, user)
    with connect_app(user_id) as conn:
        result = sync_mailbox(
            conn, user_id, service, embedder, user.lower(), query=query, max_results=max_results, full=full,
            on_progress=lambda s: typer.echo(f"  {s.emails_seen} emails processed...", err=True),
        )
    s = result.stats
    typer.echo(
        f"{user} ({result.mode} sync): {s.emails_ingested} ingested, {s.emails_unchanged} unchanged, "
        f"{result.deleted} deleted, {s.attachments_indexed} attachments indexed "
        f"({s.attachments_skipped} skipped), {s.chunks} chunks, embedding {s.embed_seconds:.2f}s"
    )


@app.command()
def token(
    user: str = typer.Option(..., "--user", "-u", help="Registered mailbox owner's email"),
    ttl: int = typer.Option(None, "--ttl", help="Lifetime in minutes (local mode)"),
) -> None:
    """Print a bearer token for the API: a local JWT, or a fresh Google ID token in google mode."""
    from rag.auth.tokens import ConfigError, issue_local_token, make_verifier
    from rag.config import get_settings

    settings = get_settings()
    try:
        make_verifier(settings)  # validates the auth configuration
        if settings.auth_mode == "google":
            from rag.auth.tokens import fresh_google_id_token
            from rag.ingest.gmail_client import CredentialsMissing

            try:
                typer.echo(fresh_google_id_token(user))
            except CredentialsMissing as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(1)
            return
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    if lookup_user_id(user) is None:
        typer.echo(f"Unknown user {user!r}. Run `rag ingest` first.", err=True)
        raise typer.Exit(1)
    typer.echo(issue_local_token(user, settings.local_jwt_secret, ttl or settings.token_ttl_minutes))


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address (keep local unless you add TLS)"),
    port: int = typer.Option(8000),
) -> None:
    """Run the HTTP API."""
    import uvicorn

    uvicorn.run("rag.api.main:create_app", factory=True, host=host, port=port)
