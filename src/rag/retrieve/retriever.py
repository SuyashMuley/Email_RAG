"""Hybrid retrieval: pgvector similarity + Postgres full-text, fused with reciprocal rank fusion.

Every query is scoped to a single user_id. Light query analysis adds a sender filter when the
question names a known sender ("What did Sarah say..."), and handles "last heard from X" style
questions by sorting on date instead of similarity.
"""

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr

import psycopg

from rag.embed.embedder import Embedder

CANDIDATES = 30  # per retriever, before fusion
MIN_SCORE_RATIO = 0.45  # drop sources scoring below this fraction of the best one
RRF_K = 60
MAX_CHUNKS_PER_SOURCE = 3

_LATEST = re.compile(r"\b(last|latest|most recent|recent)\b", re.I)
_FROM = re.compile(r"\bfrom\b", re.I)
_SUMMARIZE = re.compile(r"\b(summari[sz]e|summary|overview|recap)\b", re.I)


# Words that describe the request rather than the content; matching them adds noise to full-text search.
_GENERIC_TERMS = {
    "show", "find", "list", "emails", "email", "mail", "mails", "message", "messages", "discuss",
    "discussing", "discussed", "conversation", "conversations", "summarize", "summary", "tell",
    "say", "said", "talk", "talking", "talked", "mention", "mentioned", "last", "latest", "recent",
    "hear", "heard", "did", "does", "when", "what", "who", "which", "all", "about", "regarding",
}


@dataclass
class Source:
    document_id: int
    source: str  # gmail | attachment | drive
    subject: str
    sender: str
    sent_at: datetime | None
    web_link: str | None
    filename: str | None
    chunks: list[str]  # chunk bodies, header stripped, in document order
    score: float
    n: int = 0  # citation number, assigned after ranking


@dataclass
class Retrieval:
    sources: list[Source]
    intent: str  # "search" | "latest" | "summarize"
    sender_filter: list[str] = field(default_factory=list)
    embed_ms: float = 0.0
    search_ms: float = 0.0


_COLS = """c.id AS chunk_id, c.document_id, c.chunk_index, c.text, d.source, d.subject, d.sender,
           d.sent_at, d.web_link, d.filename"""


def _fts_query(question: str) -> str | None:
    words = {w.lower() for w in re.findall(r"[A-Za-z0-9]+", question)} - _GENERIC_TERMS
    return " | ".join(sorted(words)) or None


def _body(chunk_text: str) -> str:
    """Strip the metadata header (everything before the first blank line)."""
    head, sep, body = chunk_text.partition("\n\n")
    return body if sep else ""


def known_senders(conn: psycopg.Connection, user_id: int, own_email: str | None = None) -> dict[str, str]:
    """Map every sender address seen in this user's mailbox to its display name."""
    rows = conn.execute(
        "SELECT DISTINCT sender FROM documents WHERE user_id = %s AND source = 'gmail'", (user_id,)
    ).fetchall()
    senders = {}
    for (raw,) in rows:
        name, addr = parseaddr(raw)
        if addr and addr != (own_email or "").lower():
            senders[addr] = name
    return senders


def match_senders(question: str, senders: dict[str, str]) -> list[str]:
    """Addresses of senders mentioned by full name (any case) or capitalized first name."""
    matched = []
    for addr, name in senders.items():
        if not name:
            continue
        first = name.split()[0]
        if re.search(rf"\b{re.escape(name)}\b", question, re.I) or re.search(rf"\b{re.escape(first)}\b", question):
            matched.append(addr)
    return matched


def _filter_sql(sender_addrs: list[str], sender_only: bool) -> tuple[str, dict]:
    if not sender_addrs:
        return "", {}
    likes = [f"%<{a}>%" for a in sender_addrs]
    clause = "lower(d.sender) LIKE ANY(%(likes)s)"
    if not sender_only:
        clause += " OR d.recipients && %(addrs)s"
    return f" AND ({clause})", {"likes": likes, "addrs": sender_addrs}


def _search_candidates(conn, user_id, qvec, question, filt_sql, filt_params, mode="hybrid") -> dict[int, dict]:
    params = {"uid": user_id, "q": qvec, "n": CANDIDATES, **filt_params}
    vec_rows = []
    if mode in ("hybrid", "dense"):
        vec_rows = conn.execute(
            f"SELECT {_COLS} FROM chunks c JOIN documents d ON d.id = c.document_id "
            f"WHERE c.user_id = %(uid)s{filt_sql} ORDER BY c.embedding <=> %(q)s LIMIT %(n)s",
            params,
        ).fetchall()

    rows_by_id: dict[int, dict] = {}
    scores: dict[int, float] = {}
    names = ["chunk_id", "document_id", "chunk_index", "text", "source", "subject", "sender",
             "sent_at", "web_link", "filename"]

    def add(rows):
        for rank, row in enumerate(rows):
            rec = dict(zip(names, row))
            rows_by_id.setdefault(rec["chunk_id"], rec)
            scores[rec["chunk_id"]] = scores.get(rec["chunk_id"], 0.0) + 1.0 / (RRF_K + rank + 1)

    add(vec_rows)

    tsq = _fts_query(question) if mode in ("hybrid", "fts") else None
    if tsq:
        fts_rows = conn.execute(
            f"SELECT {_COLS} FROM chunks c JOIN documents d ON d.id = c.document_id, "
            f"to_tsquery('english', %(tsq)s) q "
            f"WHERE c.user_id = %(uid)s AND c.tsv @@ q{filt_sql} ORDER BY ts_rank_cd(c.tsv, q) DESC LIMIT %(n)s",
            {**params, "tsq": tsq},
        ).fetchall()
        add(fts_rows)

    for cid, rec in rows_by_id.items():
        rec["score"] = scores[cid]
    return rows_by_id


def _group(candidates: dict[int, dict], k_docs: int) -> list[Source]:
    """Group chunk hits by document, ranked by their best chunk."""
    by_doc: dict[int, list[dict]] = {}
    for rec in sorted(candidates.values(), key=lambda r: -r["score"]):
        by_doc.setdefault(rec["document_id"], []).append(rec)
    sources = []
    best = max((r["score"] for r in candidates.values()), default=0.0)
    for recs in list(by_doc.values())[:k_docs]:
        if recs[0]["score"] < best * MIN_SCORE_RATIO:
            break
        top = sorted(recs[:MAX_CHUNKS_PER_SOURCE], key=lambda r: r["chunk_index"])
        first = recs[0]
        sources.append(Source(
            document_id=first["document_id"], source=first["source"], subject=first["subject"],
            sender=first["sender"], sent_at=first["sent_at"], web_link=first["web_link"],
            filename=first["filename"], chunks=[_body(r["text"]) or r["text"] for r in top],
            score=first["score"],
        ))
    return sources


def _latest_from(conn, user_id, sender_addrs, limit) -> list[Source]:
    rows = conn.execute(
        f"SELECT {_COLS} FROM documents d JOIN chunks c ON c.document_id = d.id AND c.chunk_index = 0 "
        "WHERE d.user_id = %s AND d.source = 'gmail' AND lower(d.sender) LIKE ANY(%s) "
        "ORDER BY d.sent_at DESC NULLS LAST LIMIT %s",
        (user_id, [f"%<{a}>%" for a in sender_addrs], limit),
    ).fetchall()
    return [
        Source(r[1], r[4], r[5], r[6], r[7], r[8], r[9], [_body(r[3]) or r[3]], 1.0 - i * 0.01)
        for i, r in enumerate(rows)
    ]


def retrieve(
    conn: psycopg.Connection,
    embedder: Embedder,
    user_id: int,
    question: str,
    own_email: str | None = None,
    k_docs: int | None = None,
    mode: str = "hybrid",
    analysis: bool = True,
) -> Retrieval:
    """Find the sources for a question.

    `mode` ("hybrid", "dense" or "fts") and `analysis` (sender filter and "latest" handling)
    exist so the benchmark can ablate each part; the application always uses the defaults.
    """
    intent = "summarize" if _SUMMARIZE.search(question) else "search"
    k_docs = k_docs or (10 if intent == "summarize" else 5)

    senders = match_senders(question, known_senders(conn, user_id, own_email)) if analysis else []

    if senders and _LATEST.search(question) and _FROM.search(question):
        t0 = time.perf_counter()
        sources = _latest_from(conn, user_id, senders, limit=3)
        if sources:
            for i, s in enumerate(sources, 1):
                s.n = i
            return Retrieval(sources, "latest", senders, search_ms=(time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    qvec = embedder.embed_query(question)
    embed_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    candidates = {}
    if senders:
        candidates = _search_candidates(
            conn, user_id, qvec, question, *_filter_sql(senders, sender_only=False), mode=mode
        )
    if not candidates:  # no sender named, or the filter matched nothing: search the whole mailbox
        senders = []
        candidates = _search_candidates(conn, user_id, qvec, question, "", {}, mode=mode)
    sources = _group(candidates, k_docs)
    # Present in chronological order so later emails that update earlier ones are easy to spot.
    sources.sort(key=lambda s: s.sent_at or datetime.min.replace(tzinfo=timezone.utc))
    for i, s in enumerate(sources, 1):
        s.n = i
    return Retrieval(sources, intent, senders, embed_ms, (time.perf_counter() - t0) * 1000)
