"""Generate two synthetic mailboxes (.mbox) for demoing retrieval and multi-user isolation.

Both users have emails on the same topics (Q4 budget, API integration, marketing,
product launch) but with different content, so the same question must give different
answers per user. Each also has a private "canary" thread that must never leak to the
other user. Attachments carry facts that appear nowhere in the email body.

Usage: python scripts/generate_synthetic.py [--noise 40] [--out data/synthetic]
"""

import argparse
import hashlib
import io
import mailbox
import random
import re
import zipfile
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

TZ = timezone(timedelta(hours=-4))


def stable_msgid(domain: str, *parts: str) -> str:
    """Deterministic Message-ID, so regenerating the dataset yields identical emails."""
    return f"<{hashlib.sha1('|'.join(parts).encode()).hexdigest()[:24]}@{domain}>"


def pdf_bytes(title: str, lines: list[str]) -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter, invariant=1)  # byte-identical output across runs
    y = 740
    c.setFont("Helvetica-Bold", 14)
    c.drawString(72, y, title)
    c.setFont("Helvetica", 11)
    for line in lines:
        y -= 22
        c.drawString(72, y, line)
    c.save()
    return buf.getvalue()


def docx_bytes(title: str, paragraphs: list[str]) -> bytes:
    from docx import Document

    doc = Document()
    doc.core_properties.created = doc.core_properties.modified = datetime(2026, 1, 1)
    doc.add_heading(title, level=1)
    for p in paragraphs:
        doc.add_paragraph(p)
    raw = io.BytesIO()
    doc.save(raw)
    # python-docx stamps zip entries with the current time; rewrite with a fixed one.
    buf = io.BytesIO()
    with zipfile.ZipFile(raw) as src, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            info.date_time = (2026, 1, 1, 0, 0, 0)
            dst.writestr(info, src.read(info.filename))
    return buf.getvalue()


MIME = {
    "pdf": ("application", "pdf"),
    "docx": ("application", "vnd.openxmlformats-officedocument.wordprocessingml.document"),
}


def attachment(kind: str, filename: str, title: str, lines: list[str]) -> tuple[str, bytes]:
    data = pdf_bytes(title, lines) if kind == "pdf" else docx_bytes(title, lines)
    return filename, data


# Each message: (sender_key, when, body, options). "me" is the mailbox owner.
# options: to=[keys], cc=[keys], html=bool, attach=(filename, bytes)

def alex_threads():
    people = {
        "me": ("Alex Rivera", "alex.rivera@example.com", "Director of Product"),
        "sarah": ("Sarah Chen", "sarah.chen@acme-corp.example", "Finance Manager"),
        "priya": ("Priya Nair", "priya.nair@acme-corp.example", "Staff Engineer"),
        "marketing": ("Marketing Team", "marketing@acme-corp.example", None),
        "tom": ("Tom Okafor", "tom.okafor@acme-corp.example", "Head of Launch"),
        "maya": ("Maya Torres", "maya.torres@acme-corp.example", "Operations"),
    }
    budget_pdf = attachment("pdf", "Q4_Budget_Draft.pdf", "Q4 Budget Draft (Acme)", [
        "Marketing: $412,000", "Engineering salaries: frozen through December",
        "Cloud spend: $96,000", "Contingency reserve: $38,500",
        "Department sign-off deadline: October 31",
    ])
    launch_doc = attachment("docx", "Atlas_Launch_Plan.docx", "Atlas Launch Plan v1", [
        "Launch date: November 18. Go/no-go review: November 10.",
        "Beta customer quotes collected: 3 of 5.",
        "Press embargo lifts at 9am ET on launch day.",
    ])
    threads = [
        ("Q4 budget draft", [
            ("sarah", "2026-10-05 09:14", "Hi Alex,\n\nAttached is the draft Q4 budget. Headline numbers: the marketing line drops 8% to $412,000 and engineering headcount stays frozen through December. Cloud spend is flat at $96,000.\n\nCan you review by Friday? Finance needs final sign-off from each department by October 31.\n\nThanks,", {"attach": budget_pdf}),
            ("me", "2026-10-06 16:40", "Sarah, thanks. Two concerns: the 8% marketing cut hits the Atlas launch campaign, and I'd like to keep one engineering contractor on through Q4. Could we move $15,000 from the contingency reserve to marketing instead?", {}),
            ("sarah", "2026-10-08 11:02", "Moving $15k from contingency to marketing is fine, I'll update the draft. The marketing line becomes $427,000. The contractor request needs CFO approval though. Send me a one-paragraph justification and I'll add it to Thursday's review.", {}),
        ]),
        ("Payments API v2 integration kickoff", [
            ("priya", "2026-10-02 10:30", "<p>Hi Alex,</p><p>Kickoff notes for the <b>Payments API v2 integration</b>:</p><ul><li>Vendor sandbox is live; API keys are in the shared vault</li><li>Rate limit is 100 requests/minute per key, so we need request batching</li><li>Target go-live: <b>November 12</b></li></ul><p>Main risk: the vendor's webhook signatures changed in v2.</p><p>Priya</p>", {"html": True}),
            ("me", "2026-10-03 08:55", "Thanks Priya. Let's add retry with exponential backoff for the 429s. Can you also confirm whether the webhook signature change needs a vendor patch?", {}),
            ("priya", "2026-10-13 14:25", "Vendor confirmed the webhook signature change is permanent (HMAC-SHA256 now). I've updated our verifier. Integration tests pass in the sandbox and load testing is next week. Go-live is still November 12.", {}),
        ]),
        ("Weekly marketing update (Oct 9)", [
            ("marketing", "2026-10-09 17:00", "Weekly marketing update: webinar signups reached 1,240 against a target of 1,000. Newsletter open rate was 38%. We're drafting the Atlas launch email sequence.", {"to": ["me"]}),
        ]),
        ("Weekly marketing update (Oct 14)", [
            ("marketing", "2026-10-14 17:05", "Marketing update: the Atlas launch email sequence is drafted. We need your sign-off on the subject lines by October 17. Because of the Q4 budget cut we're dropping the paid social test.", {"to": ["me"]}),
        ]),
        ("Atlas product launch plan", [
            ("tom", "2026-10-07 13:20", "Hi all,\n\nAtlas product launch plan v1 is attached. Launch date is November 18. Workstreams: pricing page (Tom), press embargo until 9am ET on launch day (Dana), API docs (Priya).\n\nOpen items: pricing page copy and the remaining two beta quotes.", {"attach": launch_doc, "to": ["me"], "cc": ["priya", "marketing"]}),
            ("me", "2026-10-10 09:12", "Looks good. One concern: the API integration go-live is November 12, only six days before launch. Can we build a fallback plan?", {"cc": ["priya"]}),
            ("tom", "2026-10-15 15:48", "Agreed. Fallback: launch with the v1 API and ship the v2 integration as a fast-follow if go-live slips. I'll add a go/no-go meeting on November 10.", {"cc": ["priya"]}),
        ]),
        ("Lisbon offsite venue", [
            ("maya", "2026-10-11 12:00", "Project Falcon offsite: the venue is booked at Hotel Avenida, Lisbon, January 20-22. Total budget is $18,400 including travel.", {"to": ["me"]}),
        ]),
    ]
    return people, threads


def jordan_threads():
    people = {
        "me": ("Jordan Lee", "jordan.lee@example.com", "Engineering Manager"),
        "sarah": ("Sarah Miller", "sarah.miller@northwind.example", "Budget Analyst"),
        "wei": ("Wei Zhang", "wei.zhang@northwind.example", "Backend Engineer"),
        "marketing": ("Marketing Team", "marketing@northwind.example", None),
        "raj": ("Raj Patel", "raj.patel@northwind.example", "Mobile Lead"),
        "landlord": ("Harbor Property Mgmt", "office@harborprops.example", None),
    }
    budget_doc = attachment("docx", "Q4_Budget_Notes.docx", "Q4 Budget Notes (Northwind)", [
        "Cloud spend: $140,000 (up 12%).",
        "Travel budget: $9,200.",
        "Two analyst hires approved; sign-off due November 5.",
    ])
    threads = [
        ("Q4 budget notes", [
            ("sarah", "2026-10-04 10:05", "Hi Jordan,\n\nQ4 budget notes attached. Cloud spend rises 12% to $140,000 because of the data migration, and we're approved to hire two analysts starting in November. Department sign-off is due November 5.\n\nBest,", {"attach": budget_doc}),
            ("me", "2026-10-05 18:20", "Thanks Sarah. Can the analyst start dates be staggered? November 1 and December 1 would ease onboarding.", {}),
            ("sarah", "2026-10-09 09:45", "Staggered starts are fine, I'll mark November 1 and December 1. Cloud spend is unchanged.", {}),
        ]),
        ("CRM API integration plan", [
            ("wei", "2026-10-01 11:10", "<p>Hi Jordan,</p><p>Plan for the <b>CRM API integration</b>:</p><ul><li>Sandbox keys received</li><li>Webhooks retry 5 times over 24 hours, so we need idempotent handlers</li><li>Go-live: <b>December 2</b></li></ul><p>Wei</p>", {"html": True}),
            ("me", "2026-10-02 14:00", "Great. Please write the idempotency design up before you start coding.", {}),
            ("wei", "2026-10-16 16:30", "Design is done: we dedupe on the event ID with a 48-hour window. First sandbox sync passed. Go-live is still December 2.", {}),
        ]),
        ("Marketing newsletter results", [
            ("marketing", "2026-10-02 15:00", "September newsletter results: click rate 4.1%, 310 new subscribers.", {"to": ["me"]}),
        ]),
        ("Marketing update: Beacon beta", [
            ("marketing", "2026-10-09 15:30", "Beacon beta landing page is live and has 620 signups. We plan a teaser video next week.", {"to": ["me"]}),
        ]),
        ("Beacon mobile app launch", [
            ("raj", "2026-10-06 10:15", "Hi Jordan,\n\nBeacon mobile app launch is set for December 3. Beta feedback so far: 82% positive, and the main complaint is slow sync on older Android phones. We're prioritizing a sync fix before launch.", {"to": ["me"]}),
            ("me", "2026-10-07 09:00", "Thanks Raj. Please send a weekly crash-rate summary through launch.", {}),
            ("raj", "2026-10-14 17:40", "Crash-free sessions are at 99.3%. The sync fix ships in beta build 14. Still on track for December 3.", {}),
        ]),
        ("Lease renewal", [
            ("landlord", "2026-10-10 09:00", "Your lease at 220 Harbor St expires December 31. The renewal rate is $2,450 per month starting January 1. Please reply by November 15.", {"to": ["me"]}),
        ]),
    ]
    return people, threads


NOISE_SENDERS = [
    ("Hacker Digest", "news@hackerdigest.example", "Weekly digest: {n} stories you may have missed", "Top stories this week: open-source databases, local LLM benchmarks, and a long read on caching."),
    ("Calendar", "calendar@example.com", "Invitation: Team sync @ {d}", "You are invited to Team sync. Agenda: status updates and blockers. Join link inside."),
    ("Orders", "orders@shopmart.example", "Your order #{n} has shipped", "Your order has shipped and will arrive in 3-5 business days. Track your package online."),
    ("Rides", "receipts@rideshare.example", "Your Tuesday evening trip", "Thanks for riding. Total fare: ${n}.40. Rated 5 stars."),
    ("GitHub", "notifications@github.example", "[repo] Pull request #{n} merged", "The pull request was merged into main. 3 files changed."),
    ("HR", "hr@example.com", "Reminder: benefits enrollment closes soon", "Open enrollment ends on the last Friday of the month. Please review your selections."),
    ("Café Lumière", "hello@cafelumiere.example", "Café crawl this Saturday ☕", "Join us for a café crawl through the old town. Croissants and espresso included."),
]


def noise_messages(rng: random.Random, owner: tuple, count: int):
    start = datetime(2026, 8, 15, tzinfo=TZ)
    for i in range(count):
        name, addr, subject, body = rng.choice(NOISE_SENDERS)
        when = start + timedelta(days=rng.randint(0, 60), hours=rng.randint(7, 20), minutes=rng.randint(0, 59))
        n = rng.randint(1000, 9999)
        msg = EmailMessage()
        msg["From"] = f"{name} <{addr}>"
        msg["To"] = f"{owner[0]} <{owner[1]}>"
        msg["Subject"] = subject.format(n=n, d=when.strftime("%b %d"))
        msg["Date"] = format_datetime(when)
        msg["Message-ID"] = stable_msgid(addr.split("@")[1], owner[1], "noise", str(i), msg["Subject"])
        msg.set_content(body)
        yield when, msg


def build_thread(people: dict, subject: str, specs: list) -> list[tuple[datetime, EmailMessage]]:
    out, prev, prev_key, prev_when, prev_body = [], None, None, None, None
    root_id = None
    for key, when_s, body, opts in specs:
        name, addr, title = people[key]
        when = datetime.fromisoformat(when_s).replace(tzinfo=TZ)
        to_keys = opts.get("to") or (["me"] if key != "me" else [prev_key])
        msg = EmailMessage()
        msg["From"] = f"{name} <{addr}>"
        msg["To"] = ", ".join(f"{people[k][0]} <{people[k][1]}>" for k in to_keys)
        if opts.get("cc"):
            msg["Cc"] = ", ".join(f"{people[k][0]} <{people[k][1]}>" for k in opts["cc"])
        msg["Subject"] = subject if prev is None else f"Re: {subject}"
        msg["Date"] = format_datetime(when)
        mid = stable_msgid(addr.split("@")[1], people["me"][1], subject, when_s, key)
        msg["Message-ID"] = mid
        if prev is not None:
            msg["In-Reply-To"] = prev["Message-ID"]
            msg["References"] = root_id if root_id == prev["Message-ID"] else f"{root_id} {prev['Message-ID']}"
        else:
            root_id = mid

        if opts.get("html"):
            msg.set_content(body, subtype="html")
        else:
            text = body
            if title:
                text += f"\n\n-- \n{name}\n{title}"
            if prev is not None:
                pname, paddr, _ = people[prev_key]
                header = f"On {prev_when:%a, %b %-d, %Y at %-I:%M %p} {pname} <{paddr}> wrote:"
                text += "\n\n" + header + "\n" + "\n".join("> " + ln for ln in prev_body.splitlines())
            msg.set_content(text)
        if opts.get("attach"):
            filename, data = opts["attach"]
            maintype, subtype = MIME[filename.rsplit(".", 1)[1]]
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

        out.append((when, msg))
        prev, prev_key, prev_when = msg, key, when
        prev_body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip() if opts.get("html") else body
    return out


def write_mbox(path: Path, owner: tuple, people: dict, threads: list, noise: int, seed: int) -> int:
    messages = []
    for subject, specs in threads:
        messages += build_thread(people, subject, specs)
    messages += list(noise_messages(random.Random(seed), owner[:2], noise))
    messages.sort(key=lambda item: item[0])

    path.unlink(missing_ok=True)
    box = mailbox.mbox(path)
    box.lock()
    for _, msg in messages:
        box.add(msg)
    box.flush()
    box.unlock()
    box.close()
    return len(messages)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--noise", type=int, default=40, help="filler emails per mailbox")
    parser.add_argument("--out", type=Path, default=Path("data/synthetic"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    for seed, (stem, builder) in enumerate([("alex_rivera", alex_threads), ("jordan_lee", jordan_threads)]):
        people, threads = builder()
        path = args.out / f"{stem}.mbox"
        n = write_mbox(path, people["me"], people, threads, args.noise, seed)
        print(f"{path}: {n} messages")


if __name__ == "__main__":
    main()
