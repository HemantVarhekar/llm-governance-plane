"""Ollama adapter — the on-prem leg.

Talks to a local Ollama server (default http://localhost:11434). Ollama runs on
the HOST, deliberately outside Kubernetes — see ARCHITECTURE.md section 10.
"""

from __future__ import annotations

from typing import Any

import httpx

from .base import (
    Completion,
    Message,
    ModelProvider,
    ProviderRejected,
    ProviderUnavailable,
    _Timer,
)


class OllamaProvider(ModelProvider):
    """Adapts Ollama's /api/chat to the ModelProvider contract."""

    def __init__(
        self,
        *,
        name: str = "local",
        model: str = "qwen3:4b",
        base_url: str = "http://localhost:11434",
        zone: str = "onprem",
        cost_per_1k_tokens: float = 0.0,  # local inference: no marginal $ cost
        timeout_s: float = 120.0,
        think_default: bool | None = False,
    ) -> None:
        super().__init__(
            name=name,
            model=model,
            zone=zone,
            cost_per_1k_tokens=cost_per_1k_tokens,
            timeout_s=timeout_s,
        )
        self.base_url = base_url.rstrip("/")
        self.think_default = think_default

    async def generate(self, messages: list[Message], **opts: Any) -> Completion:
        # Ollama takes system messages inline, so no restructuring needed.
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }

        # `think` is a TOP-LEVEL Ollama field, not an option. Reasoning models
        # such as qwen3 spend their token budget in message.thinking before
        # writing any content, which on CPU is pure latency for our use case.
        # Default it off; a caller can switch it back on explicitly.
        opts = dict(opts)
        think = opts.pop("think", self.think_default)
        if think is not None:
            payload["think"] = think

        # Translate the NEUTRAL vocabulary into Ollama's spelling.
        max_out = opts.pop("max_output_tokens", None)
        if max_out is not None:
            opts.setdefault("num_predict", int(max_out))

        # Anything else (temperature, top_p, num_predict passed directly...)
        # goes into Ollama's options bag.
        if opts:
            payload["options"] = opts

        try:
            with _Timer() as timer:
                async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                    response = await client.post(
                        f"{self.base_url}/api/chat", json=payload
                    )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # Server down, DNS failure, timeout — the next hop may well work.
            raise ProviderUnavailable(
                f"ollama unreachable at {self.base_url}: {exc}", provider=self.name
            ) from exc

        if response.status_code >= 500:
            raise ProviderUnavailable(
                f"ollama {response.status_code}: {response.text[:200]}",
                provider=self.name,
            )
        if response.status_code >= 400:
            # Bad model name, malformed messages — failing over will not help.
            raise ProviderRejected(
                f"ollama {response.status_code}: {response.text[:200]}",
                provider=self.name,
            )

        data = response.json()
        message = data.get("message") or {}
        text = message.get("content")
        if not isinstance(text, str):
            raise ProviderUnavailable(
                f"ollama returned no message content: {str(data)[:200]}",
                provider=self.name,
            )

        # An empty answer is a FAILURE, not a valid completion. The usual cause
        # on a reasoning model is the token budget being consumed by thinking
        # before any content was written. Say so, instead of handing the caller
        # a silent empty string.
        if not text.strip():
            thinking = (message.get("thinking") or "").strip()
            detail = (
                f"token budget consumed by reasoning ({len(thinking)} chars of "
                f"thinking, 0 of content); raise num_predict or set think=False"
                if thinking
                else "model returned empty content"
            )
            raise ProviderUnavailable(
                f"ollama produced no answer: {detail}", provider=self.name
            )

        return Completion(
            text=text,
            provider=self.name,
            model=data.get("model", self.model),
            latency_ms=timer.elapsed_ms,
            tokens_in=int(data.get("prompt_eval_count") or 0),
            tokens_out=int(data.get("eval_count") or 0),
        )

    async def health(self) -> bool:
        """GET /api/tags — cheap, and confirms the server is actually serving."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self.base_url}/api/tags")
            return response.status_code == 200
        except (httpx.TimeoutException, httpx.TransportError):
            return False
