"""Phase 0 smoke test: Postgres + pgvector, Ollama LLM, embedding model.

Usage: python scripts/smoke_test.py [--skip-embed]
"""

import argparse
import sys
import time

from rag.config import get_settings


def check_db() -> None:
    from rag.db.connection import connect

    with connect() as conn:
        version = conn.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()
        assert version, "pgvector extension is not installed"
        tables = {r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
        missing = {"users", "documents", "chunks"} - tables
        assert not missing, f"schema not applied, missing tables: {missing}"
        row = conn.execute(
            "SELECT '[1,0,0]'::vector <=> '[0,1,0]'::vector"
        ).fetchone()
        assert abs(row[0] - 1.0) < 1e-6, "cosine distance check failed"
    print(f"db: ok (pgvector {version[0]}, tables {sorted(tables)})")


def check_llm() -> None:
    import ollama

    settings = get_settings()
    client = ollama.Client(host=settings.ollama_host)
    start = time.perf_counter()
    reply = client.chat(
        model=settings.llm_model,
        messages=[{"role": "user", "content": "Reply with the single word: ready"}],
        options={"num_predict": 16, "temperature": 0},
    )
    elapsed = time.perf_counter() - start
    print(f"llm: ok ({settings.llm_model}, {elapsed:.1f}s) -> {reply['message']['content'].strip()!r}")


def check_embed() -> None:
    from sentence_transformers import SentenceTransformer

    settings = get_settings()
    model = SentenceTransformer(settings.embed_model)
    start = time.perf_counter()
    vec = model.encode(["What did Sarah say about the Q4 budget?"], normalize_embeddings=True)
    elapsed = time.perf_counter() - start
    assert vec.shape[1] == settings.embed_dim, f"dim {vec.shape[1]} != EMBED_DIM {settings.embed_dim}"
    print(f"embed: ok ({settings.embed_model}, dim {vec.shape[1]}, {elapsed * 1000:.0f} ms)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-embed", action="store_true")
    args = parser.parse_args()

    checks = [("db", check_db), ("llm", check_llm)]
    if not args.skip_embed:
        checks.append(("embed", check_embed))

    failed = 0
    for name, fn in checks:
        try:
            fn()
        except Exception as exc:  # report every failure, not just the first
            failed += 1
            print(f"{name}: FAILED: {type(exc).__name__}: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
