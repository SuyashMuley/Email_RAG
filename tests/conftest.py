import pytest


@pytest.fixture(scope="module")
def db():
    """Connection to the local pgvector database; integration tests skip if it is not running."""
    try:
        from rag.db.connection import connect

        conn = connect()
    except Exception as exc:
        pytest.skip(f"database unavailable: {exc}")
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture(scope="module")
def embedder():
    from rag.embed.embedder import get_embedder

    return get_embedder()
