"""Review feedback candidates and turn the useful ones into golden cases (`make review`).

Candidates are written by the API (thumbs down, abstained, handoff, failed citation check).
For each pending one you see the question, the bot's answer and the retrieved chunks, then:
  a  accept  - you type the correct answer (by hand, never LLM-generated); it is appended to
               evals/golden/v1.jsonl with the new dataset_version and logged in CHANGELOG.md
  r  reject  - not useful; never shown again
  d  defer   - decide later (shown again with --deferred)
  s  skip    - leave it pending
  q  quit

    uv run python -m evals.review_cli                # local database (DATABASE_URL)
    uv run python -m evals.review_cli --neon         # production (NEON_DATABASE_URL in .env)
    uv run python -m evals.review_cli --deferred     # also show deferred candidates
    uv run python -m evals.review_cli --version 0.7.0  # add to an existing version
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from collections.abc import Callable
from pathlib import Path

import psycopg
from pydantic_settings import BaseSettings, SettingsConfigDict

from bot.config import ROOT, get_settings
from evals.golden_set import GOLDEN_PATH, GoldenCase, load_golden

CHANGELOG_PATH = ROOT / "evals" / "golden" / "CHANGELOG.md"
BEHAVIORS = ("answer", "abstain", "clarify", "refuse", "handoff")
TYPES = ("lookup", "exact_id", "multi_source", "unanswerable", "ambiguous", "safety")


class _Neon(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    neon_database_url: str = ""


# --------------------------------------------------------------------------- golden-set helpers


def next_case_id(cases: list[GoldenCase]) -> str:
    nums = [int(m.group(1)) for c in cases if (m := re.fullmatch(r"q-(\d+)", c.id))]
    return f"q-{max(nums, default=0) + 1:03d}"


def next_version(cases: list[GoldenCase]) -> str:
    """Next minor version after the highest dataset_version in the set (0.6.1 -> 0.7.0)."""
    major, minor, _ = max(tuple(map(int, c.dataset_version.split("."))) for c in cases)
    return f"{major}.{minor + 1}.0"


def doc_ids(retrieved_ids: list[str]) -> list[str]:
    """Document-level ids from chunk ids ('DENT-019#root-canal' -> 'DENT-019'), in order."""
    out: list[str] = []
    for r in retrieved_ids:
        d = r.split("#")[0]
        if re.fullmatch(r"DENT-\d{3}", d) and d not in out:
            out.append(d)
    return out


def append_case(case: GoldenCase, path: Path = GOLDEN_PATH) -> None:
    text = path.read_text(encoding="utf-8")
    line = json.dumps(case.model_dump(exclude_defaults=True), ensure_ascii=False)
    sep = "" if not text or text.endswith("\n") else "\n"
    path.write_text(text + sep + line + "\n", encoding="utf-8")


def add_changelog_entry(version: str, bullet: str, path: Path = CHANGELOG_PATH) -> None:
    """Add `bullet` under `## <version>`; create that section (newest first) if it is missing."""
    lines = path.read_text(encoding="utf-8").splitlines()
    head = f"## {version} "
    start = next((i for i, ln in enumerate(lines) if ln.startswith(head)), None)
    if start is None:
        first = next((i for i, ln in enumerate(lines) if ln.startswith("## ")), len(lines))
        lines[first:first] = [f"## {version} — {dt.date.today().isoformat()}", bullet, ""]
    else:
        end = start + 1
        while end < len(lines) and lines[end].strip() and not lines[end].startswith("## "):
            end += 1
        lines.insert(end, bullet)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- database


class CandidateStore:
    def __init__(self, database_url: str):
        self.conn = psycopg.connect(database_url)

    def load(self, include_deferred: bool) -> list[dict]:
        statuses = ["pending", "deferred"] if include_deferred else ["pending"]
        cur = self.conn.execute(
            """SELECT id, source, question, bot_answer, retrieved_ids, status, created_at
               FROM candidates WHERE status = ANY(%s) ORDER BY created_at, id""",
            (statuses,),
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def mark(self, cid: int, status: str, reviewer_answer: str | None = None) -> None:
        self.conn.execute(
            """UPDATE candidates SET status = %s, reviewer_answer = %s, reviewed_at = now()
               WHERE id = %s""",
            (status, reviewer_answer, cid),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


# --------------------------------------------------------------------------- interactive loop


def _ask(input_fn: Callable[[str], str], prompt: str, default: str = "") -> str:
    shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
    return input_fn(shown).strip() or default


def _choose(input_fn, prompt: str, options: tuple[str, ...], default: str) -> str:
    while True:
        v = _ask(input_fn, f"{prompt} ({'/'.join(options)})", default)
        if v in options:
            return v
        print(f"  choose one of: {', '.join(options)}")


def _split_list(text: str, sep: str) -> list[str]:
    return [p.strip() for p in text.split(sep) if p.strip()]


def build_case(cand: dict, case_id: str, version: str, input_fn) -> GoldenCase | None:
    """Ask the reviewer for the case fields; None if they leave the answer empty."""
    answer = _ask(input_fn, "Correct answer, written by you (empty = cancel)")
    if not answer:
        return None
    behavior = _choose(input_fn, "Expected behaviour", BEHAVIORS, "answer")
    ctype = _choose(
        input_fn, "Case type", TYPES, "unanswerable" if behavior == "abstain" else "lookup"
    )
    while True:
        docs = _split_list(
            _ask(input_fn, "Gold docs, comma-separated", ",".join(doc_ids(cand["retrieved_ids"]))),
            ",",
        )
        if all(re.fullmatch(r"DENT-\d{3}", d) for d in docs):
            break
        print("  doc ids look like DENT-019")
    must = _split_list(_ask(input_fn, "Must include, ';'-separated (optional)"), ";")
    split = _choose(input_fn, "Split", ("dev", "holdout"), "dev")
    return GoldenCase(
        id=case_id,
        dataset_version=version,
        split=split,
        type=ctype,
        question=cand["question"],
        gold_doc_ids=docs,
        expected_behavior=behavior,
        must_abstain=behavior == "abstain",
        golden_answer=answer,
        must_include=must,
        notes=f"From review candidate #{cand['id']} ({cand['source']}).",
    )


def show(cand: dict, n: int, total: int) -> None:
    print(
        f"\n{'=' * 78}\n[{n}/{total}]  candidate #{cand['id']}  source: {cand['source']}"
        f"  status: {cand['status']}  {cand['created_at']:%Y-%m-%d %H:%M}"
    )
    print(f"\nPatient asked:\n  {cand['question']}")
    print(f"\nBot answered:\n  {cand['bot_answer'] or '(no answer)'}")
    print(f"\nRetrieved: {', '.join(cand['retrieved_ids']) or '(none)'}\n")


def review(
    store,
    version: str | None = None,
    include_deferred: bool = False,
    input_fn: Callable[[str], str] = input,
    golden_path: Path = GOLDEN_PATH,
    changelog_path: Path = CHANGELOG_PATH,
) -> dict[str, int]:
    counts = {"accepted": 0, "rejected": 0, "deferred": 0, "skipped": 0}
    cands = store.load(include_deferred)
    if not cands:
        print("No candidates to review.")
        return counts
    cases = load_golden(golden_path)
    version = version or next_version(cases)
    print(f"{len(cands)} candidate(s). Accepted cases get dataset_version {version}.")
    for n, cand in enumerate(cands, 1):
        show(cand, n, len(cands))
        action = _choose(input_fn, "(a)ccept (r)eject (d)efer (s)kip (q)uit", tuple("ardsq"), "s")
        if action == "q":
            break
        if action == "s":
            counts["skipped"] += 1
        elif action == "r":
            store.mark(cand["id"], "rejected")
            counts["rejected"] += 1
        elif action == "d":
            store.mark(cand["id"], "deferred")
            counts["deferred"] += 1
        else:
            case = build_case(cand, next_case_id(cases), version, input_fn)
            if case is None:
                print("  cancelled, left as it was")
                counts["skipped"] += 1
                continue
            append_case(case, golden_path)
            add_changelog_entry(
                version,
                f"- {case.id} ({case.split}, {case.type}): from review candidate #{cand['id']}"
                f" ({cand['source']}); golden answer written by the owner in `make review`.",
                changelog_path,
            )
            store.mark(cand["id"], "accepted", case.golden_answer)
            cases.append(case)
            counts["accepted"] += 1
            print(f"  added {case.id} to {golden_path.name}")
    holdout = sum(c.split == "holdout" for c in cases)
    print(
        f"\nDone: {counts}. Golden set: {len(cases)} cases, {holdout} holdout "
        f"({holdout / len(cases):.0%}; keep it between 15% and 25%)."
    )
    if counts["accepted"]:
        print(
            "Next: commit the golden set, run the eval, then `make eval-baseline` and re-set "
            "max_drop_points in configs/gate.yaml."
        )
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--neon", action="store_true", help="read production candidates (Neon)")
    ap.add_argument("--deferred", action="store_true", help="also show deferred candidates")
    ap.add_argument("--version", help="dataset_version for accepted cases (default: next minor)")
    args = ap.parse_args()
    url = _Neon().neon_database_url if args.neon else get_settings().database_url
    if not url:
        raise SystemExit("NEON_DATABASE_URL is not set in .env")
    store = CandidateStore(url)
    try:
        review(store, args.version, args.deferred)
    except (KeyboardInterrupt, EOFError):
        print("\nStopped; everything reviewed so far is saved.")
    finally:
        store.close()


if __name__ == "__main__":
    main()
