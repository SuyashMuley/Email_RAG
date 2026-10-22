"""Offline email sources: .mbox files and directories of .eml files."""

import email
import mailbox
from collections.abc import Iterator
from email import policy
from pathlib import Path

from rag.ingest.parser import ParsedEmail, parse_message


def _factory(fp):
    return email.message_from_binary_file(fp, policy=policy.default)


def iter_mbox(path: Path) -> Iterator[ParsedEmail]:
    for msg in mailbox.mbox(path, factory=_factory):
        yield parse_message(msg)


def iter_eml_dir(path: Path) -> Iterator[ParsedEmail]:
    for eml in sorted(path.glob("*.eml")):
        with eml.open("rb") as fp:
            yield parse_message(_factory(fp))


def iter_emails(path: Path) -> Iterator[ParsedEmail]:
    """Load from a .mbox file or a directory of .eml files."""
    path = Path(path)
    return iter_eml_dir(path) if path.is_dir() else iter_mbox(path)
