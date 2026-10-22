"""Multi-user isolation: row-level security in the database, and token auth at the API.

Needs the pgvector container running. Uses two throwaway users loaded from the synthetic
mailboxes; both are ingested through the restricted `rag_app` role, so ingestion itself runs
under RLS.
"""

import base64
import json
import subprocess
import sys
import time
from pathlib import Path

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient

from rag.api.main import create_app
from rag.auth import tokens
from rag.auth.tokens import issue_local_token
from rag.config import Settings
from rag.db import repo
from rag.db.connection import connect_app, lookup_user_id
from rag.generate.llm import Answer
from rag.ingest.mbox_loader import iter_emails
from rag.ingest.pipeline import ingest_emails

ROOT = Path(__file__).resolve().parent.parent
A, B = "iso-a@example.com", "iso-b@example.com"  # A gets Alex's mailbox, B gets Jordan's
SECRET = "isolation-test-secret-" + "z" * 32


@pytest.fixture(scope="module")
def iso(db, embedder, tmp_path_factory):
    pytest.importorskip("reportlab")
    out = tmp_path_factory.mktemp("iso")
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_synthetic.py"), "--out", str(out)],
        check=True, capture_output=True,
    )
    db.execute("DELETE FROM users WHERE email IN (%s, %s)", (A, B))
    ids = {}
    for email, mbox in ((A, "alex_rivera.mbox"), (B, "jordan_lee.mbox")):
        ids[email] = repo.get_or_create_user(db, email)
        db.commit()
        with connect_app(ids[email]) as conn:
            ingest_emails(conn, ids[email], iter_emails(out / mbox), embedder)
    yield ids
    db.rollback()
    db.execute("DELETE FROM users WHERE email IN (%s, %s)", (A, B))
    db.commit()


def admin_count(db, table, uid):
    return db.execute(f"SELECT count(*) FROM {table} WHERE user_id = %s", (uid,)).fetchone()[0]


# --- database level --------------------------------------------------------------------------


def test_app_role_cannot_bypass_rls(db):
    assert db.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'rag_app'").fetchone() == (False, False)


def test_connection_without_a_user_sees_nothing(iso):
    with connect_app() as conn:
        for table in ("documents", "chunks", "sync_state"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_scoped_connection_sees_only_its_own_rows(db, iso):
    for email in (A, B):
        with connect_app(iso[email]) as conn:
            assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == admin_count(db, "documents", iso[email]) > 0
            assert conn.execute("SELECT DISTINCT user_id FROM chunks").fetchall() == [(iso[email],)]


def test_explicitly_asking_for_another_users_rows_returns_nothing(iso):
    with connect_app(iso[A]) as conn:
        assert conn.execute("SELECT count(*) FROM documents WHERE user_id = %s", (iso[B],)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM chunks WHERE user_id = %s", (iso[B],)).fetchone()[0] == 0


def test_cannot_insert_rows_for_another_user(iso):
    with connect_app(iso[A]) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO documents (user_id, source, source_id) VALUES (%s, 'gmail', 'planted')", (iso[B],)
            )


def test_cannot_update_or_delete_another_users_rows(db, iso):
    before = admin_count(db, "documents", iso[B])
    with connect_app(iso[A]) as conn:
        assert conn.execute("UPDATE documents SET subject = 'tampered' WHERE user_id = %s", (iso[B],)).rowcount == 0
        assert conn.execute("DELETE FROM documents WHERE user_id = %s", (iso[B],)).rowcount == 0
        assert conn.execute("DELETE FROM chunks WHERE user_id = %s", (iso[B],)).rowcount == 0
    assert admin_count(db, "documents", iso[B]) == before
    assert db.execute("SELECT count(*) FROM documents WHERE subject = 'tampered'").fetchone()[0] == 0


def test_cannot_hand_own_rows_to_another_user(iso):
    with connect_app(iso[A]) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE documents SET user_id = %s WHERE user_id = %s", (iso[B], iso[A]))


def test_app_role_cannot_read_the_user_list(iso):
    with connect_app(iso[A]) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT email FROM users")


def test_vector_search_never_returns_another_users_chunks(db, embedder, iso):
    query = embedder.embed_query("lease renewal at 220 Harbor St rent increase")  # B's private topic
    best_of_b = db.execute(
        "SELECT text FROM chunks WHERE user_id = %s ORDER BY embedding <=> %s LIMIT 1", (iso[B], query)
    ).fetchone()[0]
    assert "Harbor" in best_of_b  # sanity: B really holds the matching content
    with connect_app(iso[A]) as conn:
        owners = {r[0] for r in conn.execute("SELECT user_id FROM chunks ORDER BY embedding <=> %s LIMIT 100", (query,))}
    assert owners == {iso[A]}


def test_user_id_cannot_be_smuggled_through_the_session_setting(iso):
    with pytest.raises(ValueError):
        connect_app("1 OR 1=1")


def test_lookup_user_id(iso):
    assert lookup_user_id(A) == iso[A]
    assert lookup_user_id(A.upper()) == iso[A]
    assert lookup_user_id("nobody@example.com") is None


# --- API level --------------------------------------------------------------------------------


def fake_generate(question, retrieval):
    return Answer("stub answer", retrieval.sources, retrieval.sources, 0.0, 0.0, 0, 0)


@pytest.fixture(scope="module")
def client(iso, embedder):
    settings = Settings(_env_file=None, local_jwt_secret=SECRET)
    return TestClient(create_app(settings, generate_fn=fake_generate, embedder_fn=lambda: embedder))


def auth(email, **kw):
    return {"Authorization": f"Bearer {issue_local_token(email, SECRET, **kw)}"}


def ask(client, email, question):
    r = client.post("/query", json={"question": question}, headers=auth(email))
    assert r.status_code == 200, r.text
    return r.json()


def corpus(result):
    """Everything an answer exposes: subjects, senders and links."""
    return json.dumps(result["sources"]).lower()


def test_health_check_is_public(client):
    assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize("path, method", [("/me", "get"), ("/query", "post")])
def test_endpoints_require_a_token(client, path, method):
    kwargs = {"json": {"question": "hi"}} if method == "post" else {}
    for headers in ({}, {"Authorization": "Bearer garbage"}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer "}):
        r = getattr(client, method)(path, headers=headers, **kwargs)
        assert r.status_code == 401, headers
        assert r.headers["www-authenticate"] == "Bearer"


def test_expired_and_foreign_signed_tokens_are_rejected(client):
    expired = {"Authorization": "Bearer " + issue_local_token(A, SECRET, now=time.time() - 7200)}
    foreign = {"Authorization": "Bearer " + issue_local_token(A, "attacker-secret-" + "q" * 32)}
    for headers in (expired, foreign):
        assert client.get("/me", headers=headers).status_code == 401


def test_alg_none_token_is_rejected(client):
    now = int(time.time())
    forged = jwt.encode(
        {"iss": tokens.ISSUER, "aud": tokens.AUDIENCE, "sub": B, "iat": now, "exp": now + 600}, key=None, algorithm="none"
    )
    assert client.get("/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_editing_the_subject_of_a_valid_token_is_rejected(client):
    head, payload, sig = issue_local_token(A, SECRET).split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["sub"] = B
    forged_payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    r = client.get("/me", headers={"Authorization": f"Bearer {head}.{forged_payload}.{sig}"})
    assert r.status_code == 401


def test_valid_token_for_an_unregistered_account_is_forbidden(client):
    assert client.get("/me", headers=auth("stranger@example.com")).status_code == 403


def test_me_reports_each_users_own_document_count(db, client, iso):
    for email in (A, B):
        body = client.get("/me", headers=auth(email)).json()
        assert body == {"email": email, "documents": admin_count(db, "documents", iso[email])}


@pytest.mark.parametrize("field", ["user_id", "user", "email", "mailbox"])
def test_identity_fields_in_the_request_are_refused(client, iso, field):
    r = client.post("/query", json={"question": "hi", field: B}, headers=auth(A))
    assert r.status_code == 422


@pytest.mark.parametrize("body", [{}, {"question": ""}, {"question": "x", "k": 0}, {"question": "x", "k": 500}])
def test_malformed_queries_are_rejected(client, body):
    assert client.post("/query", json=body, headers=auth(A)).status_code == 422


@pytest.mark.parametrize(
    "asker, question, other_users_secret",
    [
        (A, "Tell me about the lease renewal at Harbor St", "harbor"),
        (A, "What is the Beacon mobile app launch date?", "beacon"),
        (A, "Where does Raj Patel say the crash rate is?", "raj"),
        (B, "Where is the Project Falcon offsite in Lisbon?", "lisbon"),
        (B, "What is the Atlas product launch date?", "atlas"),
        (B, "What did Priya Nair say about the Payments API?", "priya"),
    ],
)
def test_queries_about_another_users_data_leak_nothing(client, asker, question, other_users_secret):
    assert other_users_secret not in corpus(ask(client, asker, question))


def test_same_question_returns_each_users_own_answer_sources(client):
    a = corpus(ask(client, A, "What did Sarah say about the Q4 budget?"))
    b = corpus(ask(client, B, "What did Sarah say about the Q4 budget?"))
    assert "sarah.chen@acme-corp.example" in a and "northwind" not in a
    assert "sarah.miller@northwind.example" in b and "acme-corp" not in b


def test_links_in_results_only_point_at_own_mail(client, iso, db):
    result = ask(client, A, "Summarize all conversations about the product launch")
    own = {r[0] for r in db.execute("SELECT web_link FROM documents WHERE user_id = %s", (iso[A],))}
    assert result["sources"] and {s["link"] for s in result["sources"]} <= own
