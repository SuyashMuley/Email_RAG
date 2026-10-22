import email
from email import policy
from email.message import EmailMessage
from pathlib import Path

import pytest

from rag.ingest.parser import clean_body, parse_message


def parse_raw(raw: str):
    return parse_message(email.message_from_string(raw, policy=policy.default))


BASIC = """\
From: Sarah Chen <Sarah.Chen@Acme.example>
To: Alex Rivera <alex@example.com>, bob@example.com
Cc: "Doe, Jane" <jane@example.com>
Subject: Q4
 budget
Date: Mon, 05 Oct 2026 09:14:00 -0400
Message-ID: <abc@acme.example>
Content-Type: text/plain; charset=utf-8

Marketing drops 8%.
"""


def test_headers_and_addresses_are_normalized():
    p = parse_raw(BASIC)
    assert p.sender == "sarah.chen@acme.example"
    assert p.sender_name == "Sarah Chen"
    assert p.recipients == ["alex@example.com", "bob@example.com"]
    assert p.cc == ["jane@example.com"]
    assert p.subject == "Q4 budget"
    assert p.message_id == "<abc@acme.example>"
    assert p.body == "Marketing drops 8%."


def test_date_is_converted_to_utc():
    p = parse_raw(BASIC)
    assert p.sent_at.isoformat() == "2026-10-05T13:14:00+00:00"


def test_missing_date_falls_back_to_received_then_none():
    raw = "From: a@x.com\nReceived: from mx by y; Tue, 06 Oct 2026 10:00:00 +0000\nSubject: s\n\nhi\n"
    assert parse_raw(raw).sent_at.isoformat() == "2026-10-06T10:00:00+00:00"
    assert parse_raw("From: a@x.com\nSubject: s\n\nhi\n").sent_at is None


def test_malformed_date_does_not_crash():
    assert parse_raw("From: a@x.com\nDate: not a date\n\nhi\n").sent_at is None


def test_missing_headers_do_not_crash():
    p = parse_raw("\nbody only\n")
    assert (p.sender, p.recipients, p.subject, p.message_id) == ("", [], "", "")
    assert p.body == "body only"


def test_thread_id_uses_first_reference_then_in_reply_to_then_self():
    root = parse_raw("Message-ID: <r@x>\n\nhi\n")
    assert root.thread_id == "<r@x>"
    reply = parse_raw("Message-ID: <b@x>\nIn-Reply-To: <a@x>\nReferences: <r@x> <a@x>\n\nhi\n")
    assert reply.thread_id == "<r@x>"
    only_irt = parse_raw("Message-ID: <b@x>\nIn-Reply-To: <a@x>\n\nhi\n")
    assert only_irt.thread_id == "<a@x>"


def test_thread_id_override_for_gmail():
    assert parse_message(email.message_from_string("Message-ID: <m@x>\n\nhi\n", policy=policy.default), thread_id="gm123").thread_id == "gm123"


def test_html_only_body_is_converted_to_text():
    msg = EmailMessage()
    msg["From"] = "a@x.com"
    msg.set_content("<html><head><style>p{}</style></head><body><p>Hello</p><ul><li>one</li><li>two</li></ul><script>x()</script></body></html>", subtype="html")
    body = parse_message(msg).body
    assert "Hello" in body and "one" in body and "two" in body
    assert "<" not in body and "x()" not in body and "p{}" not in body


def test_plain_preferred_over_html_in_alternative():
    msg = EmailMessage()
    msg["From"] = "a@x.com"
    msg.set_content("plain version")
    msg.add_alternative("<p>html version</p>", subtype="html")
    assert parse_message(msg).body == "plain version"


def test_attachments_extracted_and_body_unaffected():
    msg = EmailMessage()
    msg["From"] = "a@x.com"
    msg.set_content("see attached")
    msg.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="report.pdf")
    p = parse_message(msg)
    assert p.body == "see attached"
    assert [(a.filename, a.content_type, a.data) for a in p.attachments] == [
        ("report.pdf", "application/pdf", b"%PDF-1.4 fake")
    ]


def test_unknown_charset_does_not_crash():
    raw = "From: a@x.com\nContent-Type: text/plain; charset=x-nonexistent\nContent-Transfer-Encoding: 8bit\n\ncaf\xe9 ok\n"
    assert "ok" in parse_raw(raw).body


@pytest.mark.parametrize(
    "text, expected",
    [
        ("New text.\n\nOn Mon, Oct 5, 2026 at 9:14 AM Sarah <s@x.com> wrote:\n> old\n> older", "New text."),
        ("New text.\n\nOn Mon, Oct 5, 2026 at 9:14 AM Sarah Chen\n<s@x.com> wrote:\n> old", "New text."),
        ("Thanks.\n\n-- \nAlex\nDirector", "Thanks."),
        ("Yes.\n\nSent from my iPhone", "Yes."),
        ("Reply\n\n-----Original Message-----\nFrom: x\nold stuff", "Reply"),
        ("Inline reply\n> quoted line\nmore text", "Inline reply\nmore text"),
        ("a\n\n\n\n\nb", "a\n\nb"),
    ],
)
def test_clean_body(text, expected):
    assert clean_body(text) == expected


def test_synthetic_mailboxes_parse():
    pytest.importorskip("reportlab")
    pytest.importorskip("docx")
    import subprocess
    import sys
    import tempfile

    from rag.ingest.mbox_loader import iter_emails

    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory() as out:
        subprocess.run(
            [sys.executable, str(root / "scripts" / "generate_synthetic.py"), "--noise", "5", "--out", out],
            check=True, capture_output=True,
        )
        emails = list(iter_emails(Path(out) / "alex_rivera.mbox"))
    assert len(emails) == 12 + 5  # 12 thread messages + 5 noise
    budget = [e for e in emails if e.subject == "Q4 budget draft"]
    assert budget and budget[0].sender == "sarah.chen@acme-corp.example"
    assert budget[0].attachments[0].filename == "Q4_Budget_Draft.pdf"
    reply = next(e for e in emails if e.subject == "Re: Q4 budget draft" and e.sender.startswith("alex"))
    assert "On " not in reply.body and "> " not in reply.body
    assert reply.thread_id == budget[0].message_id
    assert all(e.sent_at is not None for e in emails)
