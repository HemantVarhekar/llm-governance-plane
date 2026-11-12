"""End-to-end behaviour through the HTTP surface.

SC-08, SC-10, SC-11, SC-12, SC-13, SC-15. See SCENARIOS.md.

These run against the REAL pipeline with FAKE providers, so they exercise
auth, policy, routing, the audit sink and metrics without waiting on a model.
"""

from __future__ import annotations

import pytest

CLEAN = "What channels are in the sports package?"
PII = "My account 8841-22719 keeps buffering, call me on 214-555-0183"


def chat(client, headers, content, **extra):
    body = {"task": "support_draft", "messages": [{"role": "user", "content": content}]}
    body.update(extra)
    return client.post("/v1/chat/completions", json=body, headers=headers)


# --------------------------------------------------------------------------


@pytest.mark.scenario("SC-10")
@pytest.mark.security
def test_unauthenticated_request_is_rejected(api):
    """
    GIVEN the gateway is configured with an API key
    WHEN  a request arrives with no token
    THEN  it is rejected with 401
    WHEN  a request arrives with a wrong token
    THEN  it is rejected with 403

    401 and 403 are different findings: "you did not authenticate" versus
    "you authenticated as someone not permitted".
    """
    client, _ = api

    assert client.get("/ledger").status_code == 401
    assert client.get(
        "/ledger", headers={"Authorization": "Bearer wrong"}
    ).status_code == 403
    assert client.get(
        "/ledger", headers={"Authorization": "Bearer test-key"}
    ).status_code == 200


@pytest.mark.scenario("SC-10")
@pytest.mark.security
def test_healthz_stays_open_and_declares_auth_state(api):
    """
    GIVEN a gateway with auth enabled
    WHEN  /healthz is called with no credential
    THEN  it answers
    AND   it reports that auth is enabled

    Liveness probes must not need a credential — and an unauthenticated
    gateway must never be a SILENT condition.
    """
    client, _ = api
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["auth_enabled"] is True
    # /healthz names the guards that are actually running, so an unguarded
    # gateway can never be a silent condition either.
    assert "injection" in body["guardrails"]["input"]
    assert "format" in body["guardrails"]["output"]


@pytest.mark.scenario("SC-08")
@pytest.mark.security
def test_caller_cannot_force_cloud_for_a_pii_payload(api, auth_headers):
    """
    GIVEN a payload containing personal data
    WHEN  the caller explicitly asks for the cloud provider by name
    THEN  the preference is IGNORED
    AND   the request is served on-prem
    AND   the audit record shows the provider was excluded by rule

    Governance a caller can bypass by setting a field is not governance.
    """
    client, _ = api

    r = chat(client, auth_headers, PII, model="claude")

    assert r.status_code == 200
    body = r.json()
    assert body["zone"] == "onprem"
    assert body["provider"] != "claude"
    assert body["migrationguard"]["excluded"]["claude"] == "pii_stays_onprem"
    assert "claude" not in body["migrationguard"]["attempt_order"]


@pytest.mark.scenario("SC-08")
@pytest.mark.cost
def test_caller_preference_is_honoured_when_policy_permits(api, auth_headers):
    """
    GIVEN a clean payload
    WHEN  the caller asks for the cloud provider by name
    THEN  the request is served by cloud

    The preference narrows WITHIN the allowlist; it is only overridden when a
    constraint says so.
    """
    client, _ = api
    r = chat(client, auth_headers, CLEAN, model="claude")
    assert r.status_code == 200
    assert r.json()["provider"] == "claude"
    assert r.json()["zone"] == "cloud"


@pytest.mark.scenario("SC-04")
@pytest.mark.governance
def test_refuses_with_403_when_no_onprem_provider_remains(api, auth_headers):
    """
    GIVEN a payload containing personal data
    AND   every on-prem provider has been removed
    AND   the cloud provider is healthy and would answer
    WHEN  the request is made
    THEN  it is refused with 403
    AND   the cloud provider is never called

    THE LIVE-KILL MOMENT, as an assertion.
    """
    client, app = api
    cloud = app.state.providers["claude"]
    app.state.providers = {"claude": cloud}
    app.state.health = {"claude": True}

    r = chat(client, auth_headers, PII)

    assert r.status_code == 403
    assert cloud.calls == 0, "the cloud provider was called for a PII payload"
    detail = r.json()["detail"]
    assert "pii_stays_onprem" in str(detail)


@pytest.mark.scenario("SC-05")
@pytest.mark.resilience
def test_cloud_failure_degrades_to_onprem_over_http(api, auth_headers):
    """
    GIVEN a clean payload whose chain prefers cloud
    AND   the cloud provider is failing
    WHEN  the request is made
    THEN  the caller still receives 200
    AND   the response is flagged degraded with both hops recorded
    """
    client, app = api
    app.state.providers["claude"].behaviour = "unavailable"

    r = chat(client, auth_headers, CLEAN)

    assert r.status_code == 200
    mg = r.json()["migrationguard"]
    assert mg["degraded"] is True
    assert [h["outcome"] for h in mg["hops"]] == ["unavailable", "ok"]
    assert r.json()["zone"] == "onprem"


@pytest.mark.scenario("SC-06")
@pytest.mark.resilience
def test_provider_rejection_surfaces_as_400_not_500(api, auth_headers):
    """
    GIVEN the only provider rejects the request as malformed
    WHEN  the request is made
    THEN  the caller receives 400, not 503 and not 500

    Status codes must distinguish "your request is wrong" from "we are out of
    capacity", or a load test cannot tell policy from saturation.
    """
    client, app = api
    app.state.providers["local"].behaviour = "rejected"
    app.state.providers = {"local": app.state.providers["local"]}
    app.state.health = {"local": True}

    r = chat(client, auth_headers, PII)
    assert r.status_code == 400


@pytest.mark.scenario("SC-11")
@pytest.mark.performance
def test_over_capacity_sheds_with_429(api, auth_headers):
    """
    GIVEN the gateway is already at its in-flight limit
    WHEN  another request arrives
    THEN  it is shed with 429 and a Retry-After header
    AND   the in-flight gauge is restored afterwards

    Unbounded concurrency on two cores means memory growth and uniform
    timeouts. Shedding gives a real saturation point.
    """
    client, app = api
    import main as main_module

    app.state.inflight = main_module.MAX_INFLIGHT  # simulate saturation

    r = chat(client, auth_headers, CLEAN)

    assert r.status_code == 429
    assert r.headers.get("Retry-After") == "5"
    assert "X-Request-Id" in r.headers

    app.state.inflight = 0
    assert chat(client, auth_headers, CLEAN).status_code == 200


@pytest.mark.scenario("SC-12")
@pytest.mark.governance
def test_every_request_produces_exactly_one_audit_event(api, auth_headers):
    """
    GIVEN a successful request, a refused request and a shed request
    WHEN  the audit trail is read
    THEN  each produced exactly one event
    AND   the refusal is present

    The most important event is the one where something was STOPPED.
    """
    client, app = api
    import main as main_module

    before = len(client.get("/audit?limit=500", headers=auth_headers).json()["events"])

    chat(client, auth_headers, CLEAN)                       # ok
    app.state.providers = {"claude": app.state.providers["claude"]}
    app.state.health = {"claude": True}
    chat(client, auth_headers, PII)                         # refused 403
    app.state.inflight = main_module.MAX_INFLIGHT
    chat(client, auth_headers, CLEAN)                       # shed 429
    app.state.inflight = 0

    events = client.get("/audit?limit=500", headers=auth_headers).json()["events"]
    assert len(events) == before + 3, "an event was lost"

    outcomes = [e["outcome"] for e in events[-3:]]
    assert "refused" in outcomes
    assert "shed" in outcomes


@pytest.mark.scenario("SC-12")
@pytest.mark.governance
def test_audit_can_prove_no_pii_reached_the_cloud(api, auth_headers):
    """
    GIVEN a mix of clean and PII traffic
    WHEN  the audit trail is filtered to excluded decisions
    THEN  every PII request shows cloud excluded by name of the rule
    AND   no event shows a cloud zone for a PII payload

    This is the query you run when someone asks you to prove it.
    """
    client, _ = api
    chat(client, auth_headers, CLEAN)
    chat(client, auth_headers, PII)
    chat(client, auth_headers, PII)

    data = client.get(
        "/audit?limit=500&only_excluded=true", headers=auth_headers
    ).json()

    assert data["count"] >= 2
    for e in data["events"]:
        assert e["policy_decisions"]["excluded"]["claude"] == "pii_stays_onprem"
        assert e["zone"] != "cloud"


@pytest.mark.scenario("SC-13")
@pytest.mark.cost
def test_ledger_breaks_cost_down_by_zone(api, auth_headers):
    """
    GIVEN traffic served by both zones
    WHEN  the ledger is read
    THEN  calls, tokens and cost appear per zone
    AND   the cost avoided by staying on-prem is estimated
    """
    client, _ = api
    chat(client, auth_headers, CLEAN, model="claude")   # cloud, paid
    chat(client, auth_headers, PII)                     # on-prem, free

    led = client.get("/ledger", headers=auth_headers).json()

    assert led["by_zone"]["cloud"]["cost_usd"] > 0
    assert led["by_zone"]["onprem"]["cost_usd"] == 0.0
    assert led["estimated_cost_avoided_usd"] > 0
    assert led["cloud_rate_per_1k"] > 0


@pytest.mark.scenario("SC-13")
@pytest.mark.performance
def test_metrics_are_exposed_in_prometheus_format(api, auth_headers):
    """
    GIVEN some traffic has been served
    WHEN  /metrics is scraped
    THEN  counters, gauges and a latency histogram are present

    Without a server-side view, a load test only measures the client.
    """
    client, _ = api
    chat(client, auth_headers, CLEAN)

    body = client.get("/metrics", headers=auth_headers).text

    assert "mg_requests_total" in body
    assert "mg_request_duration_seconds_bucket" in body
    assert 'le="+Inf"' in body
    assert "mg_inflight" in body
    assert "mg_provider_up" in body


@pytest.mark.scenario("SC-15")
@pytest.mark.operability
def test_reload_applies_without_a_restart(api, auth_headers):
    """
    GIVEN a running gateway
    WHEN  reload is called
    THEN  it succeeds and reports before/after
    AND   the process was never restarted
    """
    client, app = api
    started_before = app.state.started

    r = client.post("/admin/reload", headers=auth_headers)

    assert r.status_code == 200
    assert r.json()["reloaded"] is True
    assert "before" in r.json() and "after" in r.json()
    assert app.state.started == started_before, "the process restarted"


@pytest.mark.scenario("SC-15")
@pytest.mark.operability
def test_a_bad_reload_is_rejected_and_the_old_config_survives(
    api, auth_headers, monkeypatch, tmp_path
):
    """
    GIVEN a configuration edit that is invalid
    WHEN  reload is called
    THEN  it is rejected with 400
    AND   the PREVIOUS configuration remains active

    A bad edit must never take down a running gateway.
    """
    client, app = api
    good = dict(app.state.config)

    bad = tmp_path / "broken.yaml"
    bad.write_text("default_provider: ghost\nproviders:\n  a: {type: nope}\n")
    monkeypatch.setenv("MG_CONFIG", str(bad))

    r = client.post("/admin/reload", headers=auth_headers)

    assert r.status_code == 400
    assert "rejected" in str(r.json()["detail"]).lower()
    assert app.state.config == good, "a bad reload clobbered the live config"
    assert client.get("/healthz").status_code == 200, "the gateway went down"


@pytest.mark.scenario("SC-04")
@pytest.mark.governance
def test_failure_response_carries_the_governance_decision(api, auth_headers):
    """
    GIVEN a PII payload and no reachable on-prem provider
    WHEN  the request fails
    THEN  the ERROR response still names the rule and what it excluded

    Without this, "we refused to use the cloud" is indistinguishable from
    "everything was down" — and that distinction is the entire guarantee.
    """
    client, app = api
    app.state.providers["local"].behaviour = "unavailable"
    app.state.providers["guard"].behaviour = "unavailable"

    r = chat(client, auth_headers, PII)

    assert r.status_code == 503
    mg = r.json()["detail"]["migrationguard"]
    assert mg["classification"]["has_pii"] is True
    assert mg["constraints_fired"] == ["pii_stays_onprem"]
    assert mg["excluded"]["claude"] == "pii_stays_onprem"
    assert "claude" not in mg["attempt_order"]
    assert app.state.providers["claude"].calls == 0


@pytest.mark.scenario("SC-16")
@pytest.mark.quality
def test_guardrail_detections_surface_in_the_response(api, auth_headers):
    """
    GIVEN a request that succeeds
    WHEN  the response is returned
    THEN  the guardrails that ran are listed, even with nothing detected

    "We looked and found nothing" must not look like "we never looked".
    """
    client, _ = api
    r = chat(client, auth_headers, CLEAN)
    assert r.status_code == 200
    guards = r.json()["migrationguard"]["guardrails"]
    assert isinstance(guards, list) and guards
    assert all("guard" in g and "allowed" in g for g in guards)
