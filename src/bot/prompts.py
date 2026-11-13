"""Prompt files live in prompts/*.md so prompt changes are reviewable diffs (and trip the CI gate).

Placeholders are `{name}` and are substituted only for the keys passed in, so literal JSON
braces in a prompt are left alone.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache

from bot.config import ROOT

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


def prompt_hash(name: str) -> str:
    """Short content hash, recorded with eval runs so results are tied to a prompt version."""
    return hashlib.sha256(load_prompt(name).encode("utf-8")).hexdigest()[:10]
