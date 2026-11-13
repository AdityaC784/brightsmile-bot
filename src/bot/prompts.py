"""Prompt files live in prompts/*.md so prompt changes are reviewable diffs (and trip the CI gate).

Placeholders are `{name}` and are substituted only for the keys passed in, so literal JSON
braces in a prompt are left alone.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache

from bot.config import ROOT, load_yaml

PROMPTS_DIR = ROOT / "prompts"


@lru_cache
def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


def render_prompt(name: str, **values: str) -> str:
    text = load_prompt(name)
    for key, value in values.items():
        placeholder = "{" + key + "}"
        if placeholder not in text:
            raise KeyError(f"prompts/{name}.md has no placeholder {placeholder}")
        text = text.replace(placeholder, value)
    return text


_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


@lru_cache
def clinic_prompt(name: str) -> str:
    """Load prompts/<name>.md with clinic facts filled in from configs/generation.yaml.

    Single source of truth for names, phone numbers and links: prompts reference them as
    {clinic_name}, {reception}, {booking_url}, {after_hours_triage}, {emergency}.
    """
    c = load_yaml("generation")["clinic"]
    values = {
        "clinic_name": c["name"],
        "reception": c["reception"],
        "booking_url": c["booking_url"],
        "after_hours_triage": c["after_hours_triage"],
        "emergency": c["emergency"],
    }
    text = load_prompt(name)
    used = set(_PLACEHOLDER.findall(text))
    unknown = used - values.keys()
    if unknown:
        raise KeyError(f"prompts/{name}.md has unknown placeholders: {sorted(unknown)}")
    return render_prompt(name, **{k: v for k, v in values.items() if k in used})


def prompt_hash(name: str) -> str:
    """Short content hash, recorded with eval runs so results are tied to a prompt version."""
    return hashlib.sha256(load_prompt(name).encode("utf-8")).hexdigest()[:10]
