# Personal Email RAG

A local retrieval-augmented generation (RAG) system for your own email. It ingests Gmail (including
attachments), stores embeddings in PostgreSQL with pgvector, and answers natural-language questions with an
open-source LLM running on your machine. Every answer cites the emails it used and links back to them.
Nothing is sent to a hosted AI service.

- **Local and open source:** Ollama (Llama 3.1 8B), sentence-transformers (bge-small), PostgreSQL + pgvector, FastAPI.
- **Email ingestion:** Gmail API with incremental sync, or `.mbox` / `.eml` files. PDF, DOCX, XLSX, text and HTML attachments are indexed.
- **Hybrid retrieval:** vector similarity plus full-text search, fused by rank, with sender-aware filtering and "latest from X" handling.
- **Multi-user isolation:** per-user data enforced by PostgreSQL row-level security, with bearer-token authentication in the API.

Example questions it handles:

```
What did Sarah say about the Q4 budget?
Show me emails discussing the API integration project
When did I last hear from the marketing team?
Summarize all conversations about the product launch
```

## Quick start (about 10 minutes, plus model download)

Prerequisites: Python 3.11+, Docker (running), and [Ollama](https://ollama.com) (running). About 8 GB of free disk.

```bash
scripts/demo.sh
```

This sets up everything, ingests two synthetic mailboxes, runs the example queries, and demonstrates user isolation
through the CLI, the HTTP API and the database. Use `SKIP_SETUP=1 scripts/demo.sh` to skip the setup step on later runs.

### Manual setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt -e .
cp .env.example .env            # then set LOCAL_JWT_SECRET (see below)

docker compose up -d --wait     # PostgreSQL 17 + pgvector, schema applied on first start
.venv/bin/rag init-db           # re-applies the schema; safe to repeat
ollama pull llama3.1:8b

.venv/bin/python scripts/smoke_test.py   # checks database, LLM and embedding model
```

Generate the signing secret for local API tokens:
`python -c "import secrets; print(secrets.token_urlsafe(48))"` and paste it as `LOCAL_JWT_SECRET` in `.env`.

## Using it

### Command line

```bash
rag ingest data/synthetic/alex_rivera.mbox -u alex.rivera@example.com   # parse, chunk, embed, store
rag ask "What did Sarah say about the Q4 budget?" -u alex.rivera@example.com
rag ask "..." -u alex.rivera@example.com --show-context     # also print what was retrieved
rag ask "..." -u alex.rivera@example.com -m phi3:mini       # use another Ollama model
rag users                                                   # users and document counts
```

Every answer ends with its sources (date, sender, subject, link) and timings for embedding, retrieval, first
token and generation. Questions the mailbox cannot answer get an honest "could not find" instead of a guess.

### Your own Gmail

Follow [docs/GMAIL_SETUP.md](docs/GMAIL_SETUP.md) once (Google Cloud OAuth client, read-only scope), then:

```bash
rag gmail-auth                                  # browser sign-in; saves a token under secrets/
rag gmail-sync -u you@gmail.com                 # first run indexes everything
rag gmail-sync -u you@gmail.com                 # later runs fetch only new and deleted mail
rag gmail-sync -u you@gmail.com --query "newer_than:90d" --max 200   # quick partial test
rag ask "..." -u you@gmail.com
```

For a live demo, send yourself some emails, run `rag gmail-sync`, and ask about them: ingestion is
incremental and re-runnable.

### HTTP API and multiple users

```bash
rag serve                                       # http://127.0.0.1:8000
TOKEN=$(rag token -u alex.rivera@example.com)   # local bearer token (1 hour)

curl -s localhost:8000/me -H "Authorization: Bearer $TOKEN"
curl -s -X POST localhost:8000/query -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"question": "When did I last hear from the marketing team?"}'
```

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Liveness check (no auth) |
| `GET /me` | Caller's email and document count |
| `POST /query` | `{"question": "...", "k": 5}` returns the answer, cited sources with links, and timings |

The caller's identity comes only from the verified token. There is no `user_id` in any request (sending one is a 422),
a bad token is a 401, and a valid token for an account with no mailbox is a 403. `AUTH_MODE=google` accepts Google
ID tokens instead of the local ones (see the notes in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)).

## Configuration

All settings come from environment variables or `.env`:

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `postgresql://rag:rag@localhost:5432/rag` | Admin connection (schema, creating users) |
| `APP_DATABASE_URL` | `postgresql://rag_app:rag_app@localhost:5432/rag` | Restricted role used for all user data |
| `LLM_MODEL` | `llama3.1:8b` | Ollama model for answers |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server |
| `EMBED_MODEL` / `EMBED_DIM` | `BAAI/bge-small-en-v1.5` / `384` | Embedding model. The dimension must match the schema. |
| `AUTH_MODE` | `local` | `local` (tokens from `rag token`) or `google` |
| `LOCAL_JWT_SECRET` | none, required | At least 32 characters; the API refuses to start without it |
| `GMAIL_CREDENTIALS_PATH`, `TOKEN_DIR` | `secrets/credentials.json`, `secrets` | Google OAuth files (git-ignored) |

Changing the embedding model to one with a different dimension needs a schema change (`vector(384)`) and a re-ingest.

## Tests and benchmarks

```bash
.venv/bin/python -m pytest                      # about 100 tests; integration tests need the database running
.venv/bin/python scripts/benchmark.py all       # embeddings, retrieval, LLM comparison; writes docs/benchmarks/
```

The benchmark scores 25 gold questions on retrieval (right email in the top 5) and on the generated answers
(expected facts present, nothing forbidden, unanswerable questions declined). Results and methodology are in
[docs/benchmarks/RESULTS.md](docs/benchmarks/RESULTS.md) and discussed in [docs/EVALUATION.md](docs/EVALUATION.md).

## Project layout

```
src/rag/
  ingest/    parser, mbox loader, attachment text extraction, chunker, pipeline, Gmail client
  embed/     sentence-transformers wrapper
  retrieve/  hybrid search, sender filter, "latest" handling
  generate/  prompt construction and streaming Ollama client
  auth/      local JWT and Google ID-token verification
  api/       FastAPI app
  db/        schema.sql (tables, row-level security, roles), connections, queries
  cli.py     the `rag` command
scripts/     demo.sh, benchmark.py, generate_synthetic.py, smoke_test.py
data/        synthetic mailboxes (data/synthetic) and gold questions (data/eval)
docs/        ARCHITECTURE.md, EVALUATION.md, GMAIL_SETUP.md, benchmarks/
tests/
```

## Security notes

- Real mail never leaves the machine except to read it from Google. The Gmail scope is read-only. Tokens, `.env` and
  `secrets/` are git-ignored.
- Isolation is enforced in the database (row-level security on a role that cannot bypass it) as well as in the
  queries, so a bug in query code cannot cross users. Its limits are described in the architecture document.
- The `rag_app` database password and the demo setup are for local development. Change them for anything else.

## Limitations

- Very large "summarize everything about X" questions are trimmed to a context budget; there is no map-reduce step.
- The 8B model sometimes misattributes or omits a detail in long summaries. Always check the cited sources.
- No OCR: scanned PDFs and images are not indexed. No Google Drive ingestion yet.
- The Gmail client and Google ID-token auth are tested against fakes and one small real mailbox.

More detail, including measured numbers, in [docs/EVALUATION.md](docs/EVALUATION.md).
