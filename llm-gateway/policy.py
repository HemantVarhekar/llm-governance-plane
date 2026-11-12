"""Policy evaluation — where policy.yaml stops being a document and becomes
enforcement.

Answers exactly one question: WHICH PROVIDERS ARE PERMITTED, AND WHY.

It does not call providers, does not execute the fallback chain (routing.py),
and does not inspect content for safety (guardrails, checkpoint 2).

The central rule, from ARCHITECTURE.md section 4:
    constraints are FILTERS   - they cannot be outvoted
    selection   is a SORT KEY - it only orders the survivors

Cost and latency are never consulted until after the filters have run, so a
cheap provider can never win its way past a compliance rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

import yaml

# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Classification:
    """What we know about a payload.

    `classifier_healthy` lives here deliberately: "we could not check" is
    itself something we know about the payload, and it is what makes the
    fail-closed rule evaluable from a single object.
    """

    has_pii: bool
    entities: tuple[str, ...] = ()
    classifier_healthy: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "has_pii": self.has_pii,
            "entities": list(self.entities),
            "classifier_healthy": self.classifier_healthy,
        }


@dataclass(frozen=True)
class PolicyDecision:
    """The audit record. More than an answer — the reasoning.

    `excluded` is the evidence: which provider was removed, by which rule.
    This is what answers "prove no PII reached the cloud".
    """

    classification: Classification
    constraints_fired: tuple[str, ...] = ()
    providers_before: tuple[str, ...] = ()
    providers_allowed: tuple[str, ...] = ()
    excluded: Mapping[str, str] = field(default_factory=dict)
    chain: tuple[str, ...] = ()
    task: str = "default"
    refused: bool = False
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "classification": self.classification.as_dict(),
            "constraints_fired": list(self.constraints_fired),
            "providers_before": list(self.providers_before),
            "providers_allowed": list(self.providers_allowed),
            "excluded": dict(self.excluded),
            "chain": list(self.chain),
            "refused": self.refused,
            "reason": self.reason,
        }


class PolicyError(Exception):
    """Raised at startup for a malformed policy file."""


# --------------------------------------------------------------------------
# The classifier contract
# --------------------------------------------------------------------------


@runtime_checkable
class Classifier(Protocol):
    """Structural, not inherited.

    Presidio arrives at checkpoint 2 as a thin HTTP client to a separate
    container. A Protocol means it does not have to inherit from anything of
    ours to qualify — it just needs these two methods. That is the swap point.
    """

    def classify(self, text: str) -> Classification: ...

    def health(self) -> bool: ...


class RegexClassifier:
    """STAND-IN until Presidio (checkpoint 2).

    Honest about its limits: it finds structured identifiers, and it CANNOT
    find names. PERSON is in policy.yaml's entity list because Presidio will
    detect it — regex cannot tell "Robert Chen" from any two capitalised
    words. Do not let a demo imply otherwise.
    """

    name = "regex"

    _PATTERNS: dict[str, re.Pattern[str]] = {
        "EMAIL_ADDRESS": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
        "US_SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        # A dot is as valid a separator as a space or a hyphen, and the red
        # team used exactly that: 4111.1111.1111.1111 walked through.
        "CREDIT_CARD": re.compile(r"\b(?:\d[ .-]?){13,19}\b"),
        "PHONE_NUMBER": re.compile(
            r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)"
        ),
        # Negative look-around so a reference code like REF-2026-00841 does
        # not match on its "2026-00841" tail. Found by the eval (edge-02),
        # not by inspection.
        # Negative look-around so a reference code like REF-2026-00841 does
        # not match on its "2026-00841" tail. Found by the eval (edge-02),
        # not by inspection.
        # The trailing guard must reject MORE DIGITS, not a full stop.
        # An earlier version used (?![\w.-]) and so refused to match any
        # account number that ended a sentence - it broke 19 cases to fix 3.
        # Caught by the red team re-run, which is the entire reason for
        # re-running it after every change.
        "ACCOUNT_NUMBER": re.compile(
            r"(?<![\w.-])\d{4}[.-]\d{5,}(?![\w-])(?!\.\d)"
        ),
        # Street addresses. Presidio recognises a city ("Dallas") reliably
        # but NOT a street address, which was the eval's only remaining gap
        # (loc-15/16/17). A house number, one to three capitalised words,
        # then a street-type word.
        #
        # Capitalisation is REQUIRED on purpose: it is what keeps "Press 3
        # for technical support" and "Error code 771" from matching. The
        # cost is that a lower-case address would be missed — an accepted
        # trade, because a false alarm here slows a customer down for
        # nothing while this miss is still caught by Presidio whenever a
        # city name appears alongside.
        "LOCATION": re.compile(
            r"\b\d{1,5}\s+(?:[A-Z][A-Za-z]{1,14}\s+){1,3}"
            r"(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|Close|Court|Ct"
            r"|Boulevard|Blvd|Way|Place|Pl|Terrace|Crescent|Circle|Cir"
            r"|Parkway|Pkwy|Trail|Trl|Square|Sq)\b"
        ),
    }

    def __init__(self, *, healthy: bool = True) -> None:
        # Settable so the demo can kill the classifier and show fail-closed.
        self._healthy = healthy

    def set_healthy(self, value: bool) -> None:
        self._healthy = value

    def health(self) -> bool:
        return self._healthy

    def classify(self, text: str) -> Classification:
        if not self._healthy:
            # Found nothing because we could not look. The fail-closed
            # constraint reads classifier_healthy, not has_pii.
            return Classification(
                has_pii=False, entities=(), classifier_healthy=False
            )
        # Same reasoning as the injection guard: fold away obfuscation
        # before matching, but only on the copy used for matching.
        from textnorm import for_identifiers

        folded = for_identifiers(text)
        found = tuple(sorted(
            name for name, pat in self._PATTERNS.items()
            if pat.search(folded) or pat.search(text)
        ))
        return Classification(
            has_pii=bool(found), entities=found, classifier_healthy=True
        )


# --------------------------------------------------------------------------
# The evaluator
# --------------------------------------------------------------------------

SUPPORTED_CONDITIONS = ("has_pii", "classifier_healthy")
VALID_ZONES = ("onprem", "cloud")


class Policy:
    """Loads policy.yaml and decides which providers are permitted."""

    def __init__(self, doc: dict[str, Any]) -> None:
        self._doc = doc
        self.constraints: list[dict[str, Any]] = doc.get("constraints") or []
        self.chains: dict[str, list[str]] = doc.get("chains") or {}
        self.selection_order: list[str] = (doc.get("selection") or {}).get(
            "order", ["health", "cost", "latency"]
        )
        self.pii_entities: list[str] = (doc.get("classification") or {}).get(
            "pii_entities", []
        )
        self.on_no_allowed_provider: str = doc.get(
            "on_no_allowed_provider", "refuse"
        )
        # Total budget for walking an ENTIRE chain, not per hop. Provisional
        # at 40s — to be replaced by a measured p99 after load testing.
        self.chain_deadline_s: float = float(
            (doc.get("routing") or {}).get("chain_deadline_s", 40)
        )
        self._validate()

    # -- loading -----------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path = "policy.yaml") -> "Policy":
        p = Path(path)
        if not p.is_file():
            raise PolicyError(f"policy file not found: {p.resolve()}")
        try:
            doc = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise PolicyError(f"{p}: YAML did not parse: {exc}") from exc
        if not isinstance(doc, dict):
            raise PolicyError(f"{p}: top level must be a mapping")
        return cls(doc)

    def _validate(self) -> None:
        problems: list[str] = []

        for rule in self.constraints:
            name = rule.get("name", "<unnamed>")
            if "name" not in rule:
                problems.append("a constraint is missing 'name'")
            when = rule.get("when") or {}
            if not when:
                problems.append(f"constraint {name}: missing 'when'")
            # The no-DSL decision, made structural: unknown condition keys are
            # rejected so nobody can quietly introduce an expression language.
            unknown = set(when) - set(SUPPORTED_CONDITIONS)
            if unknown:
                problems.append(
                    f"constraint {name}: unsupported condition(s) {sorted(unknown)}; "
                    f"supported: {list(SUPPORTED_CONDITIONS)}"
                )
            zone = rule.get("require_zone")
            if zone not in VALID_ZONES:
                problems.append(
                    f"constraint {name}: require_zone {zone!r} must be one of "
                    f"{list(VALID_ZONES)}"
                )

        if "default" not in self.chains:
            problems.append("chains must define a 'default'")

        if self.chain_deadline_s <= 0:
            problems.append(
                f"routing.chain_deadline_s must be positive, got "
                f"{self.chain_deadline_s}"
            )

        if self.on_no_allowed_provider != "refuse":
            problems.append(
                f"on_no_allowed_provider must be 'refuse', got "
                f"{self.on_no_allowed_provider!r} — degrading to an "
                f"unpermitted provider is never acceptable"
            )

        if problems:
            raise PolicyError("invalid policy:\n  - " + "\n  - ".join(problems))

    # -- evaluation --------------------------------------------------------

    @staticmethod
    def _matches(when: Mapping[str, Any], c: Classification) -> bool:
        """A rule fires when every condition it names holds."""
        for key, expected in when.items():
            actual = {
                "has_pii": c.has_pii,
                "classifier_healthy": c.classifier_healthy,
            }[key]
            if bool(actual) is not bool(expected):
                return False
        return True

    def decide(
        self,
        *,
        text: str,
        task: str,
        providers: Mapping[str, Any],
        classifier: Classifier,
    ) -> PolicyDecision:
        """Produce the allowlist, the chain, and the reasoning.

        Pure apart from the classifier call: no network, no provider calls, no
        writes. That is what makes the compliance assertions cheap to test.
        """
        before = tuple(providers)

        # 1. CLASSIFY — must happen before selection. You cannot route safely
        #    until you know the data's classification.
        classification = classifier.classify(text)

        # 2. FILTER — hard constraints. Cost and latency are not consulted.
        allowed = list(before)
        fired: list[str] = []
        excluded: dict[str, str] = {}

        for rule in self.constraints:
            if not self._matches(rule["when"], classification):
                continue
            fired.append(rule["name"])
            required = rule["require_zone"]
            for name in list(allowed):
                if getattr(providers[name], "zone", None) != required:
                    excluded[name] = rule["name"]
                    allowed.remove(name)

        # 3. REFUSE — never silently degrade to something unpermitted.
        #    Returned, NOT raised: a refusal is the most important event in
        #    the system, and an exception could escape before it is audited.
        if not allowed:
            return PolicyDecision(
                classification=classification,
                constraints_fired=tuple(fired),
                providers_before=before,
                providers_allowed=(),
                excluded=excluded,
                chain=(),
                task=task,
                refused=True,
                reason=(
                    "no provider satisfies the active constraints "
                    f"({', '.join(fired) or 'none'})"
                ),
            )

        # 4. CHAIN — explicit preference, intersected with the allowlist so
        #    forbidden hops are gone BEFORE the first call, not caught after.
        raw_chain = self.chains.get(task) or self.chains["default"]
        chain = tuple(h for h in raw_chain if h in allowed)

        if not chain:
            # The task's chain named only providers the constraints removed.
            # Fall back to the allowlist itself rather than refusing — the
            # request is permitted, it just has no preferred route left.
            chain = tuple(allowed)

        return PolicyDecision(
            classification=classification,
            constraints_fired=tuple(fired),
            providers_before=before,
            providers_allowed=tuple(allowed),
            excluded=excluded,
            chain=chain,
            task=task,
            refused=False,
        )

    # -- selection ---------------------------------------------------------

    def sort_key(self, provider: Any, *, healthy: bool = True) -> tuple:
        """SOFT preference, applied only to survivors of the filters.

        Health sorts first because a dead provider is never 'cheap'.
        """
        parts: list[Any] = []
        for criterion in self.selection_order:
            if criterion == "health":
                parts.append(0 if healthy else 1)
            elif criterion == "cost":
                parts.append(getattr(provider, "cost_per_1k_tokens", 0.0))
            elif criterion == "latency":
                parts.append(getattr(provider, "observed_p99_ms", 0))
        return tuple(parts)
