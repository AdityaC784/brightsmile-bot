import datetime as dt
import json
import shutil

from evals.golden_set import GOLDEN_PATH, load_golden
from evals.review_cli import (
    CHANGELOG_PATH,
    add_changelog_entry,
    committed_cases,
    doc_ids,
    next_case_id,
    next_version,
    review,
)


class FakeStore:
    def __init__(self, cands):
        self.cands = cands
        self.marked = []

    def load(self, include_deferred):
        return [c for c in self.cands if c["status"] == "pending" or include_deferred]

    def mark(self, cid, status, reviewer_answer=None):
        self.marked.append((cid, status, reviewer_answer))


def _cand(cid, status="pending"):
    return {
        "id": cid,
        "source": "thumbs_down",
        "question": f"Question {cid}?",
        "bot_answer": "I'm not sure.",
        "retrieved_ids": ["DENT-019#root-canal", "DENT-019#crown", "DENT-007"],
        "status": status,
        "created_at": dt.datetime(2026, 11, 16, 10, 0),
    }


def _inputs(*answers):
    it = iter(answers)
    return lambda prompt: next(it)


def _copies(tmp_path):
    g, c = tmp_path / "v1.jsonl", tmp_path / "CHANGELOG.md"
    shutil.copy(GOLDEN_PATH, g)
    shutil.copy(CHANGELOG_PATH, c)
    return g, c


def test_helpers():
    cases = load_golden()
    assert next_case_id(cases) == f"q-{len(cases) + 1:03d}"
    major, minor, _ = max(tuple(map(int, c.dataset_version.split("."))) for c in cases)
    assert next_version(cases) == f"{major}.{minor + 1}.0"
    assert doc_ids(["DENT-019#a", "DENT-019#b", "DENT-007", "junk"]) == ["DENT-019", "DENT-007"]


def test_changelog_creates_section_then_appends(tmp_path):
    p = tmp_path / "CHANGELOG.md"
    p.write_text("# Golden set changelog\n\nIntro.\n\n## 0.6.1 — 2026-11-14\n- old\n", "utf-8")
    add_changelog_entry("0.7.0", "- first", p)
    add_changelog_entry("0.7.0", "- second", p)
    text = p.read_text("utf-8")
    assert text.index("## 0.7.0") < text.index("- first") < text.index("- second")
    assert text.index("- second") < text.index("## 0.6.1")


def test_review_accept_reject_defer(tmp_path):
    golden, changelog = _copies(tmp_path)
    before = load_golden(golden)
    store = FakeStore([_cand(1), _cand(2), _cand(3), _cand(4), _cand(5, "deferred")])
    answers = _inputs(
        "a",  # candidate 1: accept
        "We do not offer that.",  # answer
        "abstain",  # behaviour
        "",  # type -> default unanswerable
        "DENT-019, DENT-007",  # gold docs
        "says it is not offered; suggests calling",  # must include
        "",  # must NOT include left empty -> asked again (required for abstain)
        "says it is not offered; suggests calling",  # must include
        "a price",  # must NOT include
        "",  # split -> dev
        "r",  # candidate 2: reject
        "d",  # candidate 3: defer
        "q",  # quit before candidate 4
    )
    counts = review(store, input_fn=answers, golden_path=golden, changelog_path=changelog)

    assert counts == {"accepted": 1, "rejected": 1, "deferred": 1, "skipped": 0}
    assert store.marked == [
        (1, "accepted", "We do not offer that."),
        (2, "rejected", None),
        (3, "deferred", None),
    ]
    after = load_golden(golden)  # still valid
    assert len(after) == len(before) + 1
    new = after[-1]
    assert new.id == next_case_id(before)
    assert new.dataset_version == next_version(before)
    assert (new.expected_behavior, new.type, new.split) == ("abstain", "unanswerable", "dev")
    assert new.must_abstain and new.gold_doc_ids == ["DENT-019", "DENT-007"]
    assert new.must_include == ["says it is not offered", "suggests calling"]
    assert new.must_not_include == ["a price"]
    assert json.loads(golden.read_text("utf-8").splitlines()[-1])["question"] == "Question 1?"
    assert f"- {new.id} (dev, unanswerable): from review candidate #1" in changelog.read_text(
        "utf-8"
    )


def test_review_empty_answer_cancels_and_writes_nothing(tmp_path):
    golden, changelog = _copies(tmp_path)
    original = golden.read_text("utf-8")
    store = FakeStore([_cand(1)])
    counts = review(store, input_fn=_inputs("a", ""), golden_path=golden, changelog_path=changelog)
    assert counts["skipped"] == 1 and store.marked == []
    assert golden.read_text("utf-8") == original


def test_version_comes_from_main(tmp_path, monkeypatch):
    # Every case added on a branch shares one version: it is based on the golden set on main.
    # git is faked: CI checkouts are shallow and have no origin/main ref.
    import subprocess

    first = GOLDEN_PATH.read_text("utf-8").splitlines()[0]
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=first + "\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cases = committed_cases(GOLDEN_PATH)
    assert [c.id for c in cases] == ["q-001"]
    assert calls[0][:2] == ["git", "show"] and calls[0][2].startswith("origin/main:")
    assert committed_cases(tmp_path / "v1.jsonl") is None  # outside the repo -> fall back

    def failing_run(cmd, **kw):
        raise subprocess.CalledProcessError(128, cmd)  # e.g. no origin/main ref

    monkeypatch.setattr(subprocess, "run", failing_run)
    assert committed_cases(GOLDEN_PATH) is None
