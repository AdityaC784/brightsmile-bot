"""Citation verification.

Two layers, cheapest first:
1. Deterministic: every ₹ amount and clinic identifier (SRV-005A, PKG-002, REF-04, ...) in a
   cited sentence must literally appear in the text of the sources it cites. Catches invented
   prices/codes with zero LLM calls.
2. LLM check (utility model): is each cited sentence fully supported by its cited sources?

A claim is any answer sentence that carries at least one [S#] citation. Sentences that contain
prices or identifiers but no citation are reported as `uncited`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel

from bot.llm.base import Message
from bot.llm.client import LLM, get_llm
from bot.prompts import load_prompt

CITE = re.compile(r"\[(S\d+)\]")
# Prices like ₹1,800 / ₹ 6,800 / INR 500 and clinic identifiers like SRV-005A, ESC-01, FRM-PED-03.
MONEY = re.compile(r"(?:₹|INR|Rs\.?)\s?([\d,]+(?:\.\d+)?)", re.IGNORECASE)
IDENT = re.compile(r"\b(?:SRV|PKG|REF|POL|ESC|WLK|FRM|PAY|STF|EQP|DENT)-[A-Z0-9-]*\d[A-Z0-9]*\b")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(₹*-])|\n+")


@dataclass
class Claim:
    text: str
    source_ids: list[str]
    supported: bool | None = None
    reason: str = ""


@dataclass
class Verification:
    claims: list[Claim] = field(default_factory=list)
    uncited: list[str] = field(default_factory=list)
    llm_checked: bool = False

    @property
    def n_claims(self) -> int:
        return len(self.claims)

    @property
    def n_supported(self) -> int:
        return sum(1 for c in self.claims if c.supported)

    @property
    def support_ratio(self) -> float | None:
        return self.n_supported / self.n_claims if self.claims else None


def split_claims(answer: str) -> tuple[list[Claim], list[str]]:
    claims: list[Claim] = []
    uncited: list[str] = []
    for raw in _SENT_SPLIT.split(answer):
        sent = raw.strip(" -*•\t")
        if not sent:
            continue
        ids = list(dict.fromkeys(CITE.findall(sent)))
        if ids:
            claims.append(Claim(text=sent, source_ids=ids))
        elif MONEY.search(sent) or IDENT.search(sent):
            uncited.append(sent)
    return claims, uncited


def _norm_money(s: str) -> str:
    return s.replace(",", "")


def facts_in_text(claim: str, source_text: str) -> tuple[bool, str]:
    """Every ₹ amount and identifier in the claim must occur in the cited source text."""
    src_money = {_norm_money(m) for m in MONEY.findall(source_text)}
    src_plain = _norm_money(source_text)
    for m in MONEY.findall(CITE.sub("", claim)):
        n = _norm_money(m)
        if n not in src_money and n not in src_plain:
            return False, f"amount ₹{m} not in cited sources"
    for ident in IDENT.findall(claim):
        if ident not in source_text:
            return False, f"identifier {ident} not in cited sources"
    return True, ""


class _Result(BaseModel):
    claim: int
    supported: bool
    reason: str = ""


class _Results(BaseModel):
    results: list[_Result]


def verify_answer(
    answer: str, sources: dict[str, str], *, llm: LLM | None = None, use_llm: bool = True
) -> Verification:
    """`sources` maps S-ids to their text (only the sources the model was shown)."""
    claims, uncited = split_claims(answer)
    v = Verification(claims=claims, uncited=uncited)
    to_check: list[Claim] = []
    for c in claims:
        unknown = [s for s in c.source_ids if s not in sources]
        if unknown:
            c.supported, c.reason = False, f"cites unknown source {', '.join(unknown)}"
            continue
        ok, why = facts_in_text(c.text, "\n".join(sources[s] for s in c.source_ids))
        if not ok:
            c.supported, c.reason = False, why
            continue
        to_check.append(c)

    if not to_check:
        return v
    if not use_llm:
        for c in to_check:
            c.supported, c.reason = True, "deterministic check only"
        return v

    llm = llm or get_llm("utility")
    pending = list(to_check)
    # The verifier occasionally skips a claim; re-ask once for just the skipped ones so a
    # dropped result isn't mistaken for "unsupported".
    for _ in range(2):
        if not pending:
            break
        results = _check(llm, pending, sources)
        still: list[Claim] = []
        for i, c in enumerate(pending, 1):
            r = results.get(i)
            if r is None:
                still.append(c)
            else:
                c.supported, c.reason = r.supported, r.reason
        pending = still
    for c in pending:
        c.supported, c.reason = False, "verifier returned no result"
    v.llm_checked = True
    return v


def _check(llm: LLM, claims: list[Claim], sources: dict[str, str]) -> dict[int, _Result]:
    blocks = []
    for i, c in enumerate(claims, 1):
        cited = "\n".join(f'<source id="{s}">\n{sources[s]}\n</source>' for s in c.source_ids)
        blocks.append(f"### Claim {i}\n{CITE.sub('', c.text).strip()}\n\nCited sources:\n{cited}")
    header = f"There are exactly {len(claims)} claims. Return exactly {len(claims)} results.\n\n"
    parsed, _ = llm.chat_json(
        load_prompt("verify"), [Message("user", header + "\n\n".join(blocks))], _Results
    )
    by_idx = {r.claim: r for r in parsed.results}
    # Some replies number claims from 0; if the count matches, fall back to position.
    if set(by_idx) != set(range(1, len(claims) + 1)) and len(parsed.results) == len(claims):
        by_idx = {i: r for i, r in enumerate(parsed.results, 1)}
    return by_idx
