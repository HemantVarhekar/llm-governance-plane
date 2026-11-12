"""Routing, fallback and the error taxonomy.

SC-05, SC-06, SC-07, SC-09. See SCENARIOS.md.
"""

from __future__ import annotations

import time

import pytest

from policy import RegexClassifier
from routing import (
    CheapestFirstStrategy,
    LocalFirstStrategy,
    PolicyStrategy,
    execute_with_fallback,
)

MSGS = [{"role": "user", "content": "hello"}]


def run(coro):
    """Tiny runner so the suite needs no async plugin (one less dependency)."""
    import asyncio

    return asyncio.run(coro)


# --------------------------------------------------------------------------


@pytest.mark.scenario("SC-05")
@pytest.mark.resilience
def test_cloud_failure_falls_back_visibly(fake_providers):
    """
    GIVEN a chain that prefers cloud
    AND   the cloud provider is failing
    WHEN  the chain is executed
    THEN  the on-prem provider serves the request
    AND   BOTH hops are recorded
    AND   the result is flagged degraded

    Silent degradation is worse than failure, because you cannot see it.
    """
    fake_providers["claude"].behaviour = "unavailable"

    result = run(execute_with_fallback(
        ("claude", "local"), fake_providers, MSGS, deadline_s=10
    ))

    assert result.completion is not None
    assert result.provider_used == "local"
    assert [h.provider for h in result.hops] == ["claude", "local"]
    assert [h.outcome for h in result.hops] == ["unavailable", "ok"]
    assert result.degraded is True
    assert fake_providers["claude"].calls == 1, "cloud must actually be attempted"


@pytest.mark.scenario("SC-05")
@pytest.mark.resilience
def test_first_time_success_still_records_a_hop(fake_providers):
    """
    GIVEN a provider that succeeds immediately
    WHEN  the chain is executed
    THEN  one 'ok' hop is still recorded
    AND   the result is NOT flagged degraded

    Logging only failures makes "worked first time" and "never tried"
    indistinguishable in the audit log.
    """
    result = run(execute_with_fallback(("local",), fake_providers, MSGS))
    assert len(result.hops) == 1
    assert result.hops[0].outcome == "ok"
    assert result.degraded is False


@pytest.mark.scenario("SC-06")
@pytest.mark.resilience
def test_malformed_request_stops_the_chain(fake_providers):
    """
    GIVEN a provider that rejects the request as malformed (4xx)
    WHEN  the chain is executed
    THEN  the chain STOPS at the first rejection
    AND   no further provider is attempted
    AND   the ORIGINAL error is reported, not a later timeout

    Retrying a bad request across providers burns timeouts and buries the
    real cause.
    """
    fake_providers["claude"].behaviour = "rejected"

    result = run(execute_with_fallback(
        ("claude", "local"), fake_providers, MSGS, deadline_s=10
    ))

    assert result.refused is True
    assert result.completion is None
    assert len(result.hops) == 1, "the chain continued past a non-retryable error"
    assert fake_providers["local"].calls == 0, "a doomed retry was attempted"
    assert "400" in (result.reason or "")


@pytest.mark.scenario("SC-06")
@pytest.mark.resilience
def test_all_providers_unavailable_refuses_with_every_hop_logged(fake_providers):
    """
    GIVEN every provider in the chain is unavailable
    WHEN  the chain is executed
    THEN  the request is refused
    AND   every attempt is recorded
    """
    for p in fake_providers.values():
        p.behaviour = "unavailable"

    result = run(execute_with_fallback(
        ("claude", "local"), fake_providers, MSGS, deadline_s=10
    ))

    assert result.refused is True
    assert len(result.hops) == 2
    assert all(h.outcome == "unavailable" for h in result.hops)


@pytest.mark.scenario("SC-07")
@pytest.mark.performance
def test_deadline_bounds_total_latency(fake_providers):
    """
    GIVEN a provider slower than the entire chain budget
    WHEN  the chain runs with that budget
    THEN  the IN-FLIGHT call is aborted at the budget
    AND   total elapsed time does not exceed it
    AND   remaining providers are recorded as skipped, with the reason

    Checking the deadline only BETWEEN hops is not enough: a single slow
    provider would blow the budget arbitrarily. Measured 55s against a 40s
    budget before this was fixed.
    """
    fake_providers["claude"].delay = 10.0  # far longer than the budget

    started = time.perf_counter()
    result = run(execute_with_fallback(
        ("claude", "local"), fake_providers, MSGS, deadline_s=1.5
    ))
    elapsed = time.perf_counter() - started

    assert elapsed < 3.0, f"deadline did not bound latency: {elapsed:.1f}s"
    assert result.refused is True
    skipped = [h for h in result.hops if h.outcome == "skipped"]
    assert skipped, "the untried provider was not recorded"
    assert "deadline" in (skipped[0].error or "")
    assert fake_providers["local"].calls == 0


@pytest.mark.scenario("SC-09")
@pytest.mark.architecture
def test_strategies_order_differently(policy, providers, classifier, clean_text):
    """
    GIVEN three routing strategies
    WHEN  each orders the providers for the same clean request
    THEN  their orders differ

    Swapping one object changes routing behaviour. The handler is untouched.
    """
    decision = policy.decide(
        text=clean_text, task="support_draft",
        providers=providers, classifier=classifier,
    )

    by_policy = PolicyStrategy(policy).order(decision, providers)
    cheapest = CheapestFirstStrategy().order(decision, providers)
    local_first = LocalFirstStrategy().order(decision, providers)

    assert by_policy[0] == "claude", "the chain's preference was not honoured"
    assert providers[cheapest[0]].cost_per_1k_tokens == 0.0
    assert providers[local_first[0]].zone == "onprem"
    assert by_policy != cheapest


@pytest.mark.scenario("SC-09")
@pytest.mark.governance
@pytest.mark.parametrize(
    "strategy_name", ["policy", "cheapest-first", "local-first"]
)
def test_no_strategy_can_reach_an_excluded_provider(
    policy, providers, classifier, pii_text, strategy_name
):
    """
    GIVEN a payload containing personal data
    WHEN  ANY routing strategy orders the providers
    THEN  no cloud provider appears in the order

    The compliance guarantee does not depend on a strategy behaving well:
    excluded providers are never in its input.
    """
    decision = policy.decide(
        text=pii_text, task="support_draft",
        providers=providers, classifier=classifier,
    )
    strategy = {
        "policy": PolicyStrategy(policy),
        "cheapest-first": CheapestFirstStrategy(),
        "local-first": LocalFirstStrategy(),
    }[strategy_name]

    order = strategy.order(decision, providers)

    assert order, "a permitted request was left with nowhere to go"
    assert "claude" not in order
    assert all(providers[n].zone == "onprem" for n in order)


@pytest.mark.scenario("SC-09")
@pytest.mark.resilience
def test_known_down_provider_is_demoted_not_dropped(
    policy, providers, classifier, clean_text
):
    """
    GIVEN the cloud provider is known to be down
    WHEN  the policy strategy orders the chain
    THEN  it is moved to the back rather than removed

    It may have recovered since the last health sweep, and attempting the
    call IS the real health check.
    """
    decision = policy.decide(
        text=clean_text, task="support_draft",
        providers=providers, classifier=classifier,
    )
    order = PolicyStrategy(policy).order(
        decision, providers, health={"claude": False, "local": True}
    )
    assert order[0] == "local"
    assert "claude" in order, "a known-down provider was dropped, not demoted"
