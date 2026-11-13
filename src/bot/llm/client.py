"""Role-based LLM client: the only entry point the rest of the app uses.

    llm = get_llm("generator")
    resp = llm.chat(system, [Message("user", "...")])
    parsed = llm.chat_json(system, messages, schema=MyModel)

Which provider/model backs a role is decided by configs/models.yaml.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from functools import lru_cache
from typing import TypeVar

import structlog
from pydantic import BaseModel, ValidationError
from tenacity import retry, retry_if_exception, stop_after_attempt

from bot.config import get_settings, load_yaml
from bot.llm.base import ChatProvider, ChatRequest, ChatResponse, LLMError, Message
from bot.llm.cache import DiskCache, request_key
from bot.llm.providers import PROVIDERS, is_retryable, wait_retry_after

log = structlog.get_logger(__name__)
T = TypeVar("T", bound=BaseModel)

_ROLE_KEYS = {"provider", "model", "max_tokens", "temperature", "api_key_setting"}


@lru_cache
def _provider(name: str, key_setting: str | None = None) -> ChatProvider:
    """One client per (provider, key). `key_setting` names a Settings field holding the key;
    if it is unset or empty, the provider's default key is used."""
    if name not in PROVIDERS:
        raise LLMError(f"Unknown provider '{name}' in configs/models.yaml")
    key = getattr(get_settings(), key_setting, "") if key_setting else ""
    return PROVIDERS[name](api_key=key or None)


def _extract_json(text: str) -> str:
    """Tolerate code fences or stray prose around a JSON object."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


class LLM:
    def __init__(self, role: str, cfg: dict, use_cache: bool):
        self.role = role
        self.provider_name: str = cfg["provider"]
        self.model: str = cfg["model"]
        self.max_tokens: int = cfg.get("max_tokens", 1024)
        self.temperature: float | None = cfg.get("temperature")
        self.key_setting: str | None = cfg.get("api_key_setting")
        self.options: dict = {k: v for k, v in cfg.items() if k not in _ROLE_KEYS}
        self.use_cache = use_cache
        self._cache = DiskCache(get_settings().llm_cache_dir, "chat")

    @property
    def provider(self) -> ChatProvider:
        return _provider(self.provider_name, self.key_setting)

    def chat(
        self,
        system: str,
        messages: list[Message],
        *,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        req = ChatRequest(
            model=self.model,
            system=system,
            messages=messages,
            max_tokens=max_tokens or self.max_tokens,
            temperature=self.temperature,
            json_mode=json_mode,
            options=self.options,
        )
        key = request_key(req.cache_payload(self.provider_name))
        if self.use_cache and (hit := self._cache.get(key)):
            return ChatResponse(**{**hit, "cached": True, "latency_ms": 0})

        resp = self._call(req)
        log.debug(
            "llm.call",
            role=self.role,
            model=resp.model,
            tokens_in=resp.tokens_in,
            tokens_out=resp.tokens_out,
            latency_ms=resp.latency_ms,
        )
        if self.use_cache:
            self._cache.set(key, asdict(resp))
        return resp

    @retry(
        retry=retry_if_exception(is_retryable),
        stop=stop_after_attempt(8),
        wait=wait_retry_after,
        reraise=True,
    )
    def _call(self, req: ChatRequest) -> ChatResponse:
        return self.provider.complete(req)

    def chat_json(
        self,
        system: str,
        messages: list[Message],
        schema: type[T],
        *,
        max_tokens: int | None = None,
    ) -> tuple[T, ChatResponse]:
        """Ask for JSON matching `schema`; one repair attempt if the first reply doesn't validate."""
        sys_prompt = (
            f"{system}\n\nReturn JSON matching this JSON Schema:\n"
            f"{json.dumps(schema.model_json_schema(), ensure_ascii=False)}"
        )
        resp = self.chat(sys_prompt, messages, json_mode=True, max_tokens=max_tokens)
        try:
            return schema.model_validate_json(_extract_json(resp.text)), resp
        except ValidationError as err:
            repair = [
                *messages,
                Message("assistant", resp.text),
                Message("user", f"That JSON was invalid: {err}. Reply with corrected JSON only."),
            ]
            resp2 = self.chat(sys_prompt, repair, json_mode=True, max_tokens=max_tokens)
            try:
                return schema.model_validate_json(_extract_json(resp2.text)), resp2
            except ValidationError as err2:
                raise LLMError(f"{self.role}: invalid JSON after repair: {err2}") from err2


def get_llm(role: str, *, use_cache: bool | None = None) -> LLM:
    roles = load_yaml("models")["roles"]
    if role not in roles:
        raise LLMError(f"Role '{role}' not defined in configs/models.yaml")
    if use_cache is None:
        use_cache = get_settings().llm_cache_enabled
    return LLM(role, roles[role], use_cache)
