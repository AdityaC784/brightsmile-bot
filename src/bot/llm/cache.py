"""Tiny on-disk cache keyed by a hash of the full request.

Keeps eval runs cheap and deterministic: an unchanged prompt to an unchanged model returns the
stored response instead of calling the provider again. One JSON file per entry, sharded by the
first two hex chars of the key.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from bot.config import ROOT


def request_key(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class DiskCache:
    def __init__(self, directory: str | Path, namespace: str):
        base = Path(directory)
        if not base.is_absolute():
            base = ROOT / base
        self.dir = base / namespace

    def _path(self, key: str) -> Path:
        return self.dir / key[:2] / f"{key}.json"

    def get(self, key: str) -> Any | None:
        p = self._path(key)
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def set(self, key: str, value: Any) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)  # atomic on the same filesystem
