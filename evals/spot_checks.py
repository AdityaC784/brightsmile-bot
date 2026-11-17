"""Spot checks with the real LLMs for two parts the main eval doesn't cover.

    uv run python -m evals.spot_checks rewrite   # real follow-up rewriter vs hand-written queries
    uv run python -m evals.spot_checks consent   # hesitant replies must not count as consent

rewrite: for each follow-up golden case, retrieval (production settings) with the patient's raw
message, the real rewriter's output, and the hand-written standalone question; recall@5 / RR.
consent: the lead extractor reads replies to the consent text; hesitant replies must never be
"yes" (a "yes" plus a name and phone saves a lead), and clear agreements should be.
Measurements only: nothing here changes the bot or the CI gate.
"""

from __future__ import annotations

import sys
import time

from bot.config import load_yaml
from bot.graph.nodes import _LeadExtract, _Standalone
from bot.llm.base import Message
from bot.llm.client import get_llm
from bot.prompts import load_prompt
from bot.retrieval.hybrid import HybridRetriever
from evals.golden_set import load_golden
from evals.retrieval_eval import MRR_DEPTH, recall_at_k, reciprocal_rank


def rewrite_check() -> None:
    llm, retriever = get_llm("utility"), HybridRetriever()
    print("| case | split | query | recall@5 | RR | query text |\n|---|---|---|---|---|---|")
    for c in (c for c in load_golden() if c.type == "followup"):
        transcript = "\n".join(f"{t.role}: {t.content}" for t in c.history)
        prompt = f"Recent conversation:\n{transcript}\n\nLatest patient message:\n{c.question}"
        parsed, _ = llm.chat_json(load_prompt("rewrite"), [Message("user", prompt)], _Standalone)
        for label, q in (
            ("raw", c.question),
            ("rewriter", parsed.standalone.strip() or c.question),
            ("hand-written", c.standalone_question),
        ):
            docs = [h.source_doc_id for h in retriever.search(q, top_k=MRR_DEPTH)]
            print(
                f"| {c.id} | {c.split} | {label} | {recall_at_k(docs, c.gold_doc_ids):.2f} "
                f"| {reciprocal_rank(docs, c.gold_doc_ids):.2f} | {q} |"
            )

    # Harder follow-ups (not golden cases): the message alone has lost its topic.
    print("\n| conversation | query | top document | right? |\n|---|---|---|---|")
    for first, follow, want in HARD_FOLLOWUPS:
        prompt = f"Recent conversation:\nuser: {first}\n\nLatest patient message:\n{follow}"
        time.sleep(3)
        parsed, _ = llm.chat_json(load_prompt("rewrite"), [Message("user", prompt)], _Standalone)
        for label, q in (("raw", follow), ("rewriter", parsed.standalone.strip() or follow)):
            top = retriever.search(q, top_k=1)[0].source_doc_id
            print(
                f"| {first} -> {follow} | {label}: {q} | {top} | {'yes' if top == want else 'no'} |"
            )


HARD_FOLLOWUPS = [  # (first message, follow-up, document that answers it)
    ("How much does a root canal cost?", "And for kids?", "DENT-014"),
    ("Do you offer teeth whitening?", "How long does it last?", "DENT-018"),
    ("What are the cancellation rules?", "What if I miss it completely?", "DENT-007"),
    ("Tell me about clear aligners.", "Is it painful?", "DENT-017"),
]


HESITANT = [
    "maybe later",
    "why do you need my number?",
    "not now, I'm just looking",
    "I'll think about it",
    "how much does it cost first?",
    "no thanks",
    "can I just book online instead?",
    "is my data safe with you?",
    "my name is Test Patient but I don't want a call",
]
AGREES = [
    "yes please",
    "sure, Test Patient, 90000 00000",
    "ok, call me on 9000000000",
    "Test Patient, +91 90000 00000",  # details in reply to the consent question = agreement
]


def consent_check() -> int:
    llm = get_llm("utility")
    consent = load_yaml("generation")["lead"]["consent_text"].strip()
    false_yes = 0
    print("| reply | expected | extracted consent | ok |\n|---|---|---|---|")
    for reply, expected in [(r, "not yes") for r in HESITANT] + [(r, "yes") for r in AGREES]:
        prompt = (
            "Recent conversation:\nuser: I'd like teeth whitening\n"
            f"assistant: {consent}\n\nLatest patient message:\n{reply}"
        )
        time.sleep(8)  # free tier: 8k tokens/minute on the utility model, ~800 per call
        ex, _ = llm.chat_json(load_prompt("lead_extract"), [Message("user", prompt)], _LeadExtract)
        ok = (ex.consent == "yes") == (expected == "yes")
        false_yes += expected != "yes" and ex.consent == "yes"
        print(f"| {reply} | {expected} | {ex.consent} | {'ok' if ok else 'WRONG'} |")
    print(f"\nhesitant replies read as consent: {false_yes} / {len(HESITANT)}")
    return 1 if false_yes else 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "rewrite":
        rewrite_check()
    elif cmd == "consent":
        sys.exit(consent_check())
    else:
        sys.exit(__doc__)
