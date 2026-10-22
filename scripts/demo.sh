#!/usr/bin/env bash
# End-to-end demo: environment, ingestion, example queries, and multi-user isolation.
#
#   scripts/demo.sh                 full run, including environment setup
#   SKIP_SETUP=1 scripts/demo.sh    skip step 1 when the environment is already up
#
# Needs: Docker (running), Ollama (running), Python 3.11+. First run downloads about 5 GB of model weights.
set -euo pipefail
cd "$(dirname "$0")/.."

RAG=.venv/bin/rag
ALEX=alex.rivera@example.com
JORDAN=jordan.lee@example.com
PORT=8765

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
note() { printf '\033[2m%s\033[0m\n' "$*"; }
quiet() { grep -v -i -E 'torch|Loading weights|HF_TOKEN|self\.dim|get_dim'; }  # hide library warnings

if [[ -z "${SKIP_SETUP:-}" ]]; then
  step "1. Set up the environment"
  [[ -d .venv ]] || python3 -m venv .venv
  .venv/bin/pip install -q -r requirements-dev.txt -e .

  if [[ ! -f .env ]]; then
    cp .env.example .env
  fi
  if ! grep -q '^LOCAL_JWT_SECRET=.\+' .env; then  # generate a random signing secret for local tokens
    .venv/bin/python - <<'PY'
import re, secrets, pathlib
p = pathlib.Path(".env")
text = re.sub(r"^LOCAL_JWT_SECRET=.*$", f"LOCAL_JWT_SECRET={secrets.token_urlsafe(48)}", p.read_text(), flags=re.M)
p.write_text(text)
PY
    note "generated LOCAL_JWT_SECRET in .env"
  fi

  docker compose up -d --wait
  $RAG init-db
  ollama list | grep -q '^llama3.1:8b' || ollama pull llama3.1:8b
  .venv/bin/python scripts/smoke_test.py 2>&1 | quiet
fi

step "2. Ingest the sample mailboxes (two users)"
.venv/bin/python scripts/generate_synthetic.py
$RAG ingest data/synthetic/alex_rivera.mbox -u "$ALEX" 2>&1 | quiet | grep -v 'processed'
$RAG ingest data/synthetic/jordan_lee.mbox -u "$JORDAN" 2>&1 | quiet | grep -v 'processed'
note "re-running is safe: unchanged emails are skipped"
$RAG ingest data/synthetic/alex_rivera.mbox -u "$ALEX" 2>&1 | quiet | grep -v 'processed' | head -1

step "3. Example queries, as Alex"
for q in \
  "What did Sarah say about the Q4 budget?" \
  "Show me emails discussing the API integration project" \
  "When did I last hear from the marketing team?" \
  "Summarize all conversations about the product launch" \
  "How much is the contingency reserve in the draft budget?"; do
  printf '\n\033[1m> %s\033[0m\n' "$q"
  $RAG ask "$q" -u "$ALEX" 2>&1 | quiet
done
note "the last question is answered from a PDF attachment; the source links to the parent email"

step "4. Isolation: the same question as a different user"
q="What did Sarah say about the Q4 budget?"
printf '\n\033[1m> Jordan: %s\033[0m\n' "$q"
$RAG ask "$q" -u "$JORDAN" 2>&1 | quiet

step "5. Isolation: Jordan asks about Alex's private thread"
printf '\n\033[1m> Jordan: Where is the Project Falcon offsite in Lisbon?\033[0m\n'
$RAG ask "Where is the Project Falcon offsite in Lisbon?" -u "$JORDAN" 2>&1 | quiet | head -3

step "6. Isolation through the HTTP API (bearer tokens)"
LOG=$(mktemp)
$RAG serve --port "$PORT" >"$LOG" 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT
until curl -s "localhost:$PORT/healthz" >/dev/null; do sleep 1; done

TOKEN_A=$($RAG token -u "$ALEX")
TOKEN_J=$($RAG token -u "$JORDAN")
call() { curl -s -X POST "localhost:$PORT/query" -H "Authorization: Bearer $1" -H 'content-type: application/json' -d "$2"; }

printf '\nno token                -> HTTP '; curl -s -o /dev/null -w '%{http_code}\n' "localhost:$PORT/me"
printf 'forged token            -> HTTP '; curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer ${TOKEN_A%?}x" "localhost:$PORT/me"
printf 'Alex /me                -> '; curl -s -H "Authorization: Bearer $TOKEN_A" "localhost:$PORT/me"; echo
printf 'Jordan /me              -> '; curl -s -H "Authorization: Bearer $TOKEN_J" "localhost:$PORT/me"; echo
printf 'Jordan sends user_id    -> '; call "$TOKEN_J" "{\"question\":\"hi\",\"user_id\":1}" | head -c 120; echo
printf 'Jordan asks for Lisbon  -> '
call "$TOKEN_J" '{"question":"Where is the Project Falcon offsite in Lisbon?"}' \
  | .venv/bin/python -c "import sys, json; r = json.load(sys.stdin); print(r['answer'][:110]); print('   Lisbon in sources:', 'lisbon' in json.dumps(r['sources']).lower())"

step "7. The database enforces it too (row-level security)"
note "a connection to the restricted role with no user set sees nothing:"
docker exec rag-pgvector env PGPASSWORD=rag_app psql -h localhost -U rag_app -d rag -Atc \
  "SELECT 'documents visible with no user set: ' || count(*) FROM documents"
note "all done. Gmail: see docs/GMAIL_SETUP.md. Benchmarks: docs/benchmarks/RESULTS.md"
