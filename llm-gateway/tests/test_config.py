"""Startup validation and the Factory guarantee.

SC-14. See SCENARIOS.md.
"""

from __future__ import annotations

import pytest

from factory import (
    ConfigError,
    _BUILDERS,
    build_all,
    build_provider,
    expand_env,
    load_config,
    supported_types,
    validate_config,
)


@pytest.mark.scenario("SC-14")
@pytest.mark.reliability
def test_the_shipped_config_is_valid(real_config, providers):
    """
    GIVEN the config.yaml that ships with the repo
    WHEN  it is loaded and built
    THEN  every declared provider is constructed
    AND   zones and costs are as declared
    """
    assert real_config["default_provider"] in real_config["providers"]
    assert set(providers) == {"local", "guard", "claude"}
    assert providers["local"].zone == "onprem"
    assert providers["local"].cost_per_1k_tokens == 0.0
    assert providers["claude"].zone == "cloud"
    assert providers["claude"].cost_per_1k_tokens > 0, (
        "a zero-cost cloud provider makes the routing decision meaningless"
    )


@pytest.mark.scenario("SC-14")
@pytest.mark.reliability
def test_bad_config_reports_every_problem_at_once():
    """
    GIVEN a config with FOUR separate faults
    WHEN  it is validated
    THEN  startup fails
    AND   ALL four are reported together, not just the first
    AND   the unknown type is named alongside the supported ones

    One typo per restart costs a minute each on this hardware.
    """
    broken = {
        "default_provider": "nope",
        "providers": {
            "a": {"type": "ollma", "model": "x"},                       # typo
            "b": {"type": "ollama"},                                    # no model
            "c": {"type": "ollama", "model": "x", "zone": "orbit"},     # bad zone
            "d": {"type": "ollama", "model": "x", "cost_per_1k": -1},   # negative
        },
    }

    with pytest.raises(ConfigError) as exc:
        validate_config(broken)

    msg = str(exc.value)
    assert "'ollma'" in msg and "ollama" in msg   # names it AND the supported set
    assert "missing 'model'" in msg
    assert "orbit" in msg
    assert "negative" in msg
    assert "nope" in msg
    assert msg.count("- ") >= 5, "validation stopped at the first problem"


@pytest.mark.scenario("SC-14")
@pytest.mark.reliability
def test_unknown_provider_type_is_refused_with_a_useful_message():
    """
    GIVEN a provider type the factory cannot build
    WHEN  it is constructed
    THEN  it is refused, listing what IS supported
    """
    with pytest.raises(ConfigError) as exc:
        build_provider("x", {"type": "bedrock", "model": "m"})
    assert "bedrock" in str(exc.value)
    assert "supported" in str(exc.value)


@pytest.mark.scenario("SC-09")
@pytest.mark.architecture
def test_adding_a_vendor_is_additive(monkeypatch):
    """
    GIVEN a new provider type registered in the builders table
    WHEN  it is built
    THEN  it constructs successfully
    AND   build_provider() was never modified

    This is the Factory claim, proven rather than asserted: the dispatch
    function is closed to modification and the registry is open to extension.
    """
    from providers import OllamaProvider

    before = supported_types()
    monkeypatch.setitem(
        _BUILDERS, "fakevendor", lambda name, c: OllamaProvider(
            name=name, model=c["model"]
        )
    )

    built = build_provider("newguy", {"type": "fakevendor", "model": "test:1b"})

    assert built.name == "newguy"
    assert len(supported_types()) == len(before) + 1


@pytest.mark.scenario("SC-14")
@pytest.mark.operability
def test_one_config_file_serves_host_and_container(monkeypatch):
    """
    GIVEN a config value written as ${OLLAMA_BASE_URL:-http://localhost:11434}
    WHEN  the variable is unset
    THEN  the default applies (running on the host)
    WHEN  the variable is set
    THEN  it wins (running in Compose, where Ollama is outside the network)

    One file, two environments — no per-environment copies to drift apart.
    """
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    assert expand_env("${OLLAMA_BASE_URL:-http://localhost:11434}") == (
        "http://localhost:11434"
    )

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
    assert expand_env("${OLLAMA_BASE_URL:-http://localhost:11434}") == (
        "http://host.docker.internal:11434"
    )

    # non-strings and plain strings are untouched
    assert expand_env(0.003) == 0.003
    assert expand_env("llama3.2:3b") == "llama3.2:3b"


@pytest.mark.scenario("SC-14")
@pytest.mark.security
def test_no_credential_is_stored_in_config(real_config):
    """
    GIVEN the shipped configuration
    WHEN  it is inspected for secrets
    THEN  no provider block carries an api key

    The Anthropic adapter reads ANTHROPIC_API_KEY from the environment. A
    config file holding a credential is a config file you cannot commit.
    """
    credential_fields = {
        "api_key", "apikey", "key", "secret", "password", "passwd",
        "credential", "credentials", "auth", "auth_token", "access_token",
        "bearer", "token",
    }
    for name, cfg in real_config["providers"].items():
        offending = credential_fields & {k.lower() for k in cfg}
        assert not offending, f"provider {name} carries credential field(s): {offending}"
        # and no value looks like a key that leaked in
        for key, value in cfg.items():
            if isinstance(value, str):
                assert not value.startswith("sk-"), (
                    f"provider {name}.{key} looks like an API key"
                )
