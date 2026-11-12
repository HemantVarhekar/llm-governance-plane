"""Composite classifier — run several, union the findings.

WHY THIS EXISTS, concretely rather than architecturally:

    regex     catches ACCOUNT_NUMBER (8841-22719). Presidio has NO built-in
              recogniser for it.
    presidio  catches PERSON ("Robert Chen"). Regex structurally cannot —
              two capitalised words are indistinguishable from a name.

NEITHER IS SUFFICIENT ALONE. A payload containing both an account number and
a customer name is only fully covered by both running. That is not a
contrived example; it is the exact shape of a DIRECTV support message.

HEALTH IS CONJUNCTIVE, and this is the part that matters:

    healthy = ALL members healthy

If Presidio dies, the composite reports UNHEALTHY even though regex is still
working and still finding account numbers. We can no longer prove the
absence of names, so the fail-closed rule must fire. Reporting healthy
because "one of them still works" would quietly reopen the hole.
"""

from __future__ import annotations

from typing import Sequence

from policy import Classification


class CompositeClassifier:
    """Union of several classifiers' findings; conjunction of their health."""

    name = "composite"

    def __init__(self, members: Sequence[object], *, require_all_healthy: bool = True):
        if not members:
            raise ValueError("composite classifier needs at least one member")
        self.members = list(members)
        self.require_all_healthy = require_all_healthy

    @property
    def member_names(self) -> list[str]:
        return [getattr(m, "name", type(m).__name__) for m in self.members]

    def health(self) -> bool:
        states = [bool(m.health()) for m in self.members]
        return all(states) if self.require_all_healthy else any(states)

    def classify(self, text: str) -> Classification:
        entities: set[str] = set()
        all_healthy = True

        for member in self.members:
            try:
                result = member.classify(text)
            except Exception:
                # A member that throws is a member that did not look.
                all_healthy = False
                continue
            entities.update(result.entities)
            if not result.classifier_healthy:
                all_healthy = False

        healthy = all_healthy if self.require_all_healthy else True

        # has_pii reflects what was actually FOUND, by whichever member.
        # healthy reflects whether we could look everywhere we meant to.
        # Keeping them independent is what lets policy.yaml distinguish
        # "no PII" from "could not check".
        return Classification(
            has_pii=bool(entities),
            entities=tuple(sorted(entities)),
            classifier_healthy=healthy,
        )
