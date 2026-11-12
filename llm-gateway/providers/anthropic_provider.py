"""Anthropic adapter — the cloud leg.

This is the instructive one. Anthropic requires `system` messages hoisted OUT of
the message list into a top-level field, and requires max_tokens. Ollama needs
neither. Same contract, completely different internals — which is Adapter
earning its keep.

Note what this file does NOT know about: PII. The hard constraint that keeps
PII away from the cloud lives in the routing policy, not in the adapter, so
compliance can be proven in one place instead of audited across every vendor.
See ARCHITECTURE.md section 4.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from .base import (
    Completion,
    Message,
    ModelProvider,
    ProviderRejected,
    ProviderUnavailable,
    _Timer,
    split_system,
)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class AnthropicProvider(ModelProvider):
    """Adapts the Anthropic Messages API to the ModelProvider contract."""

    def __init__(
        self,
        *,
        name: str = "claude",
        model: str = "claude-sonnet-5-5",
        max_tokens: int = 1024,
        api_key: str | None = None,
        zone: str = "cloud",
        cost_per_1k_tokens: float = 0.003,
        timeout_s: float = 120.0,
    ) -> None:
        super().__init__(
            name=name,
            model=model,
            zone=zone,
            cost_per_1k_tokens=cost_per_1k_tokens,
            timeout_s=timeout_s,
        )
        self.max_tokens = max_tokens
        # Read lazily from env so the gateway can start without a key; the
        # provider then simply reports unhealthy.
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    async def generate(self, messages: list[Message], **opts: Any) -> Completion:
        if not self._api_key:
            raise ProviderUnavailable(
                "ANTHROPIC_API_KEY is not set", provider=self.name
            )

        # THE difference from Ollama: system must be hoisted out of the list.
        system, conversation = split_system(messages)
        if not conversation:
            raise ProviderRejected(
                "anthropic requires at least one non-system message",
                provider=self.name,
            )

        opts = dict(opts)
        # Translate the NEUTRAL vocabulary into Anthropic's spelling.
        max_out = opts.pop("max_output_tokens", None)

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": int(
                max_out if max_out is not None
                else opts.pop("max_tokens", self.max_tokens)
            ),
            "messages": conversation,
        }
        if system:
            payload["system"] = system

        # ALLOWLIST, not passthrough. Anthropic validates strictly and 400s on
        # unknown fields, so an option intended for another vendor must be
        # dropped here rather than forwarded. Blind payload.update(opts) is
        # what produced "num_predict: Extra inputs are not permitted".
        for key in ("temperature", "top_p", "top_k", "stop_sequences", "metadata"):
            if key in opts:
                payload[key] = opts[key]

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }

        try:
            with _Timer() as timer:
                async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                    response = await client.post(
                        API_URL, json=payload, headers=headers
                    )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ProviderUnavailable(
                f"anthropic unreachable: {exc}", provider=self.name
            ) from exc

        # 429 (rate limited) and 529 (overloaded) mean "come back later", so
        # they are retryable: the chain falls through to the local model rather
        # than failing the customer's request.
        if response.status_code in (429, 529) or response.status_code >= 500:
            raise ProviderUnavailable(
                f"anthropic {response.status_code}: {response.text[:200]}",
                provider=self.name,
            )
        if response.status_code >= 400:
            raise ProviderRejected(
                f"anthropic {response.status_code}: {response.text[:200]}",
                provider=self.name,
            )

        data = response.json()
        blocks = data.get("content") or []
        # Concatenate text blocks; ignore any non-text block types.
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

        if not text.strip():
            # Same failure class as qwen3 locally: a reasoning model can spend
            # the entire token budget thinking and never emit text. Diagnose
            # it, rather than dumping raw JSON at the caller.
            kinds = sorted({b.get("type", "?") for b in blocks})
            if "thinking" in kinds:
                detail = (
                    f"token budget ({payload['max_tokens']}) consumed by "
                    f"reasoning — response held only {kinds} blocks. "
                    f"Raise max_output_tokens."
                )
            else:
                detail = (
                    f"no text block in response (block types: {kinds or 'none'}, "
                    f"stop_reason={data.get('stop_reason')!r})"
                )
            # Retryable: a bigger budget or another provider may well work,
            # and falling back beats handing the customer an empty message.
            raise ProviderUnavailable(
                f"anthropic produced no answer: {detail}", provider=self.name
            )

        usage = data.get("usage") or {}
        return Completion(
            text=text,
            provider=self.name,
            model=data.get("model", self.model),
            latency_ms=timer.elapsed_ms,
            tokens_in=int(usage.get("input_tokens") or 0),
            tokens_out=int(usage.get("output_tokens") or 0),
        )

    async def health(self) -> bool:
        """No free health endpoint, so report on configuration only.

        A real outage surfaces as ProviderUnavailable during generate(), which
        the fallback chain already handles.
        """
        return bool(self._api_key)
