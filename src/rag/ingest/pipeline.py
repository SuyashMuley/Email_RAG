"""Ingest parsed emails (and their attachments) into the vector store.

Every chunk is prefixed with a metadata header (sender, recipients, date, subject) so that
both the embedding and the full-text index can match on who wrote what and when.
"""

import hashlib
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from itertools import islice
from urllib.parse import quote

import psycopg

from rag.db import repo
from rag.embed.embedder import Embedder
from rag.ingest.attachments import extract_text
from rag.ingest.chunker import chunk_text
from rag.ingest.parser import ParsedEmail


@dataclass
class IngestStats:
    emails_seen: int = 0
    emails_ingested: int = 0
    emails_unchanged: int = 0
    attachments_indexed: int = 0
    attachments_skipped: int = 0
    chunks: int = 0
    embed_seconds: float = 0.0
    total_seconds: float = 0.0


@dataclass
class _Doc:
    fields: dict
    texts: list[str]
    attachments: list["_Doc"]


def gmail_link(message_id: str) -> str:
    """Link that opens an email in Gmail by its RFC 822 Message-ID."""
    return f"https://mail.google.com/mail/u/0/#search/rfc822msgid%3A{quote(message_id.strip('<>'), safe='')}"


def format_address(name: str, addr: str) -> str:
    return f"{name} <{addr}>" if name else addr


def _format_date(dt: datetime | None) -> str:
    return dt.strftime("%a %Y-%m-%d %H:%M UTC") if dt else "unknown"


def _header(email: ParsedEmail, attachment_name: str | None = None) -> str:
    lines = [f"Attachment: {attachment_name}"] if attachment_name else []
    lines += [f"From: {format_address(email.sender_name, email.sender)}", f"To: {', '.join(email.recipients)}"]
    if email.cc:
        lines.append(f"Cc: {', '.join(email.cc)}")
    lines += [f"Date: {_format_date(email.sent_at)}", f"Subject: {email.subject}"]
    return "\n".join(lines)


def _chunks_with_header(header: str, body: str) -> list[str]:
    return [f"{header}\n\n{c}" for c in chunk_text(body)] or [header]


def _content_hash(email: ParsedEmail) -> str:
    h = hashlib.sha256()
    for part in (email.sender, ",".join(email.recipients), email.subject, str(email.sent_at), email.body):
        h.update(part.encode())
        h.update(b"\0")
    for att in email.attachments:
        h.update(hashlib.sha256(att.data).digest())
    return h.hexdigest()


def _prepare(email: ParsedEmail, stats: IngestStats) -> tuple[str, str, _Doc]:
    digest = _content_hash(email)
    source_id = email.source_id or email.message_id or f"hash:{digest}"
    link = email.web_link or (gmail_link(email.message_id) if email.message_id else None)
    common = {
        "thread_id": email.thread_id,
        "sender": format_address(email.sender_name, email.sender),
        "recipients": email.recipients + email.cc,
        "subject": email.subject,
        "sent_at": email.sent_at,
        "web_link": link,
    }
    parent = _Doc(
        {**common, "source": "gmail", "source_id": source_id, "content_hash": digest},
        _chunks_with_header(_header(email), email.body),
        [],
    )
    for att in email.attachments:
        text = extract_text(att)
        if text is None:
            stats.attachments_skipped += 1
            continue
        parent.attachments.append(_Doc(
            {**common, "source": "attachment", "source_id": f"{source_id}#{att.filename}",
             "filename": att.filename, "mime_type": att.content_type,
             "content_hash": hashlib.sha256(att.data).hexdigest()},
            _chunks_with_header(_header(email, att.filename), text),
            [],
        ))
    return source_id, digest, parent


def _batches(items: Iterable, size: int):
    it = iter(items)
    while batch := list(islice(it, size)):
        yield batch


def ingest_emails(
    conn: psycopg.Connection,
    user_id: int,
    emails: Iterable[ParsedEmail],
    embedder: Embedder,
    batch_size: int = 16,
    on_progress: Callable[[IngestStats], None] | None = None,
) -> IngestStats:
    """Idempotent: unchanged emails are skipped, changed ones are replaced."""
    stats = IngestStats()
    seen: set[str] = set()  # guards against duplicate Message-IDs within one run
    start = time.perf_counter()

    for batch in _batches(emails, batch_size):
        pending: list[_Doc] = []
        for email in batch:
            stats.emails_seen += 1
            source_id, digest, doc = _prepare(email, stats)
            if source_id in seen:
                stats.emails_unchanged += 1
                continue
            seen.add(source_id)
            old = repo.existing_hash(conn, user_id, "gmail", source_id)
            if old == digest:
                stats.emails_unchanged += 1
                continue
            if old is not None:
                repo.delete_document(conn, user_id, "gmail", source_id)
            pending.append(doc)

        if pending:
            docs = [d for p in pending for d in (p, *p.attachments)]
            all_texts = [t for d in docs for t in d.texts]
            t0 = time.perf_counter()
            vectors = iter(embedder.embed_documents(all_texts))
            stats.embed_seconds += time.perf_counter() - t0

            for parent in pending:
                parent_id = repo.insert_document(conn, user_id, **parent.fields)
                repo.insert_chunks(conn, user_id, parent_id, parent.texts, list(islice(vectors, len(parent.texts))))
                for att in parent.attachments:
                    att_id = repo.insert_document(conn, user_id, parent_doc_id=parent_id, **att.fields)
                    repo.insert_chunks(conn, user_id, att_id, att.texts, list(islice(vectors, len(att.texts))))
                    stats.attachments_indexed += 1
                stats.emails_ingested += 1
            stats.chunks += len(all_texts)
        conn.commit()
        if on_progress:
            on_progress(stats)

    stats.total_seconds = time.perf_counter() - start
    return stats
