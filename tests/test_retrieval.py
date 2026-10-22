"""Integration tests: need the pgvector container running and the synthetic mailboxes generated.

Each test run ingests the synthetic data under throwaway users and deletes them afterwards.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from rag.db import repo
from rag.ingest.mbox_loader import iter_emails
from rag.ingest.pipeline import ingest_emails
from rag.retrieve.retriever import retrieve

ROOT = Path(__file__).resolve().parent.parent
USER_A, USER_B = "test-a@example.com", "test-b@example.com"


@pytest.fixture(scope="module")
def users(db, embedder, tmp_path_factory):
    pytest.importorskip("reportlab")
    out = tmp_path_factory.mktemp("synthetic")
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_synthetic.py"), "--out", str(out)],
        check=True, capture_output=True,
    )
    db.execute("DELETE FROM users WHERE email IN (%s, %s)", (USER_A, USER_B))
    ids = {}
    for email, mbox in ((USER_A, "alex_rivera.mbox"), (USER_B, "jordan_lee.mbox")):
        ids[email] = repo.get_or_create_user(db, email)
        db.commit()
        ingest_emails(db, ids[email], iter_emails(out / mbox), embedder)
    yield ids
    db.rollback()
    db.execute("DELETE FROM users WHERE email IN (%s, %s)", (USER_A, USER_B))
    db.commit()


def ask(db, embedder, users, email, question):
    return retrieve(db, embedder, users[email], question, own_email=email)


def subjects(result):
    return [s.subject for s in result.sources]


def test_ingest_is_idempotent(db, embedder, users, tmp_path):
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_synthetic.py"), "--out", str(tmp_path)],
        check=True, capture_output=True,
    )
    stats = ingest_emails(db, users[USER_A], iter_emails(tmp_path / "alex_rivera.mbox"), embedder)
    assert stats.emails_ingested == 0 and stats.emails_unchanged == stats.emails_seen
    count = db.execute("SELECT count(*) FROM documents WHERE user_id = %s", (users[USER_A],)).fetchone()[0]
    assert count == 54  # 52 emails + 2 attachments


def test_sender_question_finds_budget_thread(db, embedder, users):
    r = ask(db, embedder, users, USER_A, "What did Sarah say about the Q4 budget?")
    assert r.sender_filter == ["sarah.chen@acme-corp.example"]
    assert "Q4 budget draft" in subjects(r)


def test_fact_only_in_attachment_is_retrievable_and_links_to_email(db, embedder, users):
    r = ask(db, embedder, users, USER_A, "What is the contingency reserve amount?")
    hit = next(s for s in r.sources if s.filename == "Q4_Budget_Draft.pdf")
    assert "$38,500" in " ".join(hit.chunks)
    assert hit.web_link and "rfc822msgid" in hit.web_link


def test_last_heard_from_returns_most_recent_first(db, embedder, users):
    r = ask(db, embedder, users, USER_A, "When did I last hear from the marketing team?")
    assert r.intent == "latest"
    assert r.sources[0].sent_at.date().isoformat() == "2026-10-14"


def test_api_integration_query_finds_payments_thread(db, embedder, users):
    r = ask(db, embedder, users, USER_A, "Show me emails discussing the API integration project")
    assert sum("Payments API" in s for s in subjects(r)) >= 2


def test_same_question_gives_each_user_their_own_data(db, embedder, users):
    a = ask(db, embedder, users, USER_A, "What did Sarah say about the Q4 budget?")
    b = ask(db, embedder, users, USER_B, "What did Sarah say about the Q4 budget?")
    assert {s.sender for s in a.sources if "Sarah" in s.sender} == {"Sarah Chen <sarah.chen@acme-corp.example>"}
    assert {s.sender for s in b.sources if "Sarah" in s.sender} == {"Sarah Miller <sarah.miller@northwind.example>"}


@pytest.mark.parametrize(
    "asker, question, secret",
    [
        (USER_A, "Tell me about the lease renewal at Harbor St", "Harbor"),
        (USER_A, "What is the Beacon mobile app launch date?", "Beacon"),
        (USER_B, "Where is the Project Falcon offsite in Lisbon?", "Lisbon"),
        (USER_B, "Summarize the Atlas product launch plan", "Atlas"),
    ],
)
def test_no_cross_user_leakage(db, embedder, users, asker, question, secret):
    r = ask(db, embedder, users, asker, question)
    leaked = [s for s in r.sources if secret.lower() in (s.subject + " ".join(s.chunks)).lower()]
    assert not leaked
    own_ids = {row[0] for row in db.execute("SELECT id FROM documents WHERE user_id = %s", (users[asker],))}
    assert {s.document_id for s in r.sources} <= own_ids
