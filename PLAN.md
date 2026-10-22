# Personal Email RAG System: Implementation Plan

Source: the technical assessment in `screenshots/1.png` to `7.png`. Everything must run locally on open-source tools. No calls to OpenAI, Claude or similar APIs at runtime.

## Status (updated 2026-10-23)

| Phase | Status | Notes |
|---|---|---|
| 0. Environment | **Done** | pgvector, Ollama, venv, smoke test. |
| 1. Parsing and synthetic data | **Done** | Two deterministic mailboxes of 52 emails each. |
| 2. Gmail integration | **Done, lightly tested live** | Real sign-in and sync worked on a 2-message mailbox. Attachments, pagination and large mailboxes are tested only against a fake API. |
| 3. Attachments, chunking, embedding | **Done** | |
| 4. Retrieval and RAG | **Done** | Map-reduce, date hints and rerank skipped (see Phase 4). |
| 5. Multi-user isolation | **Done** | Row-level security plus token auth. Google sign-in is mocked-tested only. |
| 6. Google Drive (bonus) | Not started | |
| 7. Benchmarks and evaluation | **Done** | `scripts/benchmark.py`, 25 gold questions, results in `docs/benchmarks/`. |
| 8. Documentation and deliverables | **Done** | README, architecture, evaluation, GMAIL_SETUP, `scripts/demo.sh` (verified end to end). |

104 automated tests pass (`.venv/bin/python -m pytest`). Integration tests need the pgvector container running.

**Current machine state:** the pgvector container is running (data volume `rag_pgdata`) and Ollama has `llama3.1:8b`, `llama3.1:8b-instruct-q8_0` and `phi3:mini`. `secrets/` holds the Google OAuth client and a token for `suyashmuley03@gmail.com`. The demo (`scripts/demo.sh`) works from a clean start.

**Next steps:**
1. Put the code in a private GitHub repo (the folder is not a git repo yet) and email Rebecca Hurd to schedule the evaluation.
2. Fill the Gmail mailbox with realistic mail (threads on the example topics, one with a PDF/DOCX attachment), run `rag gmail-sync`, and confirm a live query end to end. This is the main thing not yet verified on real data.
3. Optional: Phase 6 (Google Drive), `AUTH_MODE=google` against real Google tokens, a larger gold set.

## 1. Goal

A local RAG pipeline that:
- Ingests emails from a real Gmail account, including attachments.
- Stores embeddings plus metadata in a vector DB.
- Answers natural-language questions with a local LLM, and each answer links back to its source email.
- Bonus: multi-user isolation, OAuth2 token-based auth, and Google Drive ingestion.

Example queries to support (all four work on the synthetic data):
- "What did Sarah say about the Q4 budget?"
- "Show me emails discussing the API integration project"
- "When did I last hear from the marketing team?"
- "Summarize all conversations about the product launch"

## 2. Stack Decisions

| Layer | Choice | Why |
|---|---|---|
| LLM runtime | **Ollama** | Simplest local serving, built-in quantized GGUF models, REST API. |
| LLM | **Llama 3.1 8B Instruct** (Q4_K_M) as the primary, **Phi-3 Mini** (3.8B) as the low-RAM fallback and benchmark comparison | Both are named in the spec. Llama 3.1 has a 128K context, which suits long email threads. In a spot check Phi-3 Mini was faster (about 36 vs 20 tokens/s) but garbled the Q4 budget facts, so Llama stays the default. |
| Embeddings | **BAAI/bge-small-en-v1.5** (384 dimensions, sentence-transformers) | Small and fast on CPU, about 5-20 ms per chunk. The comparison with another model (e.g. nomic-embed-text) is planned for Phase 7. |
| Vector DB | **PostgreSQL + pgvector** (Docker, `pgvector/pgvector:pg17`) | Marked "preferred". Gives SQL metadata filters, an HNSW cosine index, full-text search and row-level security. |
| Gmail | Gmail API with OAuth2 (`google-api-python-client`) | Real-account access, incremental sync via `historyId`. |
| Drive | Drive API plus the existing extractors | Bonus, not started. |
| Attachments | `pypdf`, `python-docx`, `openpyxl`, plain text, HTML | Images and OCR are not supported. |
| API / UI | **FastAPI** plus a Typer CLI | The API gets the user identity only from a verified token. The CLI is used for the demo. |
| Frameworks | None | Plain code is easier to document and benchmark. |

Document each choice and its quantization in `docs/ARCHITECTURE.md` (Phase 8).

## 3. Repository Layout (as built)

```
RAG/
├── PLAN.md
├── pyproject.toml, requirements.txt, requirements-dev.txt
├── docker-compose.yml          # pgvector
├── .env.example                # copy to .env; LOCAL_JWT_SECRET is generated locally
├── src/rag/
│   ├── config.py               # settings from env / .env
│   ├── db/                     # schema.sql (tables, RLS, roles), connection.py, repo.py
│   ├── ingest/
│   │   ├── parser.py           # headers, body, HTML to text, quote/signature stripping
│   │   ├── mbox_loader.py      # .mbox / .eml
│   │   ├── attachments.py      # PDF, DOCX, XLSX, text, HTML
│   │   ├── chunker.py
│   │   ├── pipeline.py         # chunk, embed, store; idempotent
│   │   └── gmail_client.py     # OAuth, listing, raw fetch, incremental sync
│   ├── embed/embedder.py
│   ├── retrieve/retriever.py   # hybrid search, sender filter, "latest" intent
│   ├── generate/llm.py         # prompt builder, streaming Ollama client, timings
│   ├── auth/tokens.py          # local JWT and Google ID-token verification
│   ├── api/main.py             # FastAPI: /healthz, /me, /query
│   └── cli.py                  # ingest, ask, users, init-db, gmail-auth, gmail-sync, token, serve
├── data/synthetic/             # alex_rivera.mbox, jordan_lee.mbox
├── data/eval/gold.json         # 25 gold questions
├── scripts/                    # demo.sh, benchmark.py, generate_synthetic.py, smoke_test.py
├── tests/                      # parser, chunker, attachments, retrieval, gmail, auth, isolation
├── docs/GMAIL_SETUP.md
├── src/rag/evaluation.py       # retrieval metrics and answer checks used by the benchmark
└── README.md, docs/ARCHITECTURE.md, docs/EVALUATION.md, docs/benchmarks/, scripts/benchmark.py, scripts/demo.sh
```

## 4. Data Model (pgvector), as built

- `users(id, email, created_at)`
- `documents(id, user_id, source, source_id, thread_id, parent_doc_id, sender, recipients[], subject, sent_at, filename, mime_type, web_link, content_hash, created_at)`
  - `source` is one of `gmail`, `attachment`, `drive`. Unique on `(user_id, source, source_id)`.
  - `source_id` is the Gmail message id, or the Message-ID header for mbox files.
  - `web_link` is `https://mail.google.com/mail/u/<account>/#all/<messageId>` for Gmail, and a Message-ID search link for mbox data. Attachments link to their parent email.
- `chunks(id, document_id, user_id, chunk_index, text, embedding vector(384), tsv tsvector)`. The text includes a From/To/Date/Subject header.
- `sync_state(user_id, source, state, updated_at)` holds the Gmail history cursor.
- Indexes: HNSW (cosine) on `embedding`, GIN on `tsv`, btree on `(user_id, sent_at)`, `(user_id, thread_id)` and `chunks(user_id)`.
- Isolation: see Phase 5.

## 5. Phases

### Phase 0: Environment: DONE
- `docker-compose.yml` with `pgvector/pgvector:pg17`, schema applied on first start. Ollama with `llama3.1:8b` and `phi3:mini`.
- Python 3.14 venv (all dependencies installed cleanly), `requirements.txt`, `.env.example`.
- `scripts/smoke_test.py` checks the database, the LLM and the embedding model.

### Phase 1: Email parsing and synthetic data: DONE
- `parser.py` extracts sender, recipients, cc, subject, body, UTC timestamp, thread ID and attachments. It prefers `text/plain`, falls back to HTML to text, and strips quoted replies and signatures. It survives missing headers, malformed dates and unknown charsets.
- `generate_synthetic.py` is deterministic (stable Message-IDs and attachment bytes). It writes two mailboxes of 52 emails each: Alex Rivera (Acme) and Jordan Lee (Northwind). Both cover the four example topics with different facts, different people named Sarah, PDF and DOCX attachments, an HTML-only email, and a private thread each that must never leak (Lisbon offsite, lease renewal).
- Deviation: 52 emails per mailbox instead of 100-200. It is enough for the demo and tests, and `--noise N` adds more.

### Phase 2: Gmail integration: BUILT, NOT TESTED LIVE
- `rag gmail-auth` runs the browser OAuth flow (read-only Gmail scope, plus `openid` and `email` for token auth) and saves a token per account in `secrets/`.
- `rag gmail-sync -u <address>` lists messages with pagination, fetches each with `format=raw` (attachments are inside the raw MIME, so `attachments.get` is not needed), and ingests them. Options: `--query`, `--max`, `--full`.
- Incremental sync via the history id. Deleted mail is removed from the index. An expired history id falls back to a full sync. Partial runs (`--query`, `--max`) never advance the cursor.
- Tested against a fake Gmail API: pagination, parsing, incremental sync, deletions, fallback, messages vanishing mid-sync.
- **Still to do by you:** create the Google Cloud project and OAuth client (`docs/GMAIL_SETUP.md`), then run it once for real.

### Phase 3: Attachments, chunking and embedding: DONE
- Text extraction for PDF, DOCX (including tables), XLSX, TXT/CSV/MD and HTML. Unsupported or corrupt files are logged and skipped.
- Chunks of about 220 words with 40 words of overlap, each prefixed with the metadata header.
- Attachments are separate documents linked to the parent email, so an answer from an attachment links to the email.
- Ingestion is idempotent: unchanged emails are skipped, edited ones are replaced. Embedding is batched and timed.

### Phase 4: Retrieval and RAG: DONE, with gaps
- Hybrid search: pgvector cosine plus Postgres full-text (generic words like "emails" and "show" removed), merged with reciprocal rank fusion. Low-scoring results are dropped, and chunks are grouped by document.
- Query analysis: a sender named in the question ("Sarah", "marketing team") filters to that sender plus mail addressed to them. "When did I last hear from X" is answered by date order.
- Sources are shown to the model in chronological order, with rules to report the latest value when an email updates an earlier one.
- Output: a streamed answer with `[n]` citations, a source list with links, and timings (embed, retrieve, first token, generation, tokens per second).
- Interfaces: `rag ask` and `POST /query`.
- **Not done:** map-reduce summarization (large "summarize all" topics are trimmed to a context budget), date-hint parsing ("last week"), and cross-encoder reranking. Retrieval quality is good on the synthetic data without them.

### Phase 5: Multi-user isolation: DONE
- The API and ingestion connect as `rag_app`, a role that is not a superuser and cannot bypass row-level security. Policies on `documents`, `chunks` and `sync_state` expose only rows matching a per-connection `app.user_id`. With no setting, nothing is visible or writable. `rag_app` cannot read the `users` table and finds users through a `SECURITY DEFINER` function.
- Authentication: bearer tokens only. `AUTH_MODE=local` uses HS256 JWTs from `rag token` (a stand-in identity provider, with a required 32-character secret). `AUTH_MODE=google` verifies Google ID tokens. The identity comes only from the verified token. Request bodies reject extra fields, so a smuggled `user_id` gets a 422. Bad tokens get a 401, and a valid token for an unregistered account gets a 403.
- Tests (`tests/test_isolation.py`, `tests/test_auth.py`): no-user connections see nothing, cross-user reads, writes, updates, deletes and reassignment fail, vector search stays scoped, tampered, expired, `alg=none` and foreign-signed tokens are rejected, and queries about the other user's private topics leak nothing. Switching row-level security off made 5 tests fail, so they do catch regressions.
- Caveats: Google token verification is tested with a mocked verifier only. Row-level security protects against query bugs, not against code that can run arbitrary SQL as `rag_app` and change its own setting. The `rag_app` password and the JWT secret are local-dev values.

### Phase 6: Google Drive (bonus): NOT STARTED
- Drive API listing. Google Docs, Sheets and Slides are exported to text/CSV. PDF, DOCX and others go through the Phase 3 extractors.
- Indexed as `source='drive'` with `webViewLink`, under the same user isolation. The schema already allows it.
- Demo the conversion step, which the spec says is a plus.

### Phase 7: Benchmarks and evaluation: DONE
- `scripts/benchmark.py` (`embeddings`, `retrieval`, `generation`, `report`, `all`) writes `docs/benchmarks/results.json`, `RESULTS.md` and per-model `answers_*.md`. Scoring helpers live in `src/rag/evaluation.py` (unit-tested).
- Gold set `data/eval/gold.json`: 25 questions over both mailboxes (21 answerable, 4 unanswerable by design).
- Measured: embedding time, retrieval latency (p50/p95), generation time (first token, tokens/s), ingestion throughput, and a retrieval ablation (dense, keyword, hybrid, hybrid + query analysis) on a small and a 2,000-document corpus.
- Results: hybrid + query analysis finds the right email in the top 5 for 100% of questions (top 1 for 81%); retrieval about 20 ms; Llama 3.1 8B Q4 passes 25/25 automated checks at about 20 tokens/s; Q8 is no better and 1.8x slower; Phi-3 Mini passes 22/25 and invents answers to 2 of 4 unanswerable questions. Four embedding models are within noise of each other on this small set.
- Caveats are in `docs/EVALUATION.md`: small synthetic gold set, easy filler emails, automated answer checks are a proxy.

### Phase 8: Documentation and deliverables: DONE (except the GitHub step)
- `README.md`, `docs/ARCHITECTURE.md` (system design, model choices, pgvector configuration, multi-user approach) and `docs/EVALUATION.md` (benchmarks, challenges, limitations, improvements).
- `scripts/demo.sh`: set up the environment, ingest, example queries, user switch, API and database isolation. Run from scratch on 2026-10-23 and it worked.
- Still yours: create the private GitHub repo and email Rebecca Hurd.

## 6. Deliverables Checklist

- [ ] Private GitHub repo with clean, documented Python (code is clean and tested; not yet in git, which needs you)
- [x] `requirements.txt` (and `requirements-dev.txt`)
- [x] README with setup instructions
- [x] Example synthetic email dataset (two users)
- [x] Architecture document (1-2 pages): `docs/ARCHITECTURE.md`
- [x] Demo script (setup, ingest, queries, user switch): `scripts/demo.sh`
- [x] Evaluation report (benchmarks, challenges, limitations, improvements): `docs/EVALUATION.md`
- [~] Live-demo readiness: `rag gmail-sync` works against your real account (2 messages so far); needs a realistic mailbox to rehearse with

## 7. Grading Map (100 pts + 20 bonus)

| Criterion | Points | Where addressed | Status |
|---|---|---|---|
| LLM integration | 15 | Phase 0, 4 (Ollama + Llama 3.1 8B / Phi-3 Mini) | Done |
| Vector DB | 15 | Phase 0, 3 (pgvector schema, HNSW, metadata) | Done |
| Email processing | 15 | Phase 1-3 (robust parsing, attachments) | Done; Gmail tested live only on a tiny mailbox |
| RAG pipeline | 15 | Phase 4 (hybrid retrieval, cited prompts) | Done |
| Code quality | 10 | Layout, 104 tests, docstrings | Done |
| User isolation (bonus) | 10 | Phase 5 (row-level security, filters) | Done |
| Implementation quality (bonus) | 10 | Phase 5 (token auth, security tests) | Done; Google mode mock-tested |
| Clarity | 5 | README, demo script | Done |
| Technical depth | 5 | Architecture and evaluation docs | Done |
| Extra bonus: Drive | n/a | Phase 6 | Not started |

## 8. Time Estimate

Core 8-12 h, multi-user +3-5 h, documentation 2-3 h. Drive is optional and adds about 2 h. Phases 0-5, 7 and 8 are complete. Phase 6 (Drive) is the only phase not started.

## 9. Risks and Open Questions

- **Hardware:** the machine has 24 GB of RAM. Llama 3.1 8B at Q4 (about 5-6 GB) or Q8 (about 9 GB) fits comfortably alongside Postgres, the embedding model and the API.
- **Gmail OAuth:** a Google Cloud project is required, and the consent screen stays in "testing" mode with test users. Testing-mode tokens expire after 7 days. Two real accounts are needed for the multi-user demo on real data. The synthetic mailboxes are the fallback.
- **Untested against real Google:** the Gmail client and Google ID-token auth have only been tested with fakes and mocks. Expect small surprises on first contact.
- **Privacy:** real emails stay local. Tokens, `.env` and `secrets/` are git-ignored.
- **Live demo:** the evaluators will send new emails, so ingestion must be fast and re-runnable on demand. `rag gmail-sync` is incremental.
- **Aggregate queries** ("summarize all…") can exceed the context window. Currently handled by trimming, with map-reduce as a documented limitation.
- **Name-based queries** ("Sarah") rely on the header text in each chunk plus the sender filter, not on embeddings alone.
- **Row-level security threat model:** it guards against bugs in query code, not a compromised process with arbitrary SQL access. Stronger options (a database login per user, signed session claims) go in the production notes.
- **Python 3.14:** the only version on this machine. Everything installed fine, but 3.12 is the fallback if a package breaks.
