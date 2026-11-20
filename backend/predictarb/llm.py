"""LLM client: OpenAI-compatible chat endpoints with provider fallback.

Providers:
  openrouter - https://openrouter.ai/api/v1, OPENROUTER_API_KEY, model OPENROUTER_MODEL
               (default "openrouter/free": OpenRouter routes each call to an available free model)
  local      - any OpenAI-compatible server (llama.cpp / llama-swap, Ollama, LM Studio, vLLM),
               LOCAL_LLM_URL + LOCAL_LLM_MODEL

LLM_ORDER sets the fallback order (e.g. "openrouter,local"). Every call is logged to
llm_call for debugging and the daily budget (AI_MAX_CALLS_PER_DAY).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import asyncpg
import httpx

from predictarb.config import Settings

log = logging.getLogger(__name__)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


class LLMError(RuntimeError):
    pass


class BudgetExceeded(LLMError):
    pass


@dataclass
class Provider:
    name: str
    base_url: str
    model: str
    api_key: str | None = None


def providers(s: Settings) -> list[Provider]:
    available: dict[str, Provider] = {}
    if s.openrouter_api_key:
        available["openrouter"] = Provider("openrouter", "https://openrouter.ai/api/v1", s.openrouter_model, s.openrouter_api_key)
    if s.local_llm_url:
        available["local"] = Provider("local", s.local_llm_url.rstrip("/"), s.local_llm_model, s.local_llm_api_key)
    order = [p.strip() for p in s.llm_order.split(",") if p.strip()]
    return [available[p] for p in order if p in available]


def extract_json(text: str) -> Any:
    """Parse a JSON object from a model reply that may include fences, prose or <think> blocks."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    text = _FENCE.sub("", text).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    depth = 0
    for i in range(start, len(text)) if start >= 0 else ():
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except ValueError:
                    break
    raise LLMError(f"no JSON object in reply: {text[:160]!r}")


class LLM:
    def __init__(self, settings: Settings, pool: asyncpg.Pool | None = None, client: httpx.AsyncClient | None = None):
        self.s = settings
        self.pool = pool
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(self.s.llm_timeout_s))
        self.providers = providers(settings)

    @property
    def configured(self) -> bool:
        return bool(self.providers)

    async def calls_today(self) -> int:
        if self.pool is None:
            return 0
        return await self.pool.fetchval("SELECT count(*) FROM llm_call WHERE ts >= date_trunc('day', now())")

    async def _log(self, task: str, p: Provider, ok: bool, ms: int, usage: dict[str, Any] | None, model: str | None, error: str | None) -> None:
        if self.pool is None:
            return
        try:
            await self.pool.execute(
                """INSERT INTO llm_call (task, provider, model, ok, latency_ms, prompt_tokens, completion_tokens, error)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                task, p.name, model or p.model, ok, ms,
                (usage or {}).get("prompt_tokens"), (usage or {}).get("completion_tokens"), (error or "")[:500] or None,
            )
        except Exception as e:  # logging must never break a call
            log.debug("llm_call log failed: %s", e)

    async def chat_json(self, task: str, system: str, user: str, temperature: float = 0.4, max_tokens: int = 1200) -> tuple[Any, str]:
        """Return (parsed JSON, "provider:model"). Tries each provider in order; one retry per
        provider when the reply isn't valid JSON."""
        if not self.providers:
            raise LLMError("no LLM configured: set OPENROUTER_API_KEY and/or LOCAL_LLM_URL")
        if await self.calls_today() >= self.s.ai_max_calls_per_day:
            raise BudgetExceeded(f"daily LLM call budget reached ({self.s.ai_max_calls_per_day})")
        errors = []
        for p in self.providers:
            for attempt in range(2):
                t0 = time.monotonic()
                headers = {"Content-Type": "application/json"}
                if p.api_key:
                    headers["Authorization"] = f"Bearer {p.api_key}"
                if p.name == "openrouter":
                    headers["X-Title"] = "predictarb"
                body = {
                    "model": p.model,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                }
                try:
                    r = await self.client.post(f"{p.base_url}/chat/completions", json=body, headers=headers)
                    ms = int((time.monotonic() - t0) * 1000)
                    if r.status_code != 200:
                        raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}")
                    data = r.json()
                    if data.get("error"):
                        raise LLMError(str(data["error"])[:200])
                    msg = data["choices"][0]["message"]
                    content = msg.get("content") or msg.get("reasoning") or ""
                    parsed = extract_json(content)
                    model = data.get("model") or p.model
                    await self._log(task, p, True, ms, data.get("usage"), model, None)
                    return parsed, f"{p.name}:{model}"
                except (LLMError, httpx.HTTPError, KeyError, IndexError, ValueError) as e:
                    ms = int((time.monotonic() - t0) * 1000)
                    await self._log(task, p, False, ms, None, None, str(e))
                    errors.append(f"{p.name}: {e}")
                    # Only retry the same provider for a bad reply, not for HTTP/transport errors.
                    if not (isinstance(e, LLMError) and "no JSON" in str(e)):
                        break
        raise LLMError("; ".join(errors)[:600])

    async def aclose(self) -> None:
        await self.client.aclose()
