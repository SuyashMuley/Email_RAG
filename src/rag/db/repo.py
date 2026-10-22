"""Database access for users, documents and chunks."""

import psycopg


def get_user_id(conn: psycopg.Connection, email: str) -> int | None:
    row = conn.execute("SELECT id FROM users WHERE email = %s", (email.lower(),)).fetchone()
    return row[0] if row else None


def get_or_create_user(conn: psycopg.Connection, email: str) -> int:
    row = conn.execute(
        "INSERT INTO users (email) VALUES (%s) "
        "ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email RETURNING id",
        (email.lower(),),
    ).fetchone()
    return row[0]


def existing_hash(conn: psycopg.Connection, user_id: int, source: str, source_id: str) -> str | None:
    row = conn.execute(
        "SELECT content_hash FROM documents WHERE user_id = %s AND source = %s AND source_id = %s",
        (user_id, source, source_id),
    ).fetchone()
    return row[0] if row else None


def delete_document(conn: psycopg.Connection, user_id: int, source: str, source_id: str) -> None:
    """Delete a document; its chunks and attachment documents cascade."""
    conn.execute(
        "DELETE FROM documents WHERE user_id = %s AND source = %s AND source_id = %s",
        (user_id, source, source_id),
    )


def insert_document(conn: psycopg.Connection, user_id: int, **f) -> int:
    row = conn.execute(
        """
        INSERT INTO documents (user_id, source, source_id, thread_id, parent_doc_id, sender,
                               recipients, subject, sent_at, filename, mime_type, web_link, content_hash)
        VALUES (%(user_id)s, %(source)s, %(source_id)s, %(thread_id)s, %(parent_doc_id)s, %(sender)s,
                %(recipients)s, %(subject)s, %(sent_at)s, %(filename)s, %(mime_type)s, %(web_link)s,
                %(content_hash)s)
        RETURNING id
        """,
        {"user_id": user_id, "parent_doc_id": None, "filename": None, "mime_type": None, **f},
    ).fetchone()
    return row[0]


def insert_chunks(conn: psycopg.Connection, user_id: int, document_id: int, texts, embeddings) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO chunks (document_id, user_id, chunk_index, text, embedding) "
            "VALUES (%s, %s, %s, %s, %s)",
            [(document_id, user_id, i, t, e) for i, (t, e) in enumerate(zip(texts, embeddings))],
        )


def get_sync_state(conn: psycopg.Connection, user_id: int, source: str) -> str | None:
    row = conn.execute(
        "SELECT state FROM sync_state WHERE user_id = %s AND source = %s", (user_id, source)
    ).fetchone()
    return row[0] if row else None


def set_sync_state(conn: psycopg.Connection, user_id: int, source: str, state: str) -> None:
    conn.execute(
        "INSERT INTO sync_state (user_id, source, state) VALUES (%s, %s, %s) "
        "ON CONFLICT (user_id, source) DO UPDATE SET state = EXCLUDED.state, updated_at = now()",
        (user_id, source, state),
    )
