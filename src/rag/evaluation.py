"""Scoring helpers for the benchmark: retrieval metrics and automated answer checks.

A "relevant" spec describes an email (or attachment) by subject, plus optionally a sender
substring and an attachment filename. Specs without a filename accept the email or any of its
attachments; specs with one accept only that attachment.
"""

import re
import statistics
from collections.abc import Iterable, Sequence

ABSTAIN = re.compile(
    r"(could(?: not|n't) find|can(?:not|'t) find|unable to find|no (?:relevant )?(?:information|emails?|mention|record)"
    r"|not (?:mentioned|found|contain)|(?:do not|don't) have)",
    re.I,
)
_CITATION = re.compile(r"\[(\d+)\]")


def matches(source, spec: dict) -> bool:
    if source.subject != spec["subject"]:
        return False
    if "sender" in spec and spec["sender"].lower() not in (source.sender or "").lower():
        return False
    if "filename" in spec:
        return source.filename == spec["filename"]
    return True


def by_score(sources: Iterable) -> list:
    return sorted(sources, key=lambda s: -s.score)


def first_rank(sources: Sequence, specs: Sequence[dict]) -> int | None:
    """1-based rank of the first source matching any spec, with sources ordered by score."""
    for rank, source in enumerate(by_score(sources), 1):
        if any(matches(source, spec) for spec in specs):
            return rank
    return None


def recall_at_k(sources: Sequence, specs: Sequence[dict], k: int) -> float:
    """Fraction of relevant specs matched by at least one of the top-k sources."""
    top = by_score(sources)[:k]
    return sum(any(matches(s, spec) for s in top) for spec in specs) / len(specs)


def normalize(text: str) -> str:
    return re.sub(r"[,$]", "", text).lower()


def score_answer(answer: str, n_sources: int, facts=(), forbidden=(), abstain_expected=False) -> dict:
    """Automated answer checks.

    facts: groups of alternative strings; every group needs one match.
    forbidden: strings that must not appear (hallucination guard).
    """
    text = normalize(answer)
    found = sum(any(normalize(alt) in text for alt in group) for group in facts)
    cited = {int(n) for n in _CITATION.findall(answer)}
    return {
        "facts_found": found,
        "facts_total": len(facts),
        "forbidden_hit": [f for f in forbidden if normalize(f) in text],
        "abstained": bool(ABSTAIN.search(answer)),
        "abstain_expected": abstain_expected,
        "cited": bool(cited),
        "invalid_citations": sorted(n for n in cited if not 1 <= n <= n_sources),
    }


def answer_passes(score: dict) -> bool:
    """Pass = all facts present, nothing forbidden, and the abstain behaviour matches expectation."""
    if score["forbidden_hit"] or score["invalid_citations"]:
        return False
    if score["abstain_expected"]:
        return score["abstained"]
    return score["facts_found"] == score["facts_total"] and not score["abstained"]


def percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = (len(ordered) - 1) * pct / 100
    lo, hi = int(idx), min(int(idx) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (idx - lo)


def summarize(values: Sequence[float]) -> dict:
    return {
        "n": len(values),
        "mean": statistics.fmean(values) if values else float("nan"),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "max": max(values) if values else float("nan"),
    }
