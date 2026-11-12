"""Routing — the Strategy pattern, and the fallback chain that walks it.

Two jobs, deliberately separate:

  1. ORDER   — a swappable RoutingStrategy decides the attempt order.
  2. WALK    — execute_with_fallback() tries each hop in turn, honouring the
               retryable/non-retryable split from providers/base.py.

A strategy can only ever reorder what policy already ALLOWED. Excluded
providers are not in its input, so the compliance guarantee does not depend
on any strategy behaving well.

See ARCHITECTURE.md sections 3 (stages 2-3, 5), 6 (fallback) and 11.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from policy import PolicyDecision
from providers import (
    Completion,
    Message,
    ModelProvider,
    ProviderError,
    ProviderRejected,
    ProviderUnavailable,
)

# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

OK = "ok"
UNAVAILABLE = "unavailable"
REJECTED = "rejected"
SKIPPED = "skipped"


@dataclass(frozen=True)
class FallbackHop:
    """One attempt. Recorded even on success.

    Logging only failures would make "worked first time" and "never tried"
    look identical in the audit log.
    """

    provider: str
    outcome: str
    latency_ms: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "outcome": self.outcome,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


@dataclass(frozen=True)
class RoutingResult:
    """Outcome of walking a chain.

    Failure is a VALUE, not an exception — the audit event must be written,
    and an exception can escape the path that writes it.
    """

    completion: Completion | None = None
    provider_used: str | None = None
    hops: tuple[FallbackHop, ...] = ()
    refused: bool = False
    reason: str | None = None
    elapsed_ms: int = 0

    @property
    def degraded(self) -> bool:
        """True when we succeeded, but not on the first choice."""
        return self.completion is not None and len(self.hops) > 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_used": self.provider_used,
            "hops": [h.as_dict() for h in self.hops],
            "degraded": self.degraded,
            "refused": self.refused,
            "reason": self.reason,
            "elapsed_ms": self.elapsed_ms,
        }


# --------------------------------------------------------------------------
# Strategy
# --------------------------------------------------------------------------


class RoutingStrategy(ABC):
    """Decides the ORDER providers are attempted in.

    Swapping the object changes routing behaviour without touching the
    handler. That is the whole point of the pattern — and it is demonstrable,
    not merely asserted.
    """

    name = "abstract"

    @abstractmethod
    def order(
        self,
        decision: PolicyDecision,
        providers: Mapping[str, ModelProvider],
        health: Mapping[str, bool] | None = None,
    ) -> tuple[str, ...]:
        """Return the attempt order, drawn ONLY from decision.providers_allowed."""

    @staticmethod
    def _allowed(
        decision: PolicyDecision, providers: Mapping[str, ModelProvider]
    ) -> list[str]:
        # Belt and braces: never emit a name policy did not allow, and never
        # one that is not actually built.
        return [n for n in decision.providers_allowed if n in providers]


class PolicyStrategy(RoutingStrategy):
    """Default. Honour the task's chain; otherwise sort by the policy's
    selection order (health, then cost, then latency)."""

    name = "policy"

    def __init__(self, policy: Any) -> None:
        self._policy = policy

    def order(self, decision, providers, health=None) -> tuple[str, ...]:
        health = health or {}
        allowed = self._allowed(decision, providers)

        if decision.chain:
            chain = [n for n in decision.chain if n in allowed]
            # A hop known to be down is demoted, not dropped: it may have
            # recovered since the last check, and trying IS the health check.
            chain.sort(key=lambda n: 0 if health.get(n, True) else 1)
            if chain:
                return tuple(chain)

        allowed.sort(
            key=lambda n: self._policy.sort_key(
                providers[n], healthy=health.get(n, True)
            )
        )
        return tuple(allowed)


class CheapestFirstStrategy(RoutingStrategy):
    """Cost-crunch mode: ignore the chain, always take the cheapest allowed
    provider first. Demonstrates that swapping one object changes routing."""

    name = "cheapest-first"

    def order(self, decision, providers, health=None) -> tuple[str, ...]:
        health = health or {}
        allowed = self._allowed(decision, providers)
        allowed.sort(
            key=lambda n: (
                0 if health.get(n, True) else 1,
                providers[n].cost_per_1k_tokens,
            )
        )
        return tuple(allowed)


class LocalFirstStrategy(RoutingStrategy):
    """Privacy-maximising mode: on-prem always first, cloud only as a last
    resort, even when policy would permit cloud."""

    name = "local-first"

    def order(self, decision, providers, health=None) -> tuple[str, ...]:
        health = health or {}
        allowed = self._allowed(decision, providers)
        allowed.sort(
            key=lambda n: (
                0 if providers[n].zone == "onprem" else 1,
                0 if health.get(n, True) else 1,
                providers[n].cost_per_1k_tokens,
            )
        )
        return tuple(allowed)


# --------------------------------------------------------------------------
# Walking the chain
# --------------------------------------------------------------------------


async def execute_with_fallback(
    order: Sequence[str],
    providers: Mapping[str, ModelProvider],
    messages: list[Message],
    *,
    deadline_s: float = 40.0,
    **opts: Any,
) -> RoutingResult:
    """Attempt each provider in turn until one succeeds.

    Two rules that make this a decision rather than a blind retry loop:

      * ProviderRejected STOPS the chain. The request itself is bad, so the
        next provider fails identically. Three hops at a 120s timeout is six
        minutes spent delivering an error that was knowable at hop one.
      * A total DEADLINE caps the whole chain, not each hop. Without it, p99
        latency under partial failure is unbounded and a queue builds behind
        it.
    """
    hops: list[FallbackHop] = []
    started = time.perf_counter()

    def elapsed_ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    if not order:
        return RoutingResult(
            refused=True,
            reason="no providers to attempt",
            elapsed_ms=elapsed_ms(),
        )

    for index, name in enumerate(order):
        remaining = deadline_s - (time.perf_counter() - started)
        if remaining <= 0:
            # Record WHY the rest were not tried. A silent stop is
            # indistinguishable from a chain that was never that long.
            for skipped in order[index:]:
                hops.append(
                    FallbackHop(
                        provider=skipped,
                        outcome=SKIPPED,
                        error=f"chain deadline {deadline_s}s exhausted",
                    )
                )
            break

        provider = providers.get(name)
        if provider is None:
            hops.append(
                FallbackHop(name, SKIPPED, error="provider not built")
            )
            continue

        hop_started = time.perf_counter()
        try:
            # The budget must bound the CALL, not just the decision to start
            # one. Checking only before each hop lets a single slow provider
            # blow the deadline arbitrarily - measured 55s against a 40s
            # budget before this was added.
            completion = await asyncio.wait_for(
                provider.generate(messages, **opts), timeout=remaining
            )

        except (asyncio.TimeoutError, TimeoutError):
            hops.append(
                FallbackHop(
                    name,
                    UNAVAILABLE,
                    int((time.perf_counter() - hop_started) * 1000),
                    f"hop exceeded remaining chain budget ({remaining:.1f}s)",
                )
            )
            continue

        except ProviderRejected as exc:
            hops.append(
                FallbackHop(
                    name,
                    REJECTED,
                    int((time.perf_counter() - hop_started) * 1000),
                    str(exc),
                )
            )
            # STOP. Retrying a malformed request elsewhere fails the same way
            # and buries the real error under later timeouts.
            return RoutingResult(
                hops=tuple(hops),
                refused=True,
                reason=f"request rejected by {name}: {exc}",
                elapsed_ms=elapsed_ms(),
            )

        except ProviderUnavailable as exc:
            hops.append(
                FallbackHop(
                    name,
                    UNAVAILABLE,
                    int((time.perf_counter() - hop_started) * 1000),
                    str(exc),
                )
            )
            continue

        except ProviderError as exc:  # pragma: no cover - defensive
            hops.append(
                FallbackHop(
                    name,
                    UNAVAILABLE,
                    int((time.perf_counter() - hop_started) * 1000),
                    f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        hops.append(FallbackHop(name, OK, completion.latency_ms))
        return RoutingResult(
            completion=completion,
            provider_used=name,
            hops=tuple(hops),
            elapsed_ms=elapsed_ms(),
        )

    exhausted = all(h.outcome == SKIPPED for h in hops[-1:]) and any(
        h.outcome == SKIPPED for h in hops
    )
    return RoutingResult(
        hops=tuple(hops),
        refused=True,
        reason=(
            f"chain deadline {deadline_s}s exhausted"
            if exhausted
            else "all providers in the chain were unavailable"
        ),
        elapsed_ms=elapsed_ms(),
    )
