"""Extract indexable text from email attachments."""

import io
import logging
from pathlib import Path

from rag.ingest.parser import Attachment, html_to_text

log = logging.getLogger(__name__)

MAX_CHARS = 200_000
MAX_SHEET_ROWS = 2_000


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader

    return "\n\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)


def _docx(data: bytes) -> str:
    from docx import Document

    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        parts += [" | ".join(cell.text for cell in row.cells) for row in table.rows]
    return "\n".join(parts)


def _xlsx(data: bytes) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parts = []
    for ws in wb.worksheets:
        parts.append(f"Sheet: {ws.title}")
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= MAX_SHEET_ROWS:
                break
            cells = ["" if v is None else str(v) for v in row]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _html(data: bytes) -> str:
    return html_to_text(_text(data))


_BY_EXTENSION = {
    ".pdf": _pdf, ".docx": _docx, ".xlsx": _xlsx,
    ".txt": _text, ".md": _text, ".csv": _text, ".log": _text,
    ".html": _html, ".htm": _html,
}
_BY_MIME = {
    "application/pdf": _pdf,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": _docx,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": _xlsx,
    "text/plain": _text, "text/csv": _text, "text/markdown": _text, "text/html": _html,
}


def extract_text(att: Attachment) -> str | None:
    """Return the attachment's text, or None if the type is unsupported or extraction fails."""
    extractor = _BY_EXTENSION.get(Path(att.filename).suffix.lower()) or _BY_MIME.get(att.content_type)
    if extractor is None:
        log.info("skipping unsupported attachment %s (%s)", att.filename, att.content_type)
        return None
    try:
        text = extractor(att.data).strip()
    except Exception as exc:  # corrupt or encrypted files shouldn't abort an ingest run
        log.warning("could not extract %s: %s: %s", att.filename, type(exc).__name__, exc)
        return None
    return text[:MAX_CHARS] or None
