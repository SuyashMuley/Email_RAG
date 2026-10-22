"""Gmail ingestion tests against a fake Gmail API service (no network, no credentials)."""

import base64
from email.message import EmailMessage

import httplib2
import pytest
from googleapiclient.errors import HttpError

from rag.db import repo
from rag.ingest.gmail_client import fetch_email, list_message_ids, sync_mailbox

ACCOUNT = "test-gmail@example.com"


def http_error(status: int) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b"{}")


def raw_email(subject: str, body: str, attachment: bytes | None = None) -> str:
    msg = EmailMessage()
    msg["From"] = "Pat Doe <pat@example.org>"
    msg["To"] = ACCOUNT
    msg["Subject"] = subject
    msg["Date"] = "Mon, 05 Oct 2026 09:14:00 -0400"
    msg["Message-ID"] = f"<{subject.replace(' ', '-')}@example.org>"
    msg.set_content(body)
    if attachment:
        msg.add_attachment(attachment, maintype="text", subtype="plain", filename="notes.txt")
    # Gmail returns unpadded urlsafe base64
    return base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")


class Request:
    def __init__(self, fn):
        self.fn = fn

    def execute(self, num_retries=0):
        return self.fn()


class FakeGmail:
    """Just enough of the Gmail API surface used by gmail_client."""

    PAGE_SIZE = 2

    def __init__(self):
        self.messages_by_id: dict[str, str] = {}  # newest first
        self.history_id = "100"
        self.history_records: list[dict] = []
        self.history_expired = False
        self.list_calls = 0
        self.history_calls = 0

    def add(self, mid, subject, body, attachment=None):
        self.messages_by_id = {mid: raw_email(subject, body, attachment), **self.messages_by_id}

    def users(self):
        return self

    def getProfile(self, userId):
        return Request(lambda: {"emailAddress": ACCOUNT, "historyId": self.history_id})

    def messages(self):
        return self

    def list(self, userId, q=None, pageToken=None, maxResults=None):
        def run():
            self.list_calls += 1
            ids = list(self.messages_by_id)
            start = int(pageToken or 0)
            page = ids[start : start + self.PAGE_SIZE]
            out = {"messages": [{"id": i} for i in page]}
            if start + self.PAGE_SIZE < len(ids):
                out["nextPageToken"] = str(start + self.PAGE_SIZE)
            return out
        return Request(run)

    def get(self, userId, id, format):
        def run():
            if id not in self.messages_by_id:
                raise http_error(404)
            return {"id": id, "threadId": f"thread-{id}", "raw": self.messages_by_id[id]}
        return Request(run)

    def history(self):
        fake = self

        class History:
            def list(self, userId, startHistoryId, pageToken=None, historyTypes=None):
                def run():
                    fake.history_calls += 1
                    if fake.history_expired:
                        raise http_error(404)
                    return {"history": fake.history_records}
                return Request(run)

        return History()


def test_list_message_ids_follows_pagination_and_respects_max():
    gmail = FakeGmail()
    for i in range(5):
        gmail.add(f"m{i}", f"s{i}", "body")
    assert list(list_message_ids(gmail)) == ["m4", "m3", "m2", "m1", "m0"]
    assert list(list_message_ids(gmail, max_results=3)) == ["m4", "m3", "m2"]


def test_fetch_email_parses_raw_and_sets_gmail_fields():
    gmail = FakeGmail()
    gmail.add("abc123", "Quarterly numbers", "Revenue is up.", attachment=b"notes here")
    parsed = fetch_email(gmail, "abc123", ACCOUNT)
    assert parsed.subject == "Quarterly numbers"
    assert parsed.sender == "pat@example.org"
    assert parsed.body == "Revenue is up."
    assert parsed.thread_id == "thread-abc123"
    assert parsed.source_id == "abc123"
    assert parsed.web_link == f"https://mail.google.com/mail/u/{ACCOUNT}/#all/abc123"
    assert [a.filename for a in parsed.attachments] == ["notes.txt"]


def test_fetch_email_returns_none_when_message_was_deleted():
    assert fetch_email(FakeGmail(), "gone", ACCOUNT) is None


@pytest.fixture
def user_id(db):
    db.execute("DELETE FROM users WHERE email = %s", (ACCOUNT,))
    uid = repo.get_or_create_user(db, ACCOUNT)
    db.commit()
    yield uid
    db.rollback()
    db.execute("DELETE FROM users WHERE email = %s", (ACCOUNT,))
    db.commit()


def doc_ids(db, uid):
    return {r[0] for r in db.execute(
        "SELECT source_id FROM documents WHERE user_id = %s AND source = 'gmail'", (uid,))}


def test_full_then_incremental_sync_with_deletions_and_fallbacks(db, embedder, user_id):
    gmail = FakeGmail()
    gmail.add("m1", "First", "alpha topic")
    gmail.add("m2", "Second", "beta topic", attachment=b"attached facts")
    gmail.add("m3", "Third", "gamma topic")

    # 1. first sync lists everything and stores the cursor
    r = sync_mailbox(db, user_id, gmail, embedder, ACCOUNT)
    assert (r.mode, r.stats.emails_ingested, r.stats.attachments_indexed) == ("full", 3, 1)
    assert doc_ids(db, user_id) == {"m1", "m2", "m3"}
    assert repo.get_sync_state(db, user_id, "gmail") == "100"
    link = db.execute("SELECT web_link FROM documents WHERE source_id = 'm1' AND user_id = %s", (user_id,)).fetchone()[0]
    assert link == f"https://mail.google.com/mail/u/{ACCOUNT}/#all/m1"

    # 2. a new message arrives: incremental sync fetches only it, without listing the mailbox
    gmail.add("m4", "Fourth", "delta topic")
    gmail.history_id = "105"
    gmail.history_records = [{"messagesAdded": [{"message": {"id": "m4"}}]}]
    lists_before = gmail.list_calls
    r = sync_mailbox(db, user_id, gmail, embedder, ACCOUNT)
    assert (r.mode, r.stats.emails_ingested, r.stats.emails_seen) == ("incremental", 1, 1)
    assert gmail.list_calls == lists_before
    assert repo.get_sync_state(db, user_id, "gmail") == "105"

    # 3. a message is deleted in Gmail: it is removed from the index along with its attachment
    del gmail.messages_by_id["m2"]
    gmail.history_id = "110"
    gmail.history_records = [{"messagesDeleted": [{"message": {"id": "m2"}}]}]
    r = sync_mailbox(db, user_id, gmail, embedder, ACCOUNT)
    assert r.deleted == 1
    assert doc_ids(db, user_id) == {"m1", "m3", "m4"}
    assert db.execute("SELECT count(*) FROM documents WHERE user_id = %s AND source = 'attachment'", (user_id,)).fetchone()[0] == 0

    # 4. an expired history id falls back to a full re-list, and unchanged mail is skipped
    gmail.history_expired = True
    gmail.history_id = "200"
    r = sync_mailbox(db, user_id, gmail, embedder, ACCOUNT)
    assert (r.mode, r.stats.emails_ingested, r.stats.emails_unchanged) == ("full", 0, 3)
    assert repo.get_sync_state(db, user_id, "gmail") == "200"


def test_partial_sync_does_not_advance_cursor(db, embedder, user_id):
    gmail = FakeGmail()
    for i in range(4):
        gmail.add(f"m{i}", f"Subject {i}", f"body {i}")
    r = sync_mailbox(db, user_id, gmail, embedder, ACCOUNT, max_results=2)
    assert r.stats.emails_ingested == 2
    assert repo.get_sync_state(db, user_id, "gmail") is None

    r = sync_mailbox(db, user_id, gmail, embedder, ACCOUNT, query="newer_than:7d")
    assert repo.get_sync_state(db, user_id, "gmail") is None


def test_message_deleted_between_list_and_fetch_is_skipped(db, embedder, user_id):
    class Flaky(FakeGmail):
        def get(self, userId, id, format):
            if id == "m1":
                self.messages_by_id.pop("m1", None)  # vanishes after being listed
            return super().get(userId, id, format)

    gmail = Flaky()
    gmail.add("m0", "Keep", "stays")
    gmail.add("m1", "Vanishing", "gone")
    r = sync_mailbox(db, user_id, gmail, embedder, ACCOUNT)
    assert r.stats.emails_ingested == 1
    assert doc_ids(db, user_id) == {"m0"}
