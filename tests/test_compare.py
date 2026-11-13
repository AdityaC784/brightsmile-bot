"""CI gate logic (evals/compare.py) on hand-built run files."""

import copy

from evals.compare import evaluate, render_comment

from bot.config import load_yaml


def _run(correctness=0.9, faith=1.0, safety=1.0, recall=1.0, errors=0, verdicts=None):
    verdicts = verdicts or {"q-1": True, "q-2": True, "q-3": False}
    return {
        "meta": {"git_sha": "abc", "dataset_version": "0.5.0"},
        "metrics": {
            "n_errors": errors,
            "retrieval": {"recall_at_5": 0.9, "mrr": 0.8},
            "answer": {"correctness": correctness},
            "grounding": {"faithfulness": faith},
            "citations": {"accuracy": 1.0},
            "abstention": {"recall": recall},
            "safety": {"pass_rate": safety},
        },
        "cases": [
            {"id": i, "verdict": {"correct": ok, "reason": f"reason {i}"}}
            for i, ok in verdicts.items()
        ],
    }


GATE = load_yaml("gate")


def test_identical_runs_pass_but_consent_is_not_measured():
    r = evaluate(_run(), _run(), GATE)
    assert r.passed and r.flipped_to_fail == []
    assert any("consent" in n for n in r.not_measured)


def test_hard_fails():
    assert not evaluate(_run(), _run(safety=0.75), GATE).passed
    assert not evaluate(_run(), _run(recall=0.5), GATE).passed
    assert not evaluate(_run(), _run(errors=2), GATE).passed
    bad = _run()
    bad["metrics"]["leads"] = {"consent_violations": 1}
    assert any("consent" in f for f in evaluate(_run(), bad, GATE).hard_failures)


def test_soft_fail_only_beyond_limit():
    limit = GATE["soft_fail"]["max_drop_points"] / 100
    within = evaluate(_run(correctness=0.9), _run(correctness=0.9 - limit + 0.01), GATE)
    assert within.passed
    beyond = evaluate(_run(correctness=0.9), _run(correctness=0.9 - limit - 0.02), GATE)
    assert not beyond.passed and "answer.correctness" in beyond.soft_failures[0]


def test_flipped_cases_reported_in_comment():
    base = _run(verdicts={"q-1": True, "q-2": True, "q-3": False})
    cand = _run(verdicts={"q-1": True, "q-2": False, "q-3": True})
    r = evaluate(base, cand, GATE)
    assert r.flipped_to_fail == ["q-2"] and r.flipped_to_pass == ["q-3"]
    md = render_comment(r, base, cand, GATE)
    assert "`q-2`: reason q-2" in md and "answer.correctness" in md


def test_missing_metric_is_not_treated_as_passing():
    cand = copy.deepcopy(_run())
    del cand["metrics"]["safety"]
    r = evaluate(_run(), cand, GATE)
    assert any("safety" in n for n in r.not_measured)
