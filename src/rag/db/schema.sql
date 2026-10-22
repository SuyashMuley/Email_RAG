-- Email RAG schema. The embedding dimension must match EMBED_DIM
-- (384 for BAAI/bge-small-en-v1.5).
-- Row-level security for multi-user isolation is added in Phase 5.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS users (
    id          SERIAL PRIMARY KEY,
    email       TEXT UNIQUE NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per email, attachment or Drive file.
CREATE TABLE IF NOT EXISTS documents (
    id             BIGSERIAL PRIMARY KEY,
    user_id        INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source         TEXT NOT NULL CHECK (source IN ('gmail', 'attachment', 'drive')),
    source_id      TEXT NOT NULL,          -- Gmail message id / Message-ID / Drive file id
    thread_id      TEXT,
    parent_doc_id  BIGINT REFERENCES documents(id) ON DELETE CASCADE,  -- attachment -> email
    sender         TEXT,
    recipients     TEXT[] NOT NULL DEFAULT '{}',
    subject        TEXT,
    sent_at        TIMESTAMPTZ,
    filename       TEXT,
    mime_type      TEXT,
    web_link       TEXT,                   -- referenceable link back to the original
    content_hash   TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, source, source_id)
);

CREATE INDEX IF NOT EXISTS documents_user_sent_idx ON documents (user_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS documents_thread_idx ON documents (user_id, thread_id);

CREATE TABLE IF NOT EXISTS chunks (
    id           BIGSERIAL PRIMARY KEY,
    document_id  BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    user_id      INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    chunk_index  INT NOT NULL,
    text         TEXT NOT NULL,
    embedding    vector(384) NOT NULL,
    tsv          tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    UNIQUE (document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS chunks_embedding_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_user_idx ON chunks (user_id);

-- Incremental sync cursor per user and source (e.g. Gmail historyId).
CREATE TABLE IF NOT EXISTS sync_state (
    user_id     INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    state       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, source)
);

-- ---------------------------------------------------------------------------
-- Multi-user isolation (row-level security)
--
-- The API and ingestion run as `rag_app`, a role that is neither superuser nor
-- BYPASSRLS. Each connection declares who it acts for with
--   SELECT set_config('app.user_id', '<id>', false)
-- and every policy below only exposes rows whose user_id matches. With no
-- setting, no rows are visible and no rows can be written (fails closed).
-- The admin role (`rag`, the table owner/superuser) is used only for schema
-- changes and for creating users.
--
-- The password below is for local development. Change it for anything else.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rag_app') THEN
        CREATE ROLE rag_app LOGIN PASSWORD 'rag_app'
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION app_user_id() RETURNS int
    LANGUAGE sql STABLE
    AS $$ SELECT nullif(current_setting('app.user_id', true), '')::int $$;

GRANT USAGE ON SCHEMA public TO rag_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON documents, chunks, sync_state TO rag_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO rag_app;
REVOKE ALL ON users FROM rag_app;  -- the app never reads the user list

ALTER TABLE documents  ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents  FORCE ROW LEVEL SECURITY;
ALTER TABLE chunks     ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks     FORCE ROW LEVEL SECURITY;
ALTER TABLE sync_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE sync_state FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS user_isolation ON documents;
DROP POLICY IF EXISTS user_isolation ON chunks;
DROP POLICY IF EXISTS user_isolation ON sync_state;
CREATE POLICY user_isolation ON documents
    USING (user_id = (SELECT app_user_id())) WITH CHECK (user_id = (SELECT app_user_id()));
CREATE POLICY user_isolation ON chunks
    USING (user_id = (SELECT app_user_id())) WITH CHECK (user_id = (SELECT app_user_id()));
CREATE POLICY user_isolation ON sync_state
    USING (user_id = (SELECT app_user_id())) WITH CHECK (user_id = (SELECT app_user_id()));

-- Lets the API map a verified email to a user id without being able to read `users`.
CREATE OR REPLACE FUNCTION lookup_user_id(p_email text) RETURNS int
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public
    AS $$ SELECT id FROM users WHERE email = lower(p_email) $$;
REVOKE ALL ON FUNCTION lookup_user_id(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lookup_user_id(text) TO rag_app;
