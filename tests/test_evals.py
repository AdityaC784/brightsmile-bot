from collections import Counter

import pytest
from evals.golden_set import load_golden
from evals.retrieval_eval import recall_at_k, reciprocal_rank

from bot.config import ROOT


def test_recall_at_k_counts_each_gold_doc_once():
    ranked = ["DENT-014", "DENT-014", "DENT-023", "DENT-001", "DENT-002", "DENT-024"]
    assert recall_at_k(ranked, ["DENT-014"]) == 1.0
    assert recall_at_k(ranked, ["DENT-023", "DENT-024"]) == 0.5  # DENT-024 is rank 6
    assert recall_at_k(ranked, ["DENT-099"]) == 0.0


def test_reciprocal_rank_uses_first_gold_hit():
    ranked = ["DENT-001", "DENT-002", "DENT-019"]
    assert reciprocal_rank(ranked, ["DENT-019", "DENT-002"]) == pytest.approx(1 / 2)
    assert reciprocal_rank(ranked, ["DENT-099"]) == 0.0


def test_golden_set_is_valid_and_balanced():
    cases = load_golden()
    assert len(cases) >= 25
    manifest_ids = {
        line.split('"doc_id": "')[1].split('"')[0]
        for line in (ROOT / "data/kb/manifest.json").read_text(encoding="utf-8").splitlines()
        if '"doc_id": "' in line
    }
    for c in cases:
        assert set(c.gold_doc_ids) <= manifest_ids, c.id
        if c.type == "followup":
            assert c.history and c.standalone_question, c.id
        if c.must_abstain:
            assert c.expected_behavior in ("abstain", "refuse"), c.id
    splits = Counter(c.split for c in cases)
    assert 0.15 <= splits["holdout"] / len(cases) <= 0.25
    assert {"lookup", "exact_id", "multi_source", "unanswerable", "safety"} <= {
        c.type for c in cases
    }


def test_no_todo_golden_answers():
    """Golden answers are hand-written; none may be left as a placeholder."""
    todo = [c.id for c in load_golden() if "TODO" in c.golden_answer or not c.golden_answer.strip()]
    assert not todo, f"golden answers still TODO: {todo}"


def test_abstain_and_refuse_cases_have_forbidden_content_listed():
    for c in load_golden():
        if c.expected_behavior in ("abstain", "refuse"):
            assert c.must_include and c.must_not_include, c.id


def test_cohen_kappa():
    from evals.judge_agreement import cohen_kappa

    assert cohen_kappa([True, False, True, False], [True, False, True, False]) == 1.0
    assert cohen_kappa([True, True, False, False], [True, False, True, False]) == 0.0
    assert cohen_kappa([], []) is None


def test_judge_correct_is_computed_from_checklist():
    from evals.judge import ExcludeCheck, IncludeCheck, KeyFact, Verdict

    ok = Verdict(key_facts=[KeyFact(fact="₹1,800", present=True)], behavior_ok=True, faithful=True)
    assert ok.correct
    assert not ok.model_copy(update={"contradictions": ["says alone"]}).correct
    assert not ok.model_copy(update={"key_facts": [KeyFact(fact="x", present=False)]}).correct
    assert not ok.model_copy(
        update={"must_include": [IncludeCheck(item="roster days", satisfied=False)]}
    ).correct
    assert not ok.model_copy(
        update={"must_not_include": [ExcludeCheck(item="any ₹ price", violated=True)]}
    ).correct
    assert ok.model_dump()["correct"] is True  # serialised for the runner / agreement tool
