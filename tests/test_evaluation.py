from dataclasses import dataclass

import pytest

from rag.evaluation import (
    answer_passes, first_rank, matches, percentile, recall_at_k, score_answer, summarize,
)


@dataclass
class Src:
    subject: str
    sender: str = "Sarah Chen <sarah.chen@acme.example>"
    filename: str | None = None
    score: float = 0.0


def test_matches_requires_subject_and_optional_sender_and_filename():
    s = Src("Q4 budget")
    assert matches(s, {"subject": "Q4 budget"})
    assert matches(s, {"subject": "Q4 budget", "sender": "SARAH.chen"})
    assert not matches(s, {"subject": "Q4 budget", "sender": "tom"})
    assert not matches(s, {"subject": "Other"})
    assert not matches(s, {"subject": "Q4 budget", "filename": "a.pdf"})
    assert matches(Src("Q4 budget", filename="a.pdf"), {"subject": "Q4 budget"})  # lenient without filename
    assert matches(Src("Q4 budget", filename="a.pdf"), {"subject": "Q4 budget", "filename": "a.pdf"})


def test_ranking_uses_score_not_list_order():
    sources = [Src("A", score=0.1), Src("B", score=0.9), Src("C", score=0.5)]
    assert first_rank(sources, [{"subject": "C"}]) == 2
    assert first_rank(sources, [{"subject": "B"}, {"subject": "A"}]) == 1
    assert first_rank(sources, [{"subject": "Z"}]) is None


def test_recall_at_k_counts_specs_not_sources():
    sources = [Src("A", score=3), Src("B", score=2), Src("C", score=1)]
    specs = [{"subject": "A"}, {"subject": "C"}]
    assert recall_at_k(sources, specs, 1) == 0.5
    assert recall_at_k(sources, specs, 3) == 1.0


def test_score_answer_facts_ignore_case_commas_and_dollar_signs():
    s = score_answer("The reserve is $38,500 [1].", 2, facts=[["38,500"], ["reserve"]])
    assert (s["facts_found"], s["facts_total"], s["cited"]) == (2, 2, True)


def test_score_answer_flags_invalid_citations_and_forbidden_text():
    s = score_answer("Rent is $2,450 [3].", 2, forbidden=["2,450"])
    assert s["forbidden_hit"] == ["2,450"] and s["invalid_citations"] == [3]
    assert not answer_passes(s)


@pytest.mark.parametrize(
    "text",
    ["I could not find that in the emails.", "I couldn't find anything.", "There is no mention of it.", "I don't have that."],
)
def test_abstention_detection(text):
    assert score_answer(text, 1)["abstained"]


def test_answer_passes_logic():
    ok = score_answer("It is November 18 [1].", 1, facts=[["november 18"]])
    assert answer_passes(ok)
    missing = score_answer("It is soon [1].", 1, facts=[["november 18"]])
    assert not answer_passes(missing)
    assert answer_passes(score_answer("I could not find it.", 1, abstain_expected=True))
    assert not answer_passes(score_answer("It is November 18 [1].", 1, abstain_expected=True))
    # saying "could not find" while also expecting facts is a failure
    assert not answer_passes(score_answer("Could not find it.", 1, facts=[["x"]]))


def test_percentiles_and_summary():
    assert percentile([1, 2, 3, 4, 5], 50) == 3
    assert percentile([10], 95) == 10
    assert percentile([0, 10], 95) == pytest.approx(9.5)
    s = summarize([1, 2, 3, 4])
    assert s["n"] == 4 and s["p50"] == 2.5 and s["max"] == 4
