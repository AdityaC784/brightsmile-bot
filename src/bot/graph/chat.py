"""Chat with the bot in the terminal (full graph, state saved in Postgres per thread).

python -m bot.graph.chat                 # new conversation
python -m bot.graph.chat --thread demo1  # resume a conversation
python -m bot.graph.chat --debug         # show intent, rewritten query, citations
"""

from __future__ import annotations

import argparse
import sys
import uuid

from bot.graph.builder import build_graph, new_turn, open_checkpointer


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--thread", default=None)
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    thread = a.thread or f"cli-{uuid.uuid4().hex[:8]}"
    cfg = {"configurable": {"thread_id": thread}}

    with open_checkpointer() as cp:
        graph = build_graph(checkpointer=cp)
        print(f"BrightSmile assistant — thread {thread}. Ctrl+C or 'exit' to quit.\n")
        while True:
            try:
                text = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not text:
                continue
            if text.lower() in {"exit", "quit"}:
                return 0
            out = graph.invoke(new_turn(thread, text), cfg)
            reply = out.get("reply", {})
            print(f"bot> {reply.get('answer', '')}\n")
            if a.debug:
                print(
                    f"     [intent={out.get('intent')} behavior={reply.get('behavior')} "
                    f"query={out.get('standalone')!r} citations={reply.get('citations')}]\n"
                )


if __name__ == "__main__":
    sys.exit(main())
