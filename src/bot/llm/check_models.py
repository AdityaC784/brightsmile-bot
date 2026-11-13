"""Verify that every model ID in configs/models.yaml is live with the configured keys.

    python -m bot.llm.check_models

For each role: is the model listed by the provider, and does a tiny real call succeed?
Exits non-zero if any role fails.
"""

from __future__ import annotations

import sys

from bot.config import load_yaml
from bot.llm.base import Message
from bot.llm.client import _provider, get_llm
from bot.llm.embeddings import Embedder


def main() -> int:
    cfg = load_yaml("models")
    ok = True
    listed: dict[str, list[str] | Exception] = {}

    for role, rc in cfg["roles"].items():
        prov = rc["provider"]
        key_setting = rc.get("api_key_setting")
        cache_key = f"{prov}:{key_setting or 'default'}"
        if cache_key not in listed:
            try:
                listed[cache_key] = _provider(prov, key_setting).list_models()
            except Exception as e:
                listed[cache_key] = e
        models = listed[cache_key]
        in_list = (
            "?" if isinstance(models, Exception) else ("yes" if rc["model"] in models else "NO")
        )

        try:
            resp = get_llm(role, use_cache=False).chat(
                "You are a health check. Reply with exactly: OK",
                [Message("user", "ping")],
                max_tokens=max(rc.get("max_tokens", 64), 64),
            )
            call = f"ok ({resp.model}, {resp.latency_ms} ms, reply={resp.text.strip()[:20]!r})"
        except Exception as e:
            ok = False
            call = f"FAILED: {type(e).__name__}: {str(e)[:300]}"
        print(f"[{role:9}] {prov}/{rc['model']}  listed={in_list}  call={call}")

    ec = cfg["embeddings"]
    try:
        emb = Embedder()
        emb._cache.get = lambda _k: None  # force a live call  # type: ignore[method-assign]
        v = emb.embed_query("root canal price")
        print(f"[embedding] {ec['provider']}/{ec['model']}  dims={len(v)}  call=ok")
    except Exception as e:
        ok = False
        print(f"[embedding] {ec['provider']}/{ec['model']}  call=FAILED: {type(e).__name__}: {e}")

    for prov, models in listed.items():
        if isinstance(models, Exception):
            print(f"\n{prov}: could not list models: {type(models).__name__}: {str(models)[:300]}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
