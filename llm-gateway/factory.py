"""Factory — turns config data into live provider objects.

This is the bridge that makes config.yaml a CONTROL SURFACE rather than
documentation. Because of this module, main.py imports no vendor and names no
model: it calls build_all() and receives a registry. The "vendor-blind" claim
in BUSINESS_CASE.md is structurally true, not aspirational.

Adding a provider = one entry in _BUILDERS + one block in config.yaml.
The dispatch function below is never edited.

See ARCHITECTURE.md section 11 (Contracts / Pattern roles).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable

import yaml

from providers import AnthropicProvider, ModelProvider, OllamaProvider

ProviderConfig = dict[str, Any]
Builder = Callable[[str, ProviderConfig], ModelProvider]

VALID_ZONES = ("onprem", "cloud")


class ConfigError(Exception):
    """Raised at STARTUP for a bad config.

    Deliberately fatal: a typo must crash the boot with a clear message, not
    sit dormant until it produces a 500 on a customer request at 2am.
    """


# --------------------------------------------------------------------------
# Environment expansion
#
# Lets a single config.yaml work on the host AND inside Compose, where Ollama
# runs outside the container network and needs host.docker.internal.
# Syntax: ${VAR} or ${VAR:-default}
# --------------------------------------------------------------------------

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: Any) -> Any:
    """Substitute ${VAR} / ${VAR:-default} in strings. Other types pass through."""
    if not isinstance(value, str):
        return value
    return _ENV_PATTERN.sub(
        lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value
    )


def _expand_tree(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _expand_tree(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand_tree(v) for v in node]
    return expand_env(node)


# --------------------------------------------------------------------------
# The registry
#
# THE POINT OF THIS MODULE: adding a vendor appends a line here. It does not
# edit build_provider(), which is already tested and working.
# --------------------------------------------------------------------------


def _build_ollama(name: str, cfg: ProviderConfig) -> ModelProvider:
    return OllamaProvider(
        name=name,
        model=cfg["model"],
        base_url=cfg.get("base_url", "http://localhost:11434"),
        zone=cfg.get("zone", "onprem"),
        cost_per_1k_tokens=float(cfg.get("cost_per_1k", 0.0)),
        timeout_s=float(cfg.get("timeout_s", 120)),
        think_default=cfg.get("think", False),
    )


def _build_anthropic(name: str, cfg: ProviderConfig) -> ModelProvider:
    return AnthropicProvider(
        name=name,
        model=cfg["model"],
        max_tokens=int(cfg.get("max_tokens", 1024)),
        zone=cfg.get("zone", "cloud"),
        cost_per_1k_tokens=float(cfg.get("cost_per_1k", 0.0)),
        timeout_s=float(cfg.get("timeout_s", 120)),
        # api_key deliberately absent: the adapter reads ANTHROPIC_API_KEY.
        # A config file holding a credential is a config file you cannot commit.
    )


_BUILDERS: dict[str, Builder] = {
    "ollama": _build_ollama,
    "anthropic": _build_anthropic,
}


def supported_types() -> list[str]:
    """What the factory can build. Used in error messages and in tests."""
    return sorted(_BUILDERS)


# --------------------------------------------------------------------------
# Validation — all errors at once, not just the first
# --------------------------------------------------------------------------


def validate_config(config: dict[str, Any]) -> None:
    """Check the whole config and raise ONE error listing every problem.

    Reporting all problems together beats fixing one typo per restart, which
    on this hardware costs a minute each time.
    """
    problems: list[str] = []

    providers = config.get("providers")
    if not isinstance(providers, dict) or not providers:
        raise ConfigError("config must define a non-empty 'providers' mapping")

    for name, cfg in providers.items():
        where = f"providers.{name}"
        if not isinstance(cfg, dict):
            problems.append(f"{where}: must be a mapping")
            continue

        ptype = cfg.get("type")
        if not ptype:
            problems.append(f"{where}: missing 'type'")
        elif ptype not in _BUILDERS:
            problems.append(
                f"{where}: unknown type {ptype!r}, supported: {supported_types()}"
            )

        if not cfg.get("model"):
            problems.append(f"{where}: missing 'model'")

        zone = cfg.get("zone")
        if zone is not None and zone not in VALID_ZONES:
            problems.append(
                f"{where}: zone {zone!r} invalid, must be one of {list(VALID_ZONES)}"
            )

        cost = cfg.get("cost_per_1k")
        if cost is not None:
            try:
                if float(cost) < 0:
                    problems.append(f"{where}: cost_per_1k must not be negative")
            except (TypeError, ValueError):
                problems.append(f"{where}: cost_per_1k {cost!r} is not a number")

    default = config.get("default_provider")
    if not default:
        problems.append("missing 'default_provider'")
    elif default not in providers:
        problems.append(
            f"default_provider {default!r} is not in providers: {sorted(providers)}"
        )

    if problems:
        raise ConfigError(
            "invalid config:\n  - " + "\n  - ".join(problems)
        )


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def load_config(path: str | Path = "config.yaml") -> dict[str, Any]:
    """Read YAML, expand ${ENV} references, validate. Raises ConfigError."""
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p.resolve()}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p}: YAML did not parse: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{p}: top level must be a mapping")

    config = _expand_tree(raw)
    validate_config(config)
    return config


def build_provider(name: str, cfg: ProviderConfig) -> ModelProvider:
    """Construct ONE provider. Dispatches on cfg['type'] via the registry.

    This function is not edited when a vendor is added.
    """
    ptype = cfg.get("type")
    builder = _BUILDERS.get(ptype)
    if builder is None:
        raise ConfigError(
            f"provider {name!r}: unknown type {ptype!r}, "
            f"supported: {supported_types()}"
        )
    try:
        return builder(name, cfg)
    except KeyError as exc:
        raise ConfigError(f"provider {name!r}: missing required key {exc}") from exc
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"provider {name!r}: {exc}") from exc


def build_all(config: dict[str, Any]) -> dict[str, ModelProvider]:
    """Build the full {name: provider} registry the gateway holds."""
    validate_config(config)
    return {
        name: build_provider(name, cfg)
        for name, cfg in config["providers"].items()
    }


# --------------------------------------------------------------------------
# Classifier factory
#
# The SAME pattern as providers, applied to the compliance layer: a registry
# of builders, dispatch on a type string, never edited when one is added.
# Swapping regex for Presidio is a config edit, exactly like swapping a model.
# --------------------------------------------------------------------------

_CLASSIFIER_BUILDERS: dict[str, Callable[[dict[str, Any]], Any]] = {}


def _build_regex(cfg: ProviderConfig) -> Any:
    from policy import RegexClassifier

    return RegexClassifier()


def _build_presidio(cfg: ProviderConfig) -> Any:
    from classifiers.presidio_client import PresidioClassifier

    return PresidioClassifier(
        base_url=cfg.get("url", "http://127.0.0.1:8001"),
        timeout_s=float(cfg.get("timeout_s", 2.0)),
        threshold=cfg.get("threshold"),
    )


def _build_composite(cfg: ProviderConfig) -> Any:
    from classifiers.composite import CompositeClassifier

    names = cfg.get("members") or ["regex"]
    members = [build_classifier({**cfg, "type": n, "members": None}) for n in names]
    return CompositeClassifier(
        members, require_all_healthy=bool(cfg.get("require_all_healthy", True))
    )


_CLASSIFIER_BUILDERS.update(
    {"regex": _build_regex, "presidio": _build_presidio, "composite": _build_composite}
)


def supported_classifiers() -> list[str]:
    return sorted(_CLASSIFIER_BUILDERS)


def build_classifier(cfg: ProviderConfig | None = None) -> Any:
    """Construct the configured classifier. Defaults to regex."""
    cfg = dict(cfg or {})
    ctype = cfg.get("type", "regex")
    builder = _CLASSIFIER_BUILDERS.get(ctype)
    if builder is None:
        raise ConfigError(
            f"unknown classifier type {ctype!r}, "
            f"supported: {supported_classifiers()}"
        )
    return builder(cfg)


# --------------------------------------------------------------------------
# Guardrail factory
#
# Third use of the same registry pattern: providers, classifiers, guardrails.
# Adding a guard is an entry here plus a block in config.yaml.
# --------------------------------------------------------------------------

_GUARD_BUILDERS: dict[str, Callable[..., Any]] = {}


def _build_injection(cfg: ProviderConfig, providers: Any = None) -> Any:
    from guardrails import InjectionGuard

    return InjectionGuard()


def _build_safety_model(cfg: ProviderConfig, providers: Any = None) -> Any:
    from guardrails import SafetyModelGuard

    name = cfg.get("provider", "guard")
    if not providers or name not in providers:
        raise ConfigError(
            f"guardrail 'safety_model' needs provider {name!r}, "
            f"which is not in the provider registry"
        )
    adv = cfg.get("advisory_categories")
    return SafetyModelGuard(
        providers[name],
        max_tokens=int(cfg.get("max_tokens", 24)),
        # None means "use the guard's own default". An explicit empty list in
        # config means "block everything", which must stay expressible.
        advisory_categories=None if adv is None else tuple(adv),
    )


def _build_format(cfg: ProviderConfig, providers: Any = None) -> Any:
    from guardrails import FormatGuard

    return FormatGuard(max_sentences=cfg.get("max_sentences"))


def _build_noop(cfg: ProviderConfig, providers: Any = None) -> Any:
    from guardrails import IN, NoOpGuard

    return NoOpGuard(cfg.get("direction", IN))


_GUARD_BUILDERS.update({
    "injection": _build_injection,
    "safety_model": _build_safety_model,
    "format": _build_format,
    "noop": _build_noop,
})


def supported_guardrails() -> list[str]:
    return sorted(_GUARD_BUILDERS)


def build_guardrails(specs: Any, providers: Any = None) -> list[Any]:
    """Build one direction's guard chain from a list of config blocks.

    A block with `enabled: false` is skipped, so a guard can be switched off
    without deleting its configuration.
    """
    built: list[Any] = []
    for spec in specs or []:
        cfg = dict(spec)
        if cfg.get("enabled") is False:
            continue
        gtype = cfg.get("type")
        builder = _GUARD_BUILDERS.get(gtype)
        if builder is None:
            raise ConfigError(
                f"unknown guardrail type {gtype!r}, supported: {supported_guardrails()}"
            )
        built.append(builder(cfg, providers))
    return built
