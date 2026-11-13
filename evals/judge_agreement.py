"""Validate the LLM judge against your own labels (plan §7: "validate your judge").

1. Export a labelling sheet from an eval run (bot reply + reference side by side):
       python -m evals.judge_agreement export --run evals/runs/latest.json
   -> evals/judge_labels/labels.jsonl  (each row has "human_correct": null)

2. Open the file and set "human_correct" to true/false for each row, by reading the bot reply
   against the reference answer yourself. Do NOT look at the judge's verdict first — it is
   deliberately not in the sheet. Optional: add a "human_note".

3. Measure agreement:
       python -m evals.judge_agreement score --run evals/runs/latest.json
   Reports raw agreement and Cohen's kappa, and lists every disagreement so you can decide
   whether the judge prompt or your label is wrong.

Labels are keyed by (case id, hash of the bot reply): if a later run produces a different
reply, that row needs re-labelling and is reported as stale.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from bot.config import ROOT

LABELS = ROOT / "evals" / "judge_labels" / "labels.jsonl"


def _reply_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _load_run(path: str) -> list[dict]:
    p = Path(path)
    p = p if p.is_absolute() else ROOT / p
    return [r for r in json.loads(p.read_text(encoding="utf-8"))["cases"] if "error" not in r]


def _load_labels() -> dict[str, dict]:
    if not LABELS.exists():
        return {}
    rows = [json.loads(x) for x in LABELS.read_text(encoding="utf-8").splitlines() if x.strip()]
    return {r["id"]: r for r in rows}


def export(run: str) -> int:
    from evals.golden_set import load_golden

    golden = {c.id: c for c in load_golden()}
    existing = _load_labels()
    out = []
    for r in _load_run(run):
        h = _reply_hash(r["answer"])
        old = existing.get(r["id"])
        keep = old if old and old.get("reply_hash") == h else None
        c = golden[r["id"]]
        out.append(
            {
                "id": r["id"],
                "reply_hash": h,
                "question": c.question,
                "expected_behavior": c.expected_behavior,
                "reference_answer": c.golden_answer,
                "must_include": c.must_include,
                "must_not_include": c.must_not_include,
                "bot_reply": r["answer"],
                "human_correct": keep["human_correct"] if keep else None,
                "human_note": keep.get("human_note", "") if keep else "",
                "labeler": keep.get("labeler", "human") if keep else "human",
            }
        )
    LABELS.parent.mkdir(parents=True, exist_ok=True)
    LABELS.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in out) + "\n", "utf-8")
    todo = sum(1 for x in out if x["human_correct"] is None)
    print(f"Wrote {len(out)} rows to {LABELS.relative_to(ROOT)} ({todo} need a label).")
    return 0


def cohen_kappa(a: list[bool], b: list[bool]) -> float | None:
    n = len(a)
    if n == 0:
        return None
    po = sum(x == y for x, y in zip(a, b, strict=True)) / n
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def score(run: str) -> int:
    labels = _load_labels()
    pairs, stale, unlabelled = [], [], []
    for r in _load_run(run):
        lab = labels.get(r["id"])
        if not lab or lab.get("human_correct") is None:
            unlabelled.append(r["id"])
        elif lab["reply_hash"] != _reply_hash(r["answer"]):
            stale.append(r["id"])
        else:
            pairs.append((r["id"], bool(lab["human_correct"]), bool(r["verdict"]["correct"]), r))
    if not pairs:
        print("No usable labels yet. Run `export`, fill in human_correct, then `score`.")
        return 1
    human = [h for _, h, _, _ in pairs]
    judge = [j for _, _, j, _ in pairs]
    agree = sum(h == j for h, j in zip(human, judge, strict=True))
    kappa = cohen_kappa(human, judge)
    labelers = sorted({labels[i].get("labeler", "human") for i, _, _, _ in pairs})
    print(f"Judge vs labels ({', '.join(labelers)}) on {len(pairs)} replies:")
    if labelers != ["human"]:
        print("  NOTE: not all labels are human; this is not a human-validated judge.")
    print(f"  agreement      {agree}/{len(pairs)} = {agree / len(pairs):.2f}")
    print(f"  Cohen's kappa  {kappa:.2f}" if kappa is not None else "  Cohen's kappa  -")
    print(
        f"  human pass rate {sum(human) / len(human):.2f}  judge pass rate {sum(judge) / len(judge):.2f}"
    )
    dis = [(i, h, j, r) for i, h, j, r in pairs if h != j]
    if dis:
        print("\nDisagreements:")
        for i, h, j, r in dis:
            print(f"  {i}: human={h} judge={j}  judge reason: {r['verdict']['reason']}")
    if stale:
        print(f"\nStale labels (reply changed since labelling, re-export): {stale}")
    if unlabelled:
        print(f"Unlabelled: {unlabelled}")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("command", choices=["export", "score"])
    ap.add_argument("--run", default="evals/runs/latest.json")
    a = ap.parse_args()
    return export(a.run) if a.command == "export" else score(a.run)


if __name__ == "__main__":
    sys.exit(main())
