"""Provider contract — the Adapter boundary.

Every vendor is normalised to ONE shape, so nothing downstream ever knows which
vendor served a request. This is what makes the gateway AI-agnostic.

See ARCHITECTURE.md section 11 (Contracts) and section 6 (Fallback chains).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

# A message is the lowest common denominator across every vendor we support.
# role is one of: "system" | "user" | "assistant"
Message = dict[str, str]

# ---------------------------------------------------------------------------
# NEUTRAL OPTION VOCABULARY
#
# Callers speak these names. Each adapter translates them into its vendor's
# spelling. Ollama wants options.num_predict; Anthropic wants a top-level
# max_tokens and rejects anything it does not recognise.
#
# Translating vendor vocabulary is the ADAPTER's job. A handler that sets
# both num_predict AND max_tokens and forwards both is leaking vendor detail
# upward — and it fails the moment a vendor validates its input strictly.
# (It did: "anthropic 400: num_predict: Extra inputs are not permitted".)
# ---------------------------------------------------------------------------
MAX_OUTPUT_TOKENS = "max_output_tokens"
TEMPERATURE = "temperature"
NEUTRAL_OPTIONS = (MAX_OUTPUT_TOKENS, TEMPERATURE)


@dataclass(frozen=True)
class Completion:
    """What every provider returns.

    Deliberately richer than a bare string: the inference.events schema
    (ARCHITECTURE.md section 7) needs tokens, latency and provider identity to
    build the cost ledger and the audit trail. Returning str here would force a
    breaking change in Phase B.
    """

    text: str
    provider: str          # the configured provider name, e.g. "local" / "claude"
    model: str             # the concrete model that answered, e.g. "qwen3:4b"
    latency_ms: int
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def tokens_total(self) -> int:
        return self.tokens_in + self.tokens_out


# --------------------------------------------------------------------------
# Error taxonomy
#
# The split matters: it is what lets the fallback chain decide whether trying
# the next provider could possibly help.
# --------------------------------------------------------------------------


class ProviderError(Exception):
    """Base class for provider failures."""

    retryable: bool = False

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider


class ProviderUnavailable(ProviderError):
    """Transport-level failure: timeout, connection refused, 5xx, 429.

    The request itself was fine, so the next provider in the chain is worth
    trying.
    """

    retryable = True


class ProviderRejected(ProviderError):
    """The request itself is bad: 4xx, malformed messages, context too long.

    Falling back would fail in exactly the same way, so the chain must STOP.
    Retrying a bad request against three providers just wastes three timeouts
    and buries the real error.
    """

    retryable = False


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------


class ModelProvider(ABC):
    """One async method. That is the entire vendor-facing contract.

    Metadata (zone, cost) is injected by the factory from config.yaml rather
    than hardcoded here — config owns those facts, not the adapter.
    """

    def __init__(
        self,
        *,
        name: str,
        model: str,
        zone: str = "onprem",
        cost_per_1k_tokens: float = 0.0,
        timeout_s: float = 120.0,
    ) -> None:
        if zone not in ("onprem", "cloud"):
            raise ValueError(f"zone must be 'onprem' or 'cloud', got {zone!r}")
        self.name = name
        self.model = model
        self.zone = zone
        self.cost_per_1k_tokens = cost_per_1k_tokens
        self.timeout_s = timeout_s

    @abstractmethod
    async def generate(self, messages: list[Message], **opts: Any) -> Completion:
        """Produce a completion, or raise a ProviderError subclass."""

    async def health(self) -> bool:
        """Cheap liveness check. Used by provider selection to skip dead hops.

        Default is optimistic: a provider with no cheap health endpoint is
        assumed up, and real failures surface as ProviderUnavailable.
        """
        return True

    def cost_of(self, completion: Completion) -> float:
        """Cost in USD, for the cost ledger."""
        return (completion.tokens_total / 1000.0) * self.cost_per_1k_tokens

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"{type(self).__name__}(name={self.name!r}, model={self.model!r}, "
            f"zone={self.zone!r})"
        )


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def split_system(messages: list[Message]) -> tuple[str | None, list[Message]]:
    """Separate system messages from the conversation.

    Ollama accepts system messages inline; Anthropic requires them hoisted to a
    top-level `system` field. The same caller input must work for both, so the
    normalisation lives here and the Anthropic adapter calls it.
    """
    system_parts = [m["content"] for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    system = "\n\n".join(system_parts) if system_parts else None
    return system, rest


class _Timer:
    """Context manager measuring wall-clock latency in milliseconds."""

    elapsed_ms: int = 0

    def __enter__(self) -> "_Timer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.elapsed_ms = int((time.perf_counter() - self._start) * 1000)
