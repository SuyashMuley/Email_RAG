"""Gmail ingestion: OAuth, paginated listing, raw message fetch and incremental sync.

Messages are fetched with format=raw, which includes attachments inline in the MIME payload,
so the same parser used for .mbox files handles them. Read-only scope only.
"""

import base64
import email
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from email import policy
from pathlib import Path

import psycopg
from googleapiclient.errors import HttpError

from rag.config import get_settings
from rag.db import repo
from rag.embed.embedder import Embedder
from rag.ingest.parser import ParsedEmail, parse_message
from rag.ingest.pipeline import IngestStats, ingest_emails

log = logging.getLogger(__name__)

# openid + email let `rag token` obtain a Google ID token proving which account is signed in.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]
RETRIES = 5  # googleapiclient backs off on 429 and 5xx


class CredentialsMissing(RuntimeError):
    pass


def token_path(email_address: str) -> Path:
    return Path(get_settings().token_dir) / f"token_{email_address.lower()}.json"


def authorize() -> str:
    """Run the browser OAuth flow, save the token, and return the authorized account's address."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds_file = Path(get_settings().gmail_credentials_path)
    if not creds_file.exists():
        raise CredentialsMissing(
            f"{creds_file} not found. Create an OAuth client ID (type: Desktop app) in Google Cloud "
            "Console, enable the Gmail API, and save the downloaded JSON there. See docs/GMAIL_SETUP.md."
        )
    creds = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES).run_local_server(port=0)
    service = _build(creds)
    address = service.users().getProfile(userId="me").execute(num_retries=RETRIES)["emailAddress"]
    path = token_path(address)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json())
    path.chmod(0o600)
    return address.lower()


def _build(creds):
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def load_service(email_address: str):
    """Build a Gmail service from a saved token, refreshing it if expired."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = token_path(email_address)
    if not path.exists():
        raise CredentialsMissing(f"No token for {email_address}. Run `rag gmail-auth` first.")
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        path.write_text(creds.to_json())
    return _build(creds)


def list_message_ids(service, query: str | None = None, max_results: int | None = None) -> Iterator[str]:
    """Yield message ids newest first, following pagination. Spam and trash are excluded."""
    token, yielded = None, 0
    while True:
        resp = service.users().messages().list(
            userId="me", q=query, pageToken=token, maxResults=500
        ).execute(num_retries=RETRIES)
        for m in resp.get("messages", []):
            yield m["id"]
            yielded += 1
            if max_results and yielded >= max_results:
                return
        token = resp.get("nextPageToken")
        if not token:
            return


def history_changes(service, start_history_id: str) -> tuple[list[str], list[str]]:
    """Message ids added and deleted since a history id. Raises HttpError(404) if it has expired."""
    added: dict[str, None] = {}
    deleted: set[str] = set()
    token = None
    while True:
        resp = service.users().history().list(
            userId="me", startHistoryId=start_history_id, pageToken=token,
            historyTypes=["messageAdded", "messageDeleted"],
        ).execute(num_retries=RETRIES)
        for record in resp.get("history", []):
            for item in record.get("messagesAdded", []):
                added[item["message"]["id"]] = None
            for item in record.get("messagesDeleted", []):
                deleted.add(item["message"]["id"])
        token = resp.get("nextPageToken")
        if not token:
            break
    return [i for i in added if i not in deleted], sorted(deleted)


def fetch_email(service, message_id: str, account: str) -> ParsedEmail | None:
    """Fetch and parse one message; None if it was deleted since it was listed."""
    try:
        msg = service.users().messages().get(
            userId="me", id=message_id, format="raw"
        ).execute(num_retries=RETRIES)
    except HttpError as exc:
        if exc.resp.status == 404:
            log.warning("message %s no longer exists, skipping", message_id)
            return None
        raise
    raw = base64.urlsafe_b64decode(msg["raw"] + "=" * (-len(msg["raw"]) % 4))
    parsed = parse_message(email.message_from_bytes(raw, policy=policy.default), thread_id=msg.get("threadId"))
    parsed.source_id = message_id
    parsed.web_link = f"https://mail.google.com/mail/u/{account}/#all/{message_id}"
    return parsed


def iter_gmail(service, account: str, message_ids: Iterable[str]) -> Iterator[ParsedEmail]:
    for message_id in message_ids:
        parsed = fetch_email(service, message_id, account)
        if parsed is not None:
            yield parsed


@dataclass
class SyncResult:
    stats: IngestStats
    mode: str  # "full" | "incremental"
    deleted: int = 0


def sync_mailbox(
    conn: psycopg.Connection,
    user_id: int,
    service,
    embedder: Embedder,
    account: str,
    query: str | None = None,
    max_results: int | None = None,
    full: bool = False,
    on_progress=None,
) -> SyncResult:
    """Index a Gmail account. After a complete sync, later runs only fetch what changed.

    The history cursor only advances after a sync that covered the whole mailbox (no query and
    no limit), so a partial run never causes later mail to be skipped.
    """
    # Read the cursor before listing so mail arriving mid-sync is picked up next time.
    cursor = service.users().getProfile(userId="me").execute(num_retries=RETRIES)["historyId"]
    partial = bool(query or max_results)

    ids: list[str] | None = None
    deleted: list[str] = []
    mode = "full"
    saved = None if (full or partial) else repo.get_sync_state(conn, user_id, "gmail")
    if saved:
        try:
            ids, deleted = history_changes(service, saved)
            mode = "incremental"
        except HttpError as exc:
            if exc.resp.status != 404:
                raise
            log.warning("history id expired, falling back to a full sync")
    if ids is None:
        ids = list_message_ids(service, query, max_results)

    stats = ingest_emails(conn, user_id, iter_gmail(service, account, ids), embedder, on_progress=on_progress)
    for message_id in deleted:
        repo.delete_document(conn, user_id, "gmail", message_id)
    if not partial:
        repo.set_sync_state(conn, user_id, "gmail", cursor)
    conn.commit()
    return SyncResult(stats, mode, len(deleted))
