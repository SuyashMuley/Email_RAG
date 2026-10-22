"""Parse RFC 822 messages into a normalized, storage-ready structure."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from bs4 import BeautifulSoup


@dataclass
class Attachment:
    filename: str
    content_type: str
    data: bytes


@dataclass
class ParsedEmail:
    message_id: str
    thread_id: str
    sender: str
    sender_name: str
    recipients: list[str]
    cc: list[str]
    subject: str
    body: str
    sent_at: datetime | None
    attachments: list[Attachment] = field(default_factory=list)
    web_link: str | None = None  # set by sources that know the real link (e.g. Gmail)
    source_id: str | None = None  # stable id from the source (e.g. Gmail message id); defaults to Message-ID


# "On Mon, Oct 5, 2026 at 9:14 AM Sarah Chen <sarah@x.com> wrote:" (may wrap onto two lines)
_QUOTE_HEADER = re.compile(r"^On\b[^\n]*(?:\n[^\n]*)?\bwrote:\s*$", re.MULTILINE)
_ORIGINAL_MESSAGE = re.compile(r"^-{2,}\s*(Original Message|Forwarded message)\s*-{2,}", re.I | re.MULTILINE)
_SIGNATURE_SEP = re.compile(r"^-- ?$", re.MULTILINE)
_MOBILE_SIG = re.compile(r"^Sent from my (iPhone|iPad|Android|mobile).*$", re.I | re.MULTILINE)


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    return soup.get_text("\n")


def clean_body(text: str) -> str:
    """Drop quoted replies, signatures and excess whitespace, keeping only the new content."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for pattern in (_QUOTE_HEADER, _ORIGINAL_MESSAGE, _SIGNATURE_SEP, _MOBILE_SIG):
        match = pattern.search(text)
        if match:
            text = text[: match.start()]
    lines = [ln.rstrip() for ln in text.split("\n") if not ln.lstrip().startswith(">")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _decode_part(part: Message) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError, KeyError):
        payload = part.get_payload(decode=True) or b""
        try:
            return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except LookupError:  # charset name Python doesn't know
            return payload.decode("utf-8", errors="replace")


def extract_body(msg: Message) -> str:
    """Prefer text/plain, falling back to text/html converted to text."""
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        text = clean_body(_decode_part(plain))
        if text:
            return text
    html = msg.get_body(preferencelist=("html",))
    if html is not None:
        return clean_body(html_to_text(_decode_part(html)))
    return ""


def extract_attachments(msg: Message) -> list[Attachment]:
    attachments = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if not filename and part.get_content_disposition() != "attachment":
            continue
        data = part.get_payload(decode=True)
        if data:
            attachments.append(
                Attachment(filename or "attachment", part.get_content_type(), data)
            )
    return attachments


def _addresses(msg: Message, header: str) -> list[str]:
    values = [str(v) for v in msg.get_all(header, [])]
    return [addr.lower() for _, addr in getaddresses(values) if addr]


def _sent_at(msg: Message) -> datetime | None:
    candidates = [msg.get("Date")]
    received = msg.get_all("Received", [])
    if received:  # Received: from ... ; <date>
        candidates.append(str(received[0]).rsplit(";", 1)[-1].strip())
    for value in candidates:
        if not value:
            continue
        try:
            dt = parsedate_to_datetime(str(value))
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def _message_ids(value: str | None) -> list[str]:
    return re.findall(r"<[^<>]+>", str(value)) if value else []


def parse_message(msg: Message, thread_id: str | None = None) -> ParsedEmail:
    """Parse one message. `thread_id` overrides header-based threading (e.g. Gmail's threadId)."""
    name, sender = parseaddr(str(msg.get("From", "")))
    message_id = (_message_ids(msg.get("Message-ID")) or [""])[0]

    if thread_id is None:
        # Root of the conversation: first References entry, else In-Reply-To, else itself.
        chain = _message_ids(msg.get("References")) + _message_ids(msg.get("In-Reply-To"))
        thread_id = chain[0] if chain else message_id

    return ParsedEmail(
        message_id=message_id,
        thread_id=thread_id,
        sender=sender.lower(),
        sender_name=name,
        recipients=_addresses(msg, "To"),
        cc=_addresses(msg, "Cc"),
        subject=" ".join(str(msg.get("Subject", "")).split()),
        body=extract_body(msg),
        sent_at=_sent_at(msg),
        attachments=extract_attachments(msg),
    )
