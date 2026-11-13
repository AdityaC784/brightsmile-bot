"""End-to-end eval: run every golden case through the answer pipeline, judge it, report metrics.

    python -m evals.runner --split dev --out evals/runs/latest.json     # make eval
    python -m evals.runner --split all --out evals/runs/pr.json         # make eval-ci
    python -m evals.runner --limit 3                                    # quick smoke run

All LLM calls are cached by prompt hash (LLM_CACHE_DIR), so re-runs of unchanged cases are free
and deterministic. Writes <out>.json (metrics + per-case detail) and <out>.md (report).

Follow-up cases use the hand-written `standalone_question` for retrieval (oracle rewrite) until
the LangGraph rewriter exists; the generator still sees the conversation history.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

# Eval runs always cache LLM calls; must be set before settings are first read.
os.environ.setdefault("LLM_CACHE_ENABLED", "true")

from bot.config import ROOT, get_settings, load_yaml
from bot.llm.base import Message
from bot.prompts import prompt_hash
from bot.rag.answer import Answerer, AnswerResult
from evals.golden_set import GoldenCase, load_golden
from evals.judge import Verdict, judge
from evals.retrieval_eval import recall_at_k, reciprocal_rank

PIPELINE_CONCURRENCY = 1  # Groq free tier: 7k input tokens/min on the generator (~3k/answer)
CASE_TIMEOUT_S = 300  # per step; a stuck call fails its case instead of freezing the run
JUDGE_CONCURRENCY = 1  # Groq gpt-oss-120b free tier: 8k tokens/min, ~5k per judgement


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT
        )
        return out.stdout.strip() or None
    except OSError:
        return None


async def run_case(
    case: GoldenCase, answerer: Answerer, pipe_sem: asyncio.Semaphore, judge_sem: asyncio.Semaphore
) -> dict:
    history = [Message(t.role, t.content) for t in case.history]
    row: dict = {
        "id": case.id,
        "split": case.split,
        "type": case.type,
        "expected_behavior": case.expected_behavior,
        "must_abstain": case.must_abstain,
    }
    try:
        async with pipe_sem:
            result: AnswerResult = await asyncio.wait_for(
                asyncio.to_thread(
                    answerer.answer, case.question, history, query=case.retrieval_query
                ),
                CASE_TIMEOUT_S,
            )
        async with judge_sem:
            verdict: Verdict = await asyncio.wait_for(
                asyncio.to_thread(judge, case, result), CASE_TIMEOUT_S
            )
    except TimeoutError:
        row.update(error=f"timed out after {CASE_TIMEOUT_S}s")
        print(f"  {case.id} ERROR {row['error']}", flush=True)
        return row
    except Exception as e:  # one broken case must not kill the run
        row.update(error=f"{type(e).__name__}: {str(e)[:300]}")
        print(f"  {case.id} ERROR {row['error']}", flush=True)
        return row

    docs = [h.source_doc_id for h in result.hits]
    v = result.verification
    row.update(
        behavior=result.behavior,
        answer=result.answer,
        fallback_reason=result.fallback_reason,
        repaired=result.repaired,
        citations=result.citations,
        retrieved=docs,
        recall_at_5=recall_at_k(docs, case.gold_doc_ids) if case.gold_doc_ids else None,
        rr=reciprocal_rank(docs, case.gold_doc_ids) if case.gold_doc_ids else None,
        claims=v.n_claims if v else 0,
        claims_supported=v.n_supported if v else 0,
        unsupported=[c.reason for c in v.claims if not c.supported] if v else [],
        latency_ms=result.latency_ms,
        cached=result.cached,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        verdict=verdict.model_dump(),
    )
    mark = "PASS" if verdict.correct else "FAIL"
    print(f"  {case.id} {mark} [{result.behavior}] {verdict.reason[:100]}", flush=True)
    return row


def _mean(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def _pct(xs: list[int], q: float) -> int | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(q * (len(xs) - 1)))]


def compute_metrics(rows: list[dict]) -> dict:
    ok = [r for r in rows if "error" not in r]
    # Refusing an off-topic/injection request (expected "refuse", must_abstain false) is correct
    # behaviour, not an abstention on an answerable question, so those cases are excluded.
    scored = [r for r in ok if r["must_abstain"] or r["expected_behavior"] != "refuse"]
    pred_abstain = [r for r in scored if r["behavior"] in ("abstain", "refuse")]
    must = [r for r in scored if r["must_abstain"]]
    tp = sum(1 for r in pred_abstain if r["must_abstain"])
    claims = sum(r["claims"] for r in ok)
    # Cached replies return in ~0 ms; only live calls say anything about latency.
    lat = [r["latency_ms"] for r in ok if not r.get("cached")]
    by_type: dict[str, list[bool]] = defaultdict(list)
    for r in ok:
        by_type[r["type"]].append(r["verdict"]["correct"])
    safety = [r["verdict"]["correct"] for r in ok if r["type"] == "safety"]
    return {
        "n_cases": len(rows),
        "n_errors": len(rows) - len(ok),
        "retrieval": {
            "recall_at_5": _mean([r["recall_at_5"] for r in ok]),
            "mrr": _mean([r["rr"] for r in ok]),
        },
        "answer": {
            "correctness": _mean([r["verdict"]["correct"] for r in ok]),
            "behavior_accuracy": _mean([r["verdict"]["behavior_ok"] for r in ok]),
            "by_type": {t: _mean(v) for t, v in sorted(by_type.items())},
        },
        "grounding": {"faithfulness": _mean([r["verdict"]["faithful"] for r in ok])},
        "citations": {
            "accuracy": round(sum(r["claims_supported"] for r in ok) / claims, 4)
            if claims
            else None,
            "claims": claims,
            "fallbacks": sum(1 for r in ok if r["fallback_reason"]),
            "repairs": sum(1 for r in ok if r.get("repaired")),
        },
        "abstention": {
            "precision": round(tp / len(pred_abstain), 4) if pred_abstain else None,
            "recall": round(tp / len(must), 4) if must else None,
        },
        "safety": {"pass_rate": _mean(safety)},
        "ops": {
            "latency_p50_ms": int(statistics.median(lat)) if lat else None,
            "latency_p95_ms": _pct(lat, 0.95),
            "live_calls": len(lat),
            "tokens_in": sum(r["tokens_in"] for r in ok),
            "tokens_out": sum(r["tokens_out"] for r in ok),
        },
    }


def render_report(metrics: dict, rows: list[dict], meta: dict) -> str:
    m = metrics

    def f(x: float | None) -> str:
        return "-" if x is None else f"{x:.2f}"

    lines = [
        f"# Eval report — {meta['split']} split",
        "",
        f"git `{meta['git_sha']}` · golden {meta['dataset_version']} · kb `{meta['kb_version']}` · "
        f"prompts answer `{meta['prompts']['answer']}` judge `{meta['prompts']['judge']}`",
        "",
        "| metric | value |",
        "|---|---|",
        f"| answer correctness | {f(m['answer']['correctness'])} |",
        f"| behaviour accuracy | {f(m['answer']['behavior_accuracy'])} |",
        f"| faithfulness | {f(m['grounding']['faithfulness'])} |",
        f"| citation accuracy | {f(m['citations']['accuracy'])} ({m['citations']['claims']} claims) |",
        f"| verifier repairs / fallbacks | {m['citations']['repairs']} / "
        f"{m['citations']['fallbacks']} |",
        f"| abstention precision / recall | {f(m['abstention']['precision'])} / "
        f"{f(m['abstention']['recall'])} |",
        f"| safety pass rate | {f(m['safety']['pass_rate'])} |",
        f"| retrieval recall@5 / MRR | {f(m['retrieval']['recall_at_5'])} / {f(m['retrieval']['mrr'])} |",
        f"| latency p50 / p95 | {m['ops']['latency_p50_ms']} / {m['ops']['latency_p95_ms']} ms "
        f"({m['ops']['live_calls']} live, free-tier throttling included) |"
        if m["ops"]["live_calls"]
        else "| latency p50 / p95 | n/a (all replies cached) |",
        f"| errors | {m['n_errors']} / {m['n_cases']} |",
        "",
        "## Correctness by type",
        "",
        "| type | correct |",
        "|---|---|",
        *[f"| {t} | {f(v)} |" for t, v in m["answer"]["by_type"].items()],
        "",
        "## Failed cases",
        "",
    ]
    for r in rows:
        if "error" in r:
            lines.append(f"- **{r['id']}** ERROR: {r['error']}")
        elif not r["verdict"]["correct"]:
            vd = r["verdict"]
            lines.append(
                f"- **{r['id']}** ({r['type']}, expected {r['expected_behavior']}, got "
                f"{r['behavior']}): {vd['reason']}"
                + (f" Missing: {vd['missing']}." if vd["missing"] else "")
                + (f" Violations: {vd['violations']}." if vd["violations"] else "")
                + (f" Fallback: {r['fallback_reason'][:150]}" if r["fallback_reason"] else "")
            )
    return "\n".join(lines) + "\n"


async def main_async(args: argparse.Namespace) -> int:
    cases = load_golden()
    if args.split != "all":
        cases = [c for c in cases if c.split == args.split]
    if args.ids:
        wanted = set(args.ids.split(","))
        cases = [c for c in cases if c.id in wanted]
    if args.limit:
        cases = cases[: args.limit]

    assert get_settings().llm_cache_enabled, "eval runs must cache LLM calls"
    answerer = Answerer()
    pipe_sem, judge_sem = (
        asyncio.Semaphore(PIPELINE_CONCURRENCY),
        asyncio.Semaphore(JUDGE_CONCURRENCY),
    )
    t0 = time.perf_counter()
    print(f"Running {len(cases)} cases ({args.split})...", flush=True)
    rows = await asyncio.gather(*(run_case(c, answerer, pipe_sem, judge_sem) for c in cases))
    rows = sorted(rows, key=lambda r: r["id"])

    from bot.retrieval.ingest import build_chunks  # local: avoids loading PDFs unless needed

    meta = {
        "split": args.split,
        "git_sha": _git_sha(),
        "dataset_version": cases[0].dataset_version if cases else None,
        "kb_version": build_chunks()[0],
        "prompts": {p: prompt_hash(p) for p in ("answer", "verify", "judge")},
        "models": load_yaml("models"),
        "config": {
            "retrieval": load_yaml("retrieval")["search"],
            "generation": load_yaml("generation")["verification"],
        },
        "seconds": round(time.perf_counter() - t0, 1),
    }
    metrics = compute_metrics(rows)
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"meta": meta, "metrics": metrics, "cases": rows}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    report = render_report(metrics, rows, meta)
    out.with_suffix(".md").write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"Wrote {out.relative_to(ROOT)} and .md in {meta['seconds']}s")
    return 1 if metrics["n_errors"] else 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--split", choices=["dev", "holdout", "all"], default="dev")
    ap.add_argument("--out", default="evals/runs/latest.json")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ids", default="", help="comma-separated case ids, e.g. q-001,q-022")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
