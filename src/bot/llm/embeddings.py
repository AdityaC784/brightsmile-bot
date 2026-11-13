"""Gemini embeddings, Matryoshka-truncated and L2-normalised to match the pgvector column.

Document embeddings are always cached by (model, dims, task, text) — the corpus rarely changes and
the Gemini free tier is rate-limited, so re-ingesting should cost nothing.
"""

from __future__ import annotations

import math
import time
from typing import Literal

import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt

from bot.config import get_settings, load_yaml
from bot.llm.base import LLMError
from bot.llm.cache import DiskCache, request_key
from bot.llm.providers import gemini_client, is_retryable, wait_retry_after

log = structlog.get_logger(__name__)
Task = Literal["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]


def l2_normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


class Embedder:
    def __init__(self) -> None:
        cfg = load_yaml("models")["embeddings"]
        if cfg["provider"] != "gemini":
            raise LLMError(f"Unsupported embeddings provider: {cfg['provider']}")
        self.model: str = cfg["model"]
        self.dims: int = cfg["dimensions"]
        self.batch_size: int = cfg.get("batch_size", 50)
        self._client = None
        self._cache = DiskCache(get_settings().llm_cache_dir, "embeddings")

    @property
    def client(self):
        if self._client is None:
            self._client = gemini_client()
        return self._client

    def _key(self, text: str, task: Task) -> str:
        return request_key({"m": self.model, "d": self.dims, "t": task, "x": text})

    @retry(
        retry=retry_if_exception(is_retryable),
        stop=stop_after_attempt(6),
        wait=wait_retry_after,
        reraise=True,
    )
    def _embed_batch(self, texts: list[str], task: Task) -> list[list[float]]:
        from google.genai import types

        resp = self.client.models.embed_content(
            model=self.model,
            contents=texts,
            config=types.EmbedContentConfig(task_type=task, output_dimensionality=self.dims),
        )
        vecs = [e.values for e in resp.embeddings]
        if len(vecs) != len(texts) or any(len(v) != self.dims for v in vecs):
            raise LLMError(f"Embedding shape mismatch: got {len(vecs)} vectors for {len(texts)}")
        return [l2_normalize(v) for v in vecs]

    def embed(self, texts: list[str], task: Task = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
        out: list[list[float] | None] = [None] * len(texts)
        todo: list[int] = []
        for i, t in enumerate(texts):
            hit = self._cache.get(self._key(t, task))
            if hit is not None:
                out[i] = hit
            else:
                todo.append(i)

        for start in range(0, len(todo), self.batch_size):
            idx = todo[start : start + self.batch_size]
            t0 = time.perf_counter()
            vecs = self._embed_batch([texts[i] for i in idx], task)
            log.info("embed.batch", n=len(idx), ms=int((time.perf_counter() - t0) * 1000))
            for i, v in zip(idx, vecs, strict=True):
                out[i] = v
                self._cache.set(self._key(texts[i], task), v)
        return out  # type: ignore[return-value]

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text], task="RETRIEVAL_QUERY")[0]
