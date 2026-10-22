"""Postgres connections.

`connect()` uses the admin role and bypasses row-level security. Use it only for schema
changes and creating users. Everything that touches user data uses `connect_app(user_id)`,
which connects as the restricted `rag_app` role scoped to one user.

Each scoped connection is dedicated to a single user: it sets a session variable that the
RLS policies read, so never share one between users or put it in a pool that does.
"""

import psycopg
from pgvector.psycopg import register_vector

from rag.config import get_settings


def connect() -> psycopg.Connection:
    """Admin connection (bypasses RLS)."""
    conn = psycopg.connect(get_settings().database_url)
    register_vector(conn)
    return conn


def connect_app(user_id: int | None = None, url: str | None = None) -> psycopg.Connection:
    """Restricted connection. With a user_id, RLS exposes only that user's rows; without one, none."""
    conn = psycopg.connect(url or get_settings().app_database_url)
    register_vector(conn)
    if user_id is not None:
        conn.execute("SELECT set_config('app.user_id', %s, false)", (str(int(user_id)),))
        conn.commit()  # a rollback must not undo the setting
    return conn


def lookup_user_id(email: str) -> int | None:
    """Map an email to a user id through the SECURITY DEFINER function (no access to `users`)."""
    with connect_app() as conn:
        return conn.execute("SELECT lookup_user_id(%s)", (email,)).fetchone()[0]
