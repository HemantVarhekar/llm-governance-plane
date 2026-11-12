"""Policy decisions — the governance guarantees.

SC-01, SC-02, SC-03, SC-04. See SCENARIOS.md.
"""

from __future__ import annotations

import pytest

from policy import Policy, PolicyError

# PII samples used for the parametrised recall test. Each entry is
# (label, payload, must_be_detected). The 'False' rows are the KNOWN GAP:
# regex cannot do named-entity recognition, which is why Presidio is next.
PII_SAMPLES = [
    pytest.param("My account 8841-22719 is broken", True, id="account-number"),
    pytest.param("Call me on 214-555-0183", True, id="phone-dashes"),
    pytest.param("Reach me at 214.555.0183", True, id="phone-dots"),
    pytest.param("Write to robert.chen@example.com", True, id="email"),
    pytest.param("My SSN is 123-45-6789", True, id="ssn"),
    pytest.param("Card 4111 1111 1111 1111 was charged", True, id="credit-card"),
    pytest.param("Account 8841-22719, call 214-555-0183", True, id="account+phone"),
    pytest.param(
        "Hi, this is Robert Chen speaking", True,
        id="person-name-KNOWN-GAP",
        marks=pytest.mark.xfail(
            reason="KNOWN GAP: regex cannot do NER. Presidio closes this.",
            strict=True,
        ),
    ),
]

CLEAN_SAMPLES = [
    pytest.param("What channels are in the sports package?", id="question"),
    pytest.param("My stream keeps buffering since I switched", id="complaint"),
    pytest.param("How much does the premium tier cost?", id="pricing"),
    pytest.param("I have 4 buffering events in 10 minutes", id="number-not-pii"),
]


# --------------------------------------------------------------------------


@pytest.mark.scenario("SC-01")
@pytest.mark.governance
def test_pii_payload_never_routes_to_cloud(policy, providers, classifier, pii_text):
    """
    GIVEN a payload containing a customer account number and phone number
    WHEN  the gateway decides which providers may serve it
    THEN  no provider in the cloud zone appears in the allowlist
    AND   the audit record names the rule that excluded it
    AND   the request is still served, on-prem
    """
    # GIVEN / WHEN
    decision = policy.decide(
        text=pii_text, task="support_draft",
        providers=providers, classifier=classifier,
    )

    # THEN — no cloud provider survived the filter
    cloud = [n for n in decision.providers_allowed if providers[n].zone == "cloud"]
    assert cloud == [], f"cloud providers leaked into the allowlist: {cloud}"
    assert "claude" not in decision.chain

    # AND — the audit record names the rule
    assert decision.excluded["claude"] == "pii_stays_onprem"
    assert "pii_stays_onprem" in decision.constraints_fired

    # AND — still served
    assert decision.refused is False
    assert decision.chain, "request was permitted but no provider was offered"


@pytest.mark.scenario("SC-01")
@pytest.mark.governance
@pytest.mark.parametrize("payload,expected", PII_SAMPLES)
def test_pii_detection_recall(policy, providers, classifier, payload, expected):
    """
    GIVEN a payload from the PII sample set
    WHEN  it is classified
    THEN  personal data is detected
    AND   no cloud provider is permitted

    One xfail is deliberate and strict: regex cannot do named-entity
    recognition. The gap is measured, not hidden.
    """
    decision = policy.decide(
        text=payload, task="support_draft",
        providers=providers, classifier=classifier,
    )
    assert decision.classification.has_pii is expected
    assert "claude" not in decision.providers_allowed


@pytest.mark.scenario("SC-02")
@pytest.mark.cost
@pytest.mark.parametrize("payload", CLEAN_SAMPLES)
def test_clean_payload_may_use_cloud(policy, providers, classifier, payload):
    """
    GIVEN a payload containing no personal data
    WHEN  the gateway decides which providers may serve it
    THEN  all providers including cloud are permitted
    AND   no constraint fired
    AND   the configured chain is honoured
    """
    decision = policy.decide(
        text=payload, task="support_draft",
        providers=providers, classifier=classifier,
    )
    assert decision.classification.has_pii is False
    assert decision.constraints_fired == ()
    assert "claude" in decision.providers_allowed
    assert decision.chain[0] == "claude", "the chain's stated preference was ignored"


@pytest.mark.scenario("SC-03")
@pytest.mark.governance
def test_fails_closed_when_classifier_is_down(
    policy, providers, broken_classifier, clean_text
):
    """
    GIVEN the PII classifier is unavailable
    AND   a payload that contains no detectable personal data
    WHEN  the gateway decides which providers may serve it
    THEN  cloud is refused anyway
    AND   the subscriber is still served on-prem

    We did not DETECT PII. We were unable to PROVE its absence. The rule reads
    classifier_healthy, not has_pii, and that distinction is the whole
    fail-closed argument.
    """
    decision = policy.decide(
        text=clean_text, task="support_draft",
        providers=providers, classifier=broken_classifier,
    )

    assert decision.classification.has_pii is False          # nothing found...
    assert decision.classification.classifier_healthy is False  # ...because blind
    assert "fail_closed_when_classifier_down" in decision.constraints_fired
    assert "claude" not in decision.providers_allowed
    assert decision.refused is False, "the subscriber should still be served"
    assert decision.chain == ("local",)


@pytest.mark.scenario("SC-04")
@pytest.mark.governance
def test_refuses_rather_than_degrading_to_cloud(
    policy, providers, classifier, pii_text
):
    """
    GIVEN a payload containing personal data
    AND   every on-prem provider is unavailable
    AND   a cloud provider IS available, healthy and faster
    WHEN  the gateway decides which providers may serve it
    THEN  the request is REFUSED
    AND   the cloud provider is never offered
    AND   the reason names the rule

    THE LIVE-KILL MOMENT. This is the difference between a preference and a
    guarantee: the cloud is up and would answer, and we fail the request.
    """
    # GIVEN — only the cloud provider remains
    cloud_only = {"claude": providers["claude"]}

    # WHEN
    decision = policy.decide(
        text=pii_text, task="support_draft",
        providers=cloud_only, classifier=classifier,
    )

    # THEN
    assert decision.refused is True
    assert decision.providers_allowed == ()
    assert decision.chain == ()
    assert "pii_stays_onprem" in (decision.reason or "")


@pytest.mark.scenario("SC-04")
@pytest.mark.governance
def test_refusal_is_a_value_not_an_exception(
    policy, providers, classifier, pii_text
):
    """
    GIVEN a request that policy must refuse
    WHEN  the decision is taken
    THEN  it RETURNS a decision object rather than raising

    Deliberate: the refusal is the single most important event to audit, and
    an exception can escape the path that writes the record.
    """
    decision = policy.decide(
        text=pii_text, task="support_draft",
        providers={"claude": providers["claude"]}, classifier=classifier,
    )
    assert decision.refused is True
    assert decision.as_dict()["refused"] is True  # serialisable for the audit log


@pytest.mark.scenario("SC-12")
@pytest.mark.governance
def test_decision_carries_its_own_evidence(policy, providers, classifier, pii_text):
    """
    GIVEN any policy decision
    WHEN  it is serialised for the audit trail
    THEN  it names which rules fired, what was excluded and why
    """
    rec = policy.decide(
        text=pii_text, task="support_draft",
        providers=providers, classifier=classifier,
    ).as_dict()

    assert rec["constraints_fired"] == ["pii_stays_onprem"]
    assert rec["excluded"] == {"claude": "pii_stays_onprem"}
    assert rec["providers_before"] == ["local", "guard", "claude"]
    assert rec["classification"]["entities"]


@pytest.mark.scenario("SC-14")
@pytest.mark.reliability
def test_policy_rejects_an_expression_language():
    """
    GIVEN a constraint using a condition key that is not supported
    WHEN  the policy is constructed
    THEN  it is rejected, naming the unsupported key and the supported set

    The no-DSL decision made structural: nobody can quietly grow an
    expression language inside the governance file.
    """
    with pytest.raises(PolicyError) as exc:
        Policy({
            "constraints": [
                {"name": "x", "when": {"tenant_tier": "gold"},
                 "require_zone": "onprem"}
            ],
            "chains": {"default": ["local"]},
        })
    assert "tenant_tier" in str(exc.value)
    assert "has_pii" in str(exc.value)


@pytest.mark.scenario("SC-14")
@pytest.mark.reliability
def test_policy_refuses_to_be_configured_to_degrade():
    """
    GIVEN a policy configured to fall back to cloud when nothing is permitted
    WHEN  the policy is constructed
    THEN  it is rejected

    The unsafe option is not merely absent — it is actively refused.
    """
    with pytest.raises(PolicyError) as exc:
        Policy({
            "constraints": [],
            "chains": {"default": ["local"]},
            "on_no_allowed_provider": "fallback_to_cloud",
        })
    assert "never acceptable" in str(exc.value)


@pytest.mark.scenario("SC-02")
@pytest.mark.cost
def test_health_outranks_cost_in_selection(policy, providers):
    """
    GIVEN a free provider that is DOWN and a paid provider that is UP
    WHEN  the selection sort key is applied
    THEN  the healthy paid provider sorts first

    A provider that is down is never actually cheap.
    """
    dead_free = policy.sort_key(providers["local"], healthy=False)
    live_paid = policy.sort_key(providers["claude"], healthy=True)
    assert live_paid < dead_free

    # AND when both are healthy, cost decides
    assert policy.sort_key(providers["local"], healthy=True) < live_paid
