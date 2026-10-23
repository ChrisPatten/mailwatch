"""Tool-less model calls. One system prompt (trusted), one user message (untrusted),
one schema-enforced JSON answer. No tool definitions are ever sent."""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .secrets import resolve

log = logging.getLogger("mailwatch.llm")

# $ per MTok: input, cached input, output. Used only for the run log's cost column.
PRICES = {
    "gpt-6-luna": (0.10, 0.01, 0.50), "gpt-6-sol": (2.00, 0.20, 10.00), "gpt-5.6-terra": (2.00, 0.20, 12.00),
    "claude-haiku-5-5": (0.10, 0.01, 0.50), "claude-sonnet-5-5": (2.00, 0.10, 10.00),
}


class LLMError(RuntimeError):
    """The model answered, but not usably (refusal, non-JSON, truncated)."""


class LLMUnavailable(LLMError):
    """The API could not be used: auth, quota, HTTP or network failure. Messages wait and retry."""

    def __init__(self, msg: str, status: int = 0):
        super().__init__(msg)
        self.status = status


@dataclass
class Usage:
    calls: int = 0
    input: int = 0
    cached: int = 0
    output: int = 0
    cost: float = 0.0
    by_model: dict[str, dict[str, float]] = field(default_factory=dict)
    _lock: Any = field(default_factory=threading.Lock, repr=False)

    def add(self, model: str, inp: int, cached: int, out: int) -> None:
        with self._lock:
            self._add(model, inp, cached, out)

    def _add(self, model: str, inp: int, cached: int, out: int) -> None:
        p = PRICES.get(model, (0, 0, 0))
        c = ((inp - cached) * p[0] + cached * p[1] + out * p[2]) / 1e6
        self.calls += 1
        self.input += inp
        self.cached += cached
        self.output += out
        self.cost += c
        m = self.by_model.setdefault(model, {"calls": 0, "input": 0, "output": 0, "cost": 0.0})
        m["calls"] += 1
        m["input"] += inp
        m["output"] += out
        m["cost"] += c


class Models:
    def __init__(self, models_cfg: dict[str, Any], keys: dict[str, str], transport: Any = None):
        self.cfg = models_cfg
        self.keys = keys
        self.usage = Usage()
        self._key_cache: dict[str, str] = {}
        self._client = httpx.Client(timeout=httpx.Timeout(120, connect=15), transport=transport,
                                    limits=httpx.Limits(max_connections=16))

    def check(self, spec: str) -> str:
        """Cheap key check (no tokens): list models."""
        provider = spec.split(":", 1)[0]
        if provider == "openai":
            r = self._client.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {self._key('openai')}"})
        else:
            r = self._client.get("https://api.anthropic.com/v1/models",
                                 headers={"x-api-key": self._key("anthropic"), "anthropic-version": "2023-06-01"})
        if r.status_code >= 300:
            return f"HTTP {r.status_code}: {r.text[:120]}"
        ids = {m.get("id") for m in r.json().get("data", [])}
        model = spec.split(":", 1)[1]
        return "ok" if model in ids or not ids else f"key ok, but model {model!r} not listed"

    def _key(self, provider: str) -> str:
        if provider not in self._key_cache:
            self._key_cache[provider] = resolve(self.keys[provider])
        return self._key_cache[provider]

    def json(self, role: str, system: str, user: str, schema: dict[str, Any], name: str,
             spec: str | None = None, max_tokens: int = 2500) -> dict[str, Any]:
        spec = spec or self.cfg[role]
        provider, model = spec.split(":", 1)
        if provider == "openai":
            text, inp, cached, out = self._openai(model, system, user, schema, name, max_tokens)
        elif provider == "anthropic":
            text, inp, cached, out = self._anthropic(model, system, user, schema, max_tokens)
        else:
            raise LLMError(f"unknown provider {provider}")
        self.usage.add(model, inp, cached, out)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"{model} returned non-JSON: {text[:120]!r}") from e

    # -- retry: 429/5xx/network with exponential backoff + jitter, honoring Retry-After --
    def _post(self, url: str, headers: dict[str, str], body: dict[str, Any], attempts: int = 5) -> dict[str, Any]:
        delay, last, code = 1.0, "", 0
        for i in range(attempts):
            try:
                r = self._client.post(url, headers=headers, json=body)
            except httpx.HTTPError as e:
                last, code = f"network: {e}", 0
            else:
                if r.status_code < 300:
                    return r.json()
                last, code = f"HTTP {r.status_code}: {r.text[:200]}", r.status_code
                if r.status_code != 429 and r.status_code < 500:
                    raise LLMUnavailable(last, r.status_code)
                ra = r.headers.get("retry-after")
                if ra:
                    try:
                        delay = min(float(ra), 30.0)
                    except ValueError:
                        pass
            time.sleep(delay + random.uniform(0, 0.5))
            delay = min(delay * 2, 30.0)
        raise LLMUnavailable(f"exhausted retries: {last}", code)

    def _openai(self, model: str, system: str, user: str, schema: dict[str, Any], name: str,
                max_tokens: int) -> tuple[str, int, int, int]:
        body: dict[str, Any] = {
            "model": model,
            "input": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "text": {"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}},
            "max_output_tokens": max_tokens + 4000,
            "store": False,
        }
        eff = self.cfg.get("openai_reasoning_effort")
        if eff:
            body["reasoning"] = {"effort": eff}
        d = self._post("https://api.openai.com/v1/responses",
                       {"Authorization": f"Bearer {self._key('openai')}"}, body)
        text = d.get("output_text") or ""
        if not text:
            for item in d.get("output", []):
                if item.get("type") == "message":
                    for c in item.get("content", []):
                        if c.get("type") == "refusal":
                            raise LLMError("model refused")
                        if c.get("type") in ("output_text", "text") and c.get("text"):
                            text = c["text"]
        if not text:
            raise LLMError(f"no text in response ({d.get('status')})")
        u = d.get("usage", {})
        return (text, int(u.get("input_tokens", 0)),
                int((u.get("input_tokens_details") or {}).get("cached_tokens", 0)), int(u.get("output_tokens", 0)))

    def _anthropic(self, model: str, system: str, user: str, schema: dict[str, Any],
                   max_tokens: int) -> tuple[str, int, int, int]:
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }
        budget = int(self.cfg.get("anthropic_thinking_budget") or 0)
        if budget:
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
            body["max_tokens"] = max_tokens + budget
        d = self._post("https://api.anthropic.com/v1/messages",
                       {"x-api-key": self._key("anthropic"), "anthropic-version": "2023-06-01"}, body)
        if d.get("stop_reason") in ("refusal", "max_tokens"):
            raise LLMError(f"stop_reason {d.get('stop_reason')}")
        text = next((b.get("text", "") for b in d.get("content", []) if b.get("type") == "text"), "")
        u = d.get("usage", {})
        cached = int(u.get("cache_read_input_tokens", 0))
        inp = int(u.get("input_tokens", 0)) + cached + int(u.get("cache_creation_input_tokens", 0))
        return text, inp, cached, int(u.get("output_tokens", 0))
