"""Presidio classifier — the gateway's view of the analyzer service.

Satisfies the `Classifier` protocol in policy.py: two methods, no
inheritance. The gateway swaps regex for this by editing one config line.

THE CRITICAL BEHAVIOUR, and the easiest thing to get wrong:

    When the service is unreachable, this reports classifier_healthy=False.

It does NOT quietly return "no PII found". An empty result from a dead
classifier is indistinguishable from an empty result from a healthy one —
and the gateway would happily send the payload to the cloud. Reporting
unhealthy is what makes the fail-closed rule in policy.yaml fire.
"""

from __future__ import annotations

import os

import httpx

from policy import Classification

DEFAULT_URL = os.environ.get("PRESIDIO_URL", "http://127.0.0.1:8001")


class PresidioClassifier:
    """spaCy NER-backed PII detection over HTTP."""

    name = "presidio"

    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        *,
        timeout_s: float = 2.0,
        threshold: float | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # Deliberately tight. This call sits on the hot path; a hung
        # analyzer must degrade us to fail-closed quickly, not stall the
        # gateway behind it.
        self.timeout_s = timeout_s
        self.threshold = threshold

    # -- Classifier protocol ----------------------------------------------

    def health(self) -> bool:
        try:
            r = httpx.get(f"{self.base_url}/healthz", timeout=min(self.timeout_s, 2.0))
            return r.status_code == 200 and bool(r.json().get("ready"))
        except (httpx.HTTPError, ValueError):
            return False

    def classify(self, text: str) -> Classification:
        payload: dict = {"text": text or ""}
        if self.threshold is not None:
            payload["threshold"] = self.threshold
        try:
            r = httpx.post(
                f"{self.base_url}/analyze", json=payload, timeout=self.timeout_s
            )
        except httpx.HTTPError:
            return self._unavailable()

        if r.status_code != 200:
            return self._unavailable()

        try:
            entities = tuple(sorted(r.json().get("entities") or ()))
        except ValueError:
            return self._unavailable()

        return Classification(
            has_pii=bool(entities), entities=entities, classifier_healthy=True
        )

    @staticmethod
    def _unavailable() -> Classification:
        # has_pii=False because we found nothing — but healthy=False because
        # we could not LOOK. policy.yaml reads classifier_healthy, so this
        # forces on-prem rather than permitting the cloud.
        return Classification(has_pii=False, entities=(), classifier_healthy=False)
