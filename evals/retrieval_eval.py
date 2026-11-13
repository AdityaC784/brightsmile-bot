"""Retrieval eval: recall@k and MRR for every search mode x chunker (plan §7, experiment 1).

    python -m evals.retrieval_eval                  # dev split (what you tune on)
    python -m evals.retrieval_eval --split all      # dev and holdout reported separately
    python -m evals.retrieval_eval --show-misses    # list dev cases hybrid failed on

Only cases with gold_doc_ids are scored. Matching is at document level (DENT-xxx), so the same
golden set scores both chunkers. No LLM calls: only query embeddings (cached on disk).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

from bot.config import ROOT, load_yaml
from bot.retrieval.hybrid import HybridRetriever
from evals.golden_set import GoldenCase, load_golden

MODES = ("hybrid", "lexical", "dense")
CHUNKERS = ("heading", "fixed")
K = 5
MRR_DEPTH = 10


def recall_at_k(ranked_docs: list[str], gold: list[str], k: int = K) -> float:
    """Fraction of gold documents present among the top-k retrieved chunks."""
    top = set(ranked_docs[:k])
    return sum(1 for g in gold if g in top) / len(gold)


def reciprocal_rank(ranked_docs: list[str], gold: list[str], depth: int = MRR_DEPTH) -> float:
    """1 / rank of the first chunk from any gold document (0 if none in the top `depth`)."""
    for i, d in enumerate(ranked_docs[:depth], 1):
        if d in gold:
            return 1.0 / i
    return 0.0


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def run(cases: list[GoldenCase]) -> dict:
    retriever = HybridRetriever()
    scored = [c for c in cases if c.gold_doc_ids]
    per_case: list[dict] = []
    for c in scored:
        for chunker in CHUNKERS:
            for mode in MODES:
                hits = retriever.search(
                    c.retrieval_query, mode=mode, chunker=chunker, top_k=MRR_DEPTH
                )
                docs = [h.source_doc_id for h in hits]
                per_case.append(
                    {
                        "id": c.id,
                        "split": c.split,
                        "type": c.type,
                        "chunker": chunker,
                        "mode": mode,
                        "gold": c.gold_doc_ids,
                        "retrieved": docs,
                        "recall_at_5": recall_at_k(docs, c.gold_doc_ids),
                        "rr": reciprocal_rank(docs, c.gold_doc_ids),
                    }
                )
    return {"per_case": per_case, "n_scored": len(scored), "n_total": len(cases)}


def summarize(per_case: list[dict], split: str) -> dict:
    rows = [r for r in per_case if r["split"] == split]
    out: dict = {"overall": {}, "by_type": defaultdict(dict)}
    for chunker in CHUNKERS:
        for mode in MODES:
            sel = [r for r in rows if r["chunker"] == chunker and r["mode"] == mode]
            key = f"{chunker}/{mode}"
            out["overall"][key] = {
                "recall_at_5": _mean([r["recall_at_5"] for r in sel]),
                "mrr": _mean([r["rr"] for r in sel]),
                "n": len(sel),
            }
            types = sorted({r["type"] for r in sel})
            for t in types:
                ts = [r for r in sel if r["type"] == t]
                out["by_type"][t][key] = {
                    "recall_at_5": _mean([r["recall_at_5"] for r in ts]),
                    "n": len(ts),
                }
    out["by_type"] = dict(out["by_type"])
    return out


def _print_tables(summary: dict, split: str) -> None:
    ov = summary["overall"]
    n = next(iter(ov.values()))["n"]
    print(f"\n## Retrieval — {split} split ({n} scored cases), recall@{K} / MRR@{MRR_DEPTH}\n")
    print("| chunker | " + " | ".join(MODES) + " |")
    print("|---|" + "---|" * len(MODES))
    for chunker in CHUNKERS:
        cells = [
            f"{ov[f'{chunker}/{m}']['recall_at_5']:.2f} / {ov[f'{chunker}/{m}']['mrr']:.2f}"
            for m in MODES
        ]
        print(f"| {chunker} | " + " | ".join(cells) + " |")

    print(f"\n### recall@{K} by question type (heading chunker)\n")
    print("| type | n | " + " | ".join(MODES) + " |")
    print("|---|---|" + "---|" * len(MODES))
    for t, d in summary["by_type"].items():
        nt = d[f"heading/{MODES[0]}"]["n"]
        cells = [f"{d[f'heading/{m}']['recall_at_5']:.2f}" for m in MODES]
        print(f"| {t} | {nt} | " + " | ".join(cells) + " |")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--split", choices=["dev", "holdout", "all"], default="dev")
    ap.add_argument("--out", default="evals/runs/retrieval_latest.json")
    ap.add_argument("--show-misses", action="store_true")
    a = ap.parse_args()

    cases = load_golden()
    if a.split != "all":
        cases = [c for c in cases if c.split == a.split]
    t0 = time.perf_counter()
    result = run(cases)
    splits = ["dev", "holdout"] if a.split == "all" else [a.split]
    summaries = {s: summarize(result["per_case"], s) for s in splits}

    for s in splits:
        _print_tables(summaries[s], s)

    if a.show_misses:
        print(f"\n### dev cases where heading/hybrid recall@{K} < 1\n")
        for r in result["per_case"]:
            if (
                r["split"] == "dev"
                and r["chunker"] == "heading"
                and r["mode"] == "hybrid"
                and r["recall_at_5"] < 1
            ):
                print(f"- {r['id']} ({r['type']}): gold={r['gold']} top5={r['retrieved'][:5]}")

    out = Path(a.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "config": {
            "search": load_yaml("retrieval")["search"],
            "embeddings": load_yaml("models")["embeddings"],
        },
        "summaries": summaries,
        **result,
    }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(
        f"\n{result['n_scored']}/{result['n_total']} cases scored in "
        f"{time.perf_counter() - t0:.1f}s -> {out.relative_to(ROOT)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
