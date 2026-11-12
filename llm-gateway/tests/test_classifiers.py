"""Pluggable classifiers and the composite guarantee.

SC-17, SC-18. See SCENARIOS.md.

These use a FAKE presidio so they run without the service. The real service
is exercised by evals/compare.py, which is where realism belongs.
"""

from __future__ import annotations

import pytest

from classifiers.composite import CompositeClassifier
from factory import ConfigError, build_classifier, supported_classifiers
from policy import Classification, Policy, RegexClassifier


class FakePresidio:
    """Stands in for the NER service: finds names, misses structured ids."""

    name = "presidio"

    def __init__(self, *, healthy: bool = True) -> None:
        self._healthy = healthy

    def health(self) -> bool:
        return self._healthy

    def classify(self, text: str) -> Classification:
        if not self._healthy:
            # Found nothing BECAUSE it could not look.
            return Classification(False, (), classifier_healthy=False)
        found = tuple(
            e for e, needle in (("PERSON", "Robert Chen"), ("LOCATION", "Dallas"))
            if needle in text
        )
        return Classification(bool(found), found, classifier_healthy=True)


ACCOUNT = "account 8841-22719"
NAME = "Robert Chen"
BOTH = f"{NAME}, {ACCOUNT}, keeps buffering"


# --------------------------------------------------------------------------


@pytest.mark.scenario("SC-17")
@pytest.mark.architecture
def test_classifier_is_plug_and_play_from_config():
    """
    GIVEN a classifier type named in config
    WHEN  it is built
    THEN  the right implementation is constructed
    AND   an unknown type is refused, listing what is supported

    The SAME factory pattern as providers, applied to the compliance layer.
    """
    assert supported_classifiers() == ["composite", "presidio", "regex"]
    assert type(build_classifier({"type": "regex"})).__name__ == "RegexClassifier"
    assert type(build_classifier({"type": "presidio"})).__name__ == "PresidioClassifier"

    with pytest.raises(ConfigError) as exc:
        build_classifier({"type": "nope"})
    assert "supported" in str(exc.value)


@pytest.mark.scenario("SC-17")
@pytest.mark.quality
def test_neither_classifier_alone_covers_a_real_payload():
    """
    GIVEN a payload containing BOTH a customer name and an account number
    WHEN  each classifier inspects it alone
    THEN  regex finds the account but NOT the name
    AND   presidio finds the name but NOT the account
    AND   only the composite finds both

    This is the empirical case for composing, not a design preference.
    """
    regex_only = RegexClassifier().classify(BOTH)
    assert "ACCOUNT_NUMBER" in regex_only.entities
    assert "PERSON" not in regex_only.entities

    ner_only = FakePresidio().classify(BOTH)
    assert "PERSON" in ner_only.entities
    assert "ACCOUNT_NUMBER" not in ner_only.entities

    both = CompositeClassifier([RegexClassifier(), FakePresidio()]).classify(BOTH)
    assert {"ACCOUNT_NUMBER", "PERSON"} <= set(both.entities)


@pytest.mark.scenario("SC-18")
@pytest.mark.governance
def test_composite_reports_unhealthy_when_any_member_is_down():
    """
    GIVEN a composite whose NER member is down
    AND   whose regex member is still working and still finding PII
    WHEN  it classifies
    THEN  it reports UNHEALTHY

    The dangerous bug this prevents: reporting healthy because "one of them
    still works". We can no longer prove the ABSENCE of names, so the
    fail-closed rule must fire even though regex is fine.
    """
    comp = CompositeClassifier([RegexClassifier(), FakePresidio(healthy=False)])

    result = comp.classify(BOTH)

    assert "ACCOUNT_NUMBER" in result.entities, "regex should still contribute"
    assert result.has_pii is True
    assert result.classifier_healthy is False, "a partial classifier claimed health"
    assert comp.health() is False


@pytest.mark.scenario("SC-18")
@pytest.mark.governance
def test_degraded_composite_forces_onprem_even_for_clean_text(policy, providers):
    """
    GIVEN the NER member is down
    AND   a payload with no detectable PII
    WHEN  policy decides
    THEN  the cloud is refused

    End to end: a half-working classifier must not permit cloud egress.
    """
    comp = CompositeClassifier([RegexClassifier(), FakePresidio(healthy=False)])

    decision = policy.decide(
        text="What channels are in the sports package?",
        task="support_draft", providers=providers, classifier=comp,
    )

    assert decision.classification.has_pii is False
    assert decision.classification.classifier_healthy is False
    assert "fail_closed_when_classifier_down" in decision.constraints_fired
    assert "claude" not in decision.providers_allowed


@pytest.mark.scenario("SC-18")
@pytest.mark.governance
def test_a_member_that_raises_counts_as_not_having_looked():
    """
    GIVEN a composite member whose classify() throws
    WHEN  it classifies
    THEN  the composite reports unhealthy rather than swallowing the error
    """

    class Exploding:
        name = "exploding"

        def health(self) -> bool:
            return True

        def classify(self, text: str) -> Classification:
            raise RuntimeError("boom")

    result = CompositeClassifier([RegexClassifier(), Exploding()]).classify(BOTH)
    assert result.classifier_healthy is False


@pytest.mark.scenario("SC-17")
@pytest.mark.quality
def test_presidio_client_fails_closed_when_service_is_unreachable():
    """
    GIVEN the Presidio service is not running
    WHEN  the client classifies
    THEN  it reports classifier_healthy=False
    AND   it does NOT report "no PII found" as though it had looked

    The easiest and most dangerous thing to get wrong in this whole system.
    """
    from classifiers.presidio_client import PresidioClassifier

    dead = PresidioClassifier(base_url="http://127.0.0.1:59998", timeout_s=1.0)

    assert dead.health() is False
    result = dead.classify("Robert Chen, account 8841-22719")
    assert result.classifier_healthy is False
    assert result.has_pii is False      # found nothing...
    assert result.entities == ()        # ...because it could not look
