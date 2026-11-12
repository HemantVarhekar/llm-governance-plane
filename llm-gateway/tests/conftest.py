"""Shared fixtures.

Design decision: the API tests use FAKE providers, not real models. A suite
that calls llama3.2:3b would take minutes and be non-deterministic. Real
models are exercised by demo/scenario.py, which is where realism belongs.
These tests prove the LOGIC.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# Make the gateway package importable without installing it.
GATEWAY_ROOT = Path(__file__).resolve().parents[1]
if str(GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(GATEWAY_ROOT))

from factory import build_all, load_config  # noqa: E402
from policy import Classification, Policy, RegexClassifier  # noqa: E402
from providers import (  # noqa: E402
    Completion,
    ModelProvider,
    ProviderRejected,
    ProviderUnavailable,
)

CONFIG_PATH = GATEWAY_ROOT / "config.yaml"
POLICY_PATH = GATEWAY_ROOT / "policy.yaml"


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeProvider(ModelProvider):
    """Deterministic stand-in. `behaviour` drives the failure mode."""

    def __init__(
        self,
        name: str,
        zone: str = "onprem",
        cost: float = 0.0,
        behaviour: str = "ok",
        delay: float = 0.0,
        healthy: bool = True,
    ) -> None:
        super().__init__(
            name=name, model=f"{name}-model", zone=zone, cost_per_1k_tokens=cost
        )
        self.behaviour = behaviour
        self.delay = delay
        self._healthy = healthy
        self.calls = 0

    async def generate(self, messages, **opts) -> Completion:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.behaviour == "unavailable":
            raise ProviderUnavailable(f"{self.name} is down", provider=self.name)
        if self.behaviour == "rejected":
            raise ProviderRejected(f"{self.name} says 400", provider=self.name)
        return Completion(
            text=f"answer from {self.name}",
            provider=self.name,
            model=self.model,
            latency_ms=int(self.delay * 1000),
            tokens_in=10,
            tokens_out=20,
        )

    async def health(self) -> bool:
        return self._healthy


class BrokenClassifier:
    """A classifier that is down. Finds nothing BECAUSE it cannot look."""

    def health(self) -> bool:
        return False

    def classify(self, text: str) -> Classification:
        return Classification(has_pii=False, entities=(), classifier_healthy=False)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

CLEAN_TEXT = "What channels are included in the sports package?"
PII_TEXT = "My account 8841-22719 keeps buffering, call me on 214-555-0183"


@pytest.fixture
def policy() -> Policy:
    return Policy.load(POLICY_PATH)


@pytest.fixture
def real_config() -> dict:
    return load_config(CONFIG_PATH)


@pytest.fixture
def providers(real_config) -> dict:
    """Providers built from the REAL config, so tests exercise the shipped
    zones and costs rather than invented ones."""
    return build_all(real_config)


@pytest.fixture
def classifier() -> RegexClassifier:
    return RegexClassifier()


@pytest.fixture
def broken_classifier() -> BrokenClassifier:
    return BrokenClassifier()


@pytest.fixture
def clean_text() -> str:
    return CLEAN_TEXT


@pytest.fixture
def pii_text() -> str:
    return PII_TEXT


@pytest.fixture
def fake_providers() -> dict[str, FakeProvider]:
    """A cloud + on-prem pair mirroring the real topology."""
    return {
        "claude": FakeProvider("claude", zone="cloud", cost=0.003),
        "local": FakeProvider("local", zone="onprem", cost=0.0),
        "guard": FakeProvider("guard", zone="onprem", cost=0.0),
    }


@pytest.fixture
def api(tmp_path, monkeypatch):
    """A TestClient over the real app, with FAKE providers swapped in.

    Keeps the suite fast and deterministic while still exercising the real
    pipeline, auth, metrics and audit sink.
    """
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MG_API_KEY", "test-key")
    monkeypatch.setenv("MG_CONFIG", str(CONFIG_PATH))
    monkeypatch.setenv("MG_POLICY", str(POLICY_PATH))
    monkeypatch.chdir(tmp_path)  # audit file lands in a temp dir

    import importlib

    import main as main_module

    importlib.reload(main_module)

    with TestClient(main_module.app) as client:
        app = main_module.app
        app.state.providers = {
            "claude": FakeProvider("claude", zone="cloud", cost=0.003),
            "local": FakeProvider("local", zone="onprem", cost=0.0),
            "guard": FakeProvider("guard", zone="onprem", cost=0.0),
        }
        app.state.health = {n: True for n in app.state.providers}

        # The classifier is faked too, for the same reason the providers
        # are, and this was a REAL GAP rather than tidiness.
        #
        # The composite classifier calls Presidio over HTTP. With Presidio
        # down the classifier reports unhealthy, the policy correctly fails
        # closed, nothing is allowed to route to cloud, and five tests
        # here fail with KeyError: 'cloud'. The gateway was behaving
        # perfectly; the tests had an undeclared dependency on a service
        # nobody had declared.
        #
        # Worse than failing in continuous integration: locally they
        # PASSED, but only because somebody happened to have Presidio
        # running. A test whose result depends on what else is running on
        # the machine is not evidence of anything.
        #
        # The composite classifier is exercised for real in
        # test_classifiers.py, which is where that belongs.
        app.state.classifier = RegexClassifier()
        app.state.classifier_name = app.state.classifier.name

        # Guards capture their provider at BUILD time, so swapping
        # app.state.providers alone leaves the safety guard still pointing at
        # the real llama-guard over HTTP — slow, and fail-closed blocks the
        # test when it is unreachable. Rebuild the chains against the fakes.
        from factory import build_guardrails
        from guardrails import GuardChain, IN, OUT, NoOpGuard

        cfg = app.state.config.get("guardrails") or {}
        app.state.guards_in = GuardChain(
            build_guardrails(cfg.get("input"), app.state.providers) or [NoOpGuard(IN)]
        )
        app.state.guards_out = GuardChain(
            build_guardrails(cfg.get("output"), app.state.providers) or [NoOpGuard(OUT)]
        )
        app.state.guard_names = {
            "input": [getattr(g, "name", type(g).__name__)
                      for g in app.state.guards_in.guards],
            "output": [getattr(g, "name", type(g).__name__)
                       for g in app.state.guards_out.guards],
        }
        yield client, app


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-key"}
