"""One-off cross-check of our eval against RAGAS (faithfulness, context recall).

    uv run --group ragas python -m evals.ragas_check

Takes 12 dev "answer" cases from the committed baseline (all our judge's failures + the first
passes), re-fetches exactly the sources the generator saw (search + reference expansion;
retrieval is deterministic and embeddings are cached),
and scores the baseline answers with RAGAS on the same model as our judge (gpt-oss-120b on
Groq, via its OpenAI-compatible endpoint). Prints both sides per case. Not part of the CI gate;
the `ragas` dependency group is installed by neither CI nor Docker.
"""

from __future__ import annotations

import json
import re
import time

from openai import OpenAI
from ragas.llms import llm_factory
from ragas.metrics import collections as rm

from bot.config import ROOT, get_settings
from bot.retrieval.hybrid import HybridRetriever
from evals.golden_set import load_golden

N_FAIL, N_PASS, PAUSE_S = 6, 6, 40  # pause: free tier, 8k tokens/min on the judge model
CITE = re.compile(r"\s*\[S\d+\]")


def main() -> None:
    golden = {c.id: c for c in load_golden()}
    rows = json.loads((ROOT / "evals" / "baseline.json").read_text(encoding="utf-8"))["cases"]
    pool = [r for r in rows if r["split"] == "dev" and r["expected_behavior"] == "answer"]
    picked = [r for r in pool if not r["verdict"]["correct"]][:N_FAIL]
    picked += [r for r in pool if r["verdict"]["correct"]][:N_PASS]

    client = OpenAI(
        api_key=get_settings().groq_api_key.strip(), base_url="https://api.groq.com/openai/v1"
    )
    # Same settings as our judge (configs/models.yaml); with library defaults gpt-oss spends its
    # token budget on reasoning and returns empty JSON.
    llm = llm_factory(
        "openai/gpt-oss-120b",
        provider="openai",
        client=client,
        max_tokens=4096,
        temperature=0,
        reasoning_effort="medium",
    )
    faith, recall, retriever = (
        rm.Faithfulness(llm=llm),
        rm.ContextRecall(llm=llm),
        HybridRetriever(),
    )

    out = []
    print(
        "| case | our correct | our faithful | RAGAS faithfulness | our recall@5 "
        "| RAGAS context recall |\n|---|---|---|---|---|---|"
    )
    for i, r in enumerate(picked):
        if i:
            time.sleep(PAUSE_S)
        case = golden[r["id"]]
        # Exactly the sources the generator saw (src/bot/rag/answer.py): search + references.
        hits = retriever.expand_references(retriever.search(case.retrieval_query))
        contexts = [h.content for h in hits]
        answer = CITE.sub("", r["answer"])
        f = faith.score(user_input=case.question, response=answer, retrieved_contexts=contexts)
        time.sleep(PAUSE_S)
        c = recall.score(
            user_input=case.question, retrieved_contexts=contexts, reference=case.golden_answer
        )
        row = {
            "id": r["id"],
            "our_correct": r["verdict"]["correct"],
            "our_faithful": r["verdict"]["faithful"],
            "ragas_faithfulness": round(f.value, 2),
            "our_recall_at_5": r["recall_at_5"],
            "ragas_context_recall": round(c.value, 2),
        }
        out.append(row)
        print(
            f"| {row['id']} | {row['our_correct']} | {row['our_faithful']} "
            f"| {row['ragas_faithfulness']:.2f} | {row['our_recall_at_5']:.2f} "
            f"| {row['ragas_context_recall']:.2f} |",
            flush=True,
        )

    path = ROOT / "evals" / "runs" / "ragas_check.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    def mean(xs: list[float]) -> float:
        return round(sum(xs) / len(xs), 2) if xs else float("nan")

    for label, ok in (("our judge: correct", True), ("our judge: incorrect", False)):
        grp = [o for o in out if o["our_correct"] is ok]
        print(
            f"\n{label} (n={len(grp)}): RAGAS faithfulness "
            f"{mean([o['ragas_faithfulness'] for o in grp])}, context recall "
            f"{mean([o['ragas_context_recall'] for o in grp])}"
        )
    print(f"\nwritten {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
