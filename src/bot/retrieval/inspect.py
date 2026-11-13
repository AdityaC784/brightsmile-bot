"""Retrieval inspector: see what each search mode returns for a query.

python -m bot.retrieval.inspect "root canal price"
python -m bot.retrieval.inspect "SRV-005A" --mode lexical --k 10
python -m bot.retrieval.inspect "molar RCT cost" --compare      # hybrid vs lexical vs dense
"""

from __future__ import annotations

import argparse
import sys

from bot.retrieval.hybrid import HybridRetriever


def _print(title: str, hits) -> None:
    print(f"\n=== {title}")
    for i, h in enumerate(hits, 1):
        flag = " [ARCHIVED]" if h.status == "archived" else ""
        lr = h.lexical_rank if h.lexical_rank is not None else "-"
        dr = h.dense_rank if h.dense_rank is not None else "-"
        print(f"{i:2}. {h.score:.4f}  lex={lr!s:>3} dense={dr!s:>3}  {h.doc_id}{flag}")
        snippet = h.content.replace("\n", " ")
        print(f"    {snippet[:160]}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # ₹ and em-dashes on Windows consoles
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("query")
    ap.add_argument("--mode", choices=["hybrid", "lexical", "dense"])
    ap.add_argument("--chunker", choices=["heading", "fixed"])
    ap.add_argument("--k", type=int, default=None)
    ap.add_argument("--compare", action="store_true", help="show all three modes side by side")
    a = ap.parse_args()

    if not a.query.strip():
        ap.error('empty query — usage: make search Q="root canal price"')
    r = HybridRetriever()
    modes = ["hybrid", "lexical", "dense"] if a.compare else [a.mode]
    for m in modes:
        hits = r.search(a.query, mode=m, top_k=a.k, chunker=a.chunker)
        _print(f"{m or r.cfg['search']['mode']}  |  {a.query!r}", hits)
    return 0


if __name__ == "__main__":
    sys.exit(main())
