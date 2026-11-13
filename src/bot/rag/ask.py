"""Ask the bot one question from the terminal (full pipeline, no conversation state).

python -m bot.rag.ask "How much is a molar root canal?"
python -m bot.rag.ask "What does a crown cost?" --debug
"""

from __future__ import annotations

import argparse
import sys

from bot.rag.answer import Answerer


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--debug", action="store_true", help="show sources and verification")
    a = ap.parse_args()

    r = Answerer().answer(a.question)
    print(f"\n[{r.behavior}] {r.answer}\n")
    conf = f"{r.confidence:.2f}" if r.confidence is not None else "-"
    print(f"citations={r.citations}  confidence={conf}  latency={r.latency_ms} ms  {r.timings}")
    if r.fallback_reason:
        print(f"fallback: {r.fallback_reason}")
    if a.debug:
        print("\n--- model output (before fallback/rewrite) ---\n" + r.raw_answer)
        print("\n--- sources ---")
        for i, h in enumerate(r.hits, 1):
            print(f"S{i} {h.score:.4f} {h.doc_id}{' [ARCHIVED]' if h.status == 'archived' else ''}")
        if r.verification:
            print("\n--- verification ---")
            for c in r.verification.claims:
                print(
                    f"{'OK ' if c.supported else 'BAD'} {c.source_ids} {c.text[:110]}  ({c.reason})"
                )
            for u in r.verification.uncited:
                print(f"UNCITED {u[:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
