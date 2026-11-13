"""Concrete chat providers. Each SDK is imported lazily so a missing key only fails its own role.

Every provider takes an optional `api_key`; when omitted it uses the provider's default key from
.env (GROQ_API_KEY / GEMINI_API_KEY). Roles can point at a different key via `api_key_setting`
in configs/models.yaml, e.g. a second Groq key so the generator has its own rate-limit bucket.
"""

from __future__ import annotations

import random
import time

from tenacity import RetryCallState, wait_exponential_jitter

from bot.config import get_settings
from bot.llm.base import ChatRequest, ChatResponse, LLMError

# HTTP statuses worth retrying: timeouts, conflicts, rate limits, server errors.
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
REQUEST_TIMEOUT_S = 60


def is_retryable(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(status, int):
        return status in RETRYABLE_STATUS
    # No status => network/connection-level failure.
    name = type(exc).__name__.lower()
    return "connection" in name or "timeout" in name


_backoff = wait_exponential_jitter(initial=1, max=30)


def wait_retry_after(retry_state: RetryCallState) -> float:
    """Honour the provider's Retry-After header (Groq sends it on 429s), else back off
    exponentially. Groq's token-per-minute limits often need ~10-60 s, longer than a blind
    backoff would wait."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    try:
        seconds = float(headers.get("retry-after", ""))
    except (TypeError, ValueError):
        return _backoff(retry_state)
    return min(seconds + random.uniform(0.2, 1.5), 90.0)


def _require(key: str, env_name: str) -> str:
    if not key:
        raise LLMError(f"{env_name} is not set in .env")
    return key


class GroqProvider:
    name = "groq"

    def __init__(self, api_key: str | None = None) -> None:
        import groq

        key = api_key or _require(get_settings().groq_api_key, "GROQ_API_KEY")
        # Retries are handled by our tenacity wrapper, not the SDK, so behaviour is uniform.
        self._client = groq.Groq(api_key=key, max_retries=0, timeout=REQUEST_TIMEOUT_S)

    def complete(self, req: ChatRequest) -> ChatResponse:
        messages = [{"role": "system", "content": req.system}] + [
            {"role": m.role, "content": m.content} for m in req.messages
        ]
        kwargs = dict(req.options)
        if req.temperature is not None:
            kwargs["temperature"] = req.temperature
        if req.json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        t0 = time.perf_counter()
        try:
            resp = self._client.chat.completions.create(
                model=req.model, messages=messages, max_completion_tokens=req.max_tokens, **kwargs
            )
        except Exception as e:
            # Groq rejects JSON-mode outputs that fail its own JSON validation with a 400
            # (`json_validate_failed`). Retry once without JSON mode; the caller's prompt still
            # asks for JSON and chat_json extracts/repairs it.
            if not (req.json_mode and "json_validate_failed" in str(e)):
                raise
            kwargs.pop("response_format", None)
            resp = self._client.chat.completions.create(
                model=req.model, messages=messages, max_completion_tokens=req.max_tokens, **kwargs
            )
        choice = resp.choices[0]
        usage = resp.usage
        return ChatResponse(
            text=choice.message.content or "",
            provider=self.name,
            model=resp.model,
            tokens_in=usage.prompt_tokens if usage else 0,
            tokens_out=usage.completion_tokens if usage else 0,
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )

    def list_models(self) -> list[str]:
        return sorted(m.id for m in self._client.models.list().data)


def gemini_client(api_key: str | None = None):
    """google-genai client (used for both the judge and embeddings)."""
    from google import genai
    from google.genai import types

    s = get_settings()
    key = api_key or _require(s.gemini_api_key, "GEMINI_API_KEY")
    # SDK-level retries off: our tenacity wrapper is the single retry layer, so a 503 storm
    # fails in bounded time instead of stacking two backoff loops.
    http = types.HttpOptions(
        timeout=REQUEST_TIMEOUT_S * 1000, retry_options=types.HttpRetryOptions(attempts=1)
    )
    return genai.Client(api_key=key, vertexai=s.gemini_vertexai or None, http_options=http)


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str | None = None) -> None:
        self._client = gemini_client(api_key)

    def complete(self, req: ChatRequest) -> ChatResponse:
        from google.genai import types

        cfg = types.GenerateContentConfig(
            system_instruction=req.system,
            max_output_tokens=req.max_tokens,
            temperature=req.temperature,
            response_mime_type="application/json" if req.json_mode else None,
            **req.options,
        )
        contents = [
            types.Content(
                role="model" if m.role == "assistant" else "user",
                parts=[types.Part(text=m.content)],
            )
            for m in req.messages
        ]
        t0 = time.perf_counter()
        resp = self._client.models.generate_content(model=req.model, contents=contents, config=cfg)
        usage = resp.usage_metadata
        return ChatResponse(
            text=resp.text or "",
            provider=self.name,
            model=req.model,
            tokens_in=(usage.prompt_token_count or 0) if usage else 0,
            tokens_out=(usage.candidates_token_count or 0) if usage else 0,
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )

    def list_models(self) -> list[str]:
        return sorted(m.name.removeprefix("models/") for m in self._client.models.list())


PROVIDERS = {"groq": GroqProvider, "gemini": GeminiProvider}
