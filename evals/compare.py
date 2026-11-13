"""CI eval gate: compare a candidate eval run against the committed baseline (plan §8).

    python -m evals.compare --baseline evals/baseline.json --candidate evals/runs/pr.json \
        --comment-out comment.md            # write the PR comment
    python -m evals.compare ... --enforce   # exit 1 if the gate fails (blocks the merge)

Rules come from configs/gate.yaml:
  hard fails  consent violations > max, safety pass rate < min, abstention recall < min,
              any errored case (metrics would be unreliable)
  soft fail   a headline metric drops more than `max_drop_points` percentage points
Also lists the cases that flipped pass -> fail. Metrics that a run doesn't report yet (e.g.
consent violations before the lead eval exists) are shown as "not measured", never as passing.

Noise measurement: compare two runs of the *same* system (`--no-cache` twice); the reported
deltas are the run-to-run noise, which `max_drop_points` must exceed.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from bot.config import ROOT, load_yaml


def _get(d: dict, dotted: str):
    for part in dotted.split("."):
        if not isinstance(d, dict) or part not in d:
            return None
        d = d[part]
    return d


def _load(path: str) -> dict:
    p = Path(path)
    p = p if p.is_absolute() else ROOT / p
    return json.loads(p.read_text(encoding="utf-8"))


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


@dataclass
class GateResult:
    hard_failures: list[str] = field(default_factory=list)
    soft_failures: list[str] = field(default_factory=list)
    not_measured: list[str] = field(default_factory=list)
    deltas: list[tuple[str, float | None, float | None]] = field(default_factory=list)
    flipped_to_fail: list[str] = field(default_factory=list)
    flipped_to_pass: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.hard_failures and not self.soft_failures


def evaluate(baseline: dict, candidate: dict, gate: dict) -> GateResult:
    r = GateResult()
    cm, bm = candidate["metrics"], baseline["metrics"]
    hard = gate["hard_fail"]

    if cm.get("n_errors"):
        r.hard_failures.append(f"{cm['n_errors']} case(s) errored — metrics are unreliable")

    consent = _get(cm, "leads.consent_violations")
    if consent is None:
        r.not_measured.append("consent violations (lead eval not run)")
    elif consent > hard["consent_violations_max"]:
        r.hard_failures.append(
            f"consent violations: {consent} (max {hard['consent_violations_max']})"
        )

    safety = _get(cm, "safety.pass_rate")
    if safety is None:
        r.not_measured.append("safety pass rate (no safety cases in this run)")
    elif safety < hard["safety_pass_rate_min"]:
        r.hard_failures.append(
            f"safety pass rate {_pct(safety)} < {_pct(hard['safety_pass_rate_min'])}"
        )

    recall = _get(cm, "abstention.recall")
    if recall is None:
        r.not_measured.append("abstention recall (no must-abstain cases in this run)")
    elif recall < hard["abstention_recall_min"]:
        r.hard_failures.append(
            f"abstention recall {_pct(recall)} < {_pct(hard['abstention_recall_min'])}"
        )

    soft = gate["soft_fail"]
    max_drop = soft["max_drop_points"] / 100
    for metric in soft["metrics"]:
        b, c = _get(bm, metric), _get(cm, metric)
        r.deltas.append((metric, b, c))
        if b is not None and c is not None and (b - c) > max_drop + 1e-9:
            r.soft_failures.append(
                f"{metric} dropped {(b - c) * 100:.1f} points ({_pct(b)} → {_pct(c)}), "
                f"limit {soft['max_drop_points']}"
            )

    def verdicts(run: dict) -> dict[str, bool]:
        return {c["id"]: bool(c["verdict"]["correct"]) for c in run["cases"] if "verdict" in c}

    bv, cv = verdicts(baseline), verdicts(candidate)
    r.flipped_to_fail = sorted(i for i in bv.keys() & cv.keys() if bv[i] and not cv[i])
    r.flipped_to_pass = sorted(i for i in bv.keys() & cv.keys() if not bv[i] and cv[i])
    return r


def render_comment(r: GateResult, baseline: dict, candidate: dict, gate: dict) -> str:
    status = "✅ **Eval gate passed**" if r.passed else "❌ **Eval gate failed**"
    lines = [status, ""]
    for f in r.hard_failures:
        lines.append(f"- 🛑 hard fail: {f}")
    for f in r.soft_failures:
        lines.append(f"- ⚠️ regression: {f}")
    for n in r.not_measured:
        lines.append(f"- 📝 not measured: {n}")
    lines += ["", "| metric | baseline | candidate | Δ |", "|---|---|---|---|"]
    for metric, b, c in r.deltas:
        delta = "n/a" if b is None or c is None else f"{(c - b) * 100:+.1f}"
        lines.append(f"| {metric} | {_pct(b)} | {_pct(c)} | {delta} |")
    if gate.get("report", {}).get("list_flipped_cases", True):
        cases = {c["id"]: c for c in candidate["cases"]}
        lines += ["", f"**Flipped pass → fail ({len(r.flipped_to_fail)})**"]
        for cid in r.flipped_to_fail:
            reason = cases.get(cid, {}).get("verdict", {}).get("reason", "")
            lines.append(f"- `{cid}`: {reason}")
        if r.flipped_to_pass:
            lines.append(f"\nFlipped fail → pass: {', '.join(f'`{c}`' for c in r.flipped_to_pass)}")
    meta_b, meta_c = baseline.get("meta", {}), candidate.get("meta", {})
    lines += [
        "",
        f"<sub>baseline `{meta_b.get('git_sha')}` · candidate `{meta_c.get('git_sha')}` · "
        f"golden {meta_c.get('dataset_version')} · soft-fail limit "
        f"{gate['soft_fail']['max_drop_points']} points</sub>",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--baseline", default="evals/baseline.json")
    ap.add_argument("--candidate", default="evals/runs/pr.json")
    ap.add_argument("--comment-out", default=None)
    ap.add_argument("--enforce", action="store_true", help="exit 1 if the gate fails")
    a = ap.parse_args()

    baseline, candidate, gate = _load(a.baseline), _load(a.candidate), load_yaml("gate")
    result = evaluate(baseline, candidate, gate)
    comment = render_comment(result, baseline, candidate, gate)
    print(comment)
    if a.comment_out:
        Path(a.comment_out).write_text(comment, encoding="utf-8")
    return 1 if a.enforce and not result.passed else 0


if __name__ == "__main__":
    sys.exit(main())
