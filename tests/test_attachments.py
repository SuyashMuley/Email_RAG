import io

import pytest

from rag.ingest.attachments import extract_text
from rag.ingest.parser import Attachment


def att(name, data, ctype="application/octet-stream"):
    return Attachment(name, ctype, data)


def test_plain_text_and_csv():
    assert extract_text(att("n.txt", b"hello world", "text/plain")) == "hello world"
    assert "a,b" in extract_text(att("d.csv", b"a,b\n1,2", "text/csv"))


def test_html_attachment_is_converted():
    text = extract_text(att("p.html", b"<p>Hi <b>there</b></p><script>x()</script>"))
    assert "Hi" in text and "<" not in text and "x()" not in text


def test_pdf():
    pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 700, "Contingency reserve: $38,500")
    c.save()
    assert "Contingency reserve: $38,500" in extract_text(att("b.pdf", buf.getvalue(), "application/pdf"))


def test_docx_including_tables():
    from docx import Document

    doc = Document()
    doc.add_paragraph("Launch date: November 18")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Owner"
    table.rows[0].cells[1].text = "Tom"
    buf = io.BytesIO()
    doc.save(buf)
    text = extract_text(att("plan.docx", buf.getvalue()))
    assert "Launch date: November 18" in text and "Owner | Tom" in text


def test_xlsx():
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["Item", "Cost"])
    wb.active.append(["Cloud", 96000])
    buf = io.BytesIO()
    wb.save(buf)
    text = extract_text(att("budget.xlsx", buf.getvalue()))
    assert "Item | Cost" in text and "Cloud | 96000" in text


def test_unsupported_and_corrupt_return_none():
    assert extract_text(att("photo.png", b"\x89PNG", "image/png")) is None
    assert extract_text(att("broken.pdf", b"not a pdf", "application/pdf")) is None
    assert extract_text(att("empty.txt", b"   ", "text/plain")) is None
