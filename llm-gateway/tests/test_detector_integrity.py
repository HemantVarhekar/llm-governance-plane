"""Two checks on the guard itself, rather than on what it catches.

SC-19 — the time it takes never reveals what it found.
SC-20 — a guard that is quietly WRONG is caught, not just one that is down.

See SCENARIOS.md.
"""

from __future__ import annotations

import json
import re
import statistics
import sys
import time
from pathlib import Path

import pytest

GATEWAY_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = GATEWAY_ROOT.parent
DATASET = REPO_ROOT / "evals" / "dataset.jsonl"

from factory import build_classifier  # noqa: E402
from policy import RegexClassifier  # noqa: E402


def _rows() -> list[dict]:
    return [json.loads(l) for l in DATASET.read_text(encoding="utf-8").splitlines() if l.strip()]


def _presidio_up() -> bool:
    try:
        import httpx

        r = httpx.get("http://127.0.0.1:8001/healthz", timeout=2.0)
        return r.status_code == 200 and bool(r.json().get("ready"))
    except Exception:
        return False


# ==========================================================================
# SC-19 — the stopwatch must not give anything away
# ==========================================================================


@pytest.mark.scenario("SC-19")
@pytest.mark.security
@pytest.mark.slow
@pytest.mark.skipif(not _presidio_up(), reason="needs the Presidio service running")
def test_time_taken_does_not_reveal_what_was_found():
    """
    GIVEN a mix of messages, some containing personal information and some not
    WHEN each is checked
    THEN the time taken is about the same either way

    Why this matters: if checking took noticeably longer whenever the guard
    found something, anyone could send messages, watch a stopwatch, and work
    out which ones the system treats as sensitive. That is a side channel.
    The guard reads the whole message regardless of what is in it, so the
    clock tells an observer nothing.

    The bar is deliberately loose (within 2x) because this runs on a shared,
    two-core laptop and a tight bound would be flaky rather than meaningful.
    """
    clf = build_classifier({"type": "composite", "members": ["regex", "presidio"]})
    rows = _rows()

    with_pii = [r for r in rows if r["has_pii"]][:15]
    without_pii = [r for r in rows if not r["has_pii"]][:15]
    assert with_pii and without_pii, "dataset must contain both kinds"

    def timings(sample: list[dict]) -> list[float]:
        out = []
        for r in sample:
            started = time.perf_counter()
            clf.classify(r["text"])
            out.append((time.perf_counter() - started) * 1000)
        return out

    found = statistics.median(timings(with_pii))
    nothing = statistics.median(timings(without_pii))
    ratio = max(found, nothing) / max(min(found, nothing), 0.001)

    assert ratio < 2.0, (
        f"timing leaks the verdict: {found:.0f}ms when something was found vs "
        f"{nothing:.0f}ms when nothing was ({ratio:.1f}x apart)"
    )


@pytest.mark.scenario("SC-19")
@pytest.mark.security
def test_the_message_itself_is_never_written_into_an_error():
    """
    GIVEN a classifier that cannot reach its service
    WHEN it fails
    THEN the failure carries no part of the message being checked

    A message that could not be checked is exactly the one most likely to
    contain something sensitive. Putting it in an error string would write
    it to a log that is probably less protected than the audit trail.
    """
    from classifiers.presidio_client import PresidioClassifier

    secret = "Robert Chen account 8841-22719 call 214-555-0183"
    dead = PresidioClassifier(base_url="http://127.0.0.1:59997", timeout_s=1.0)

    result = dead.classify(secret)

    assert result.classifier_healthy is False
    blob = repr(result)
    for leak in ("Robert", "8841", "555-0183"):
        assert leak not in blob, f"{leak!r} leaked into the classifier result"


# ==========================================================================
# SC-20 — a guard that is quietly wrong, not merely switched off
# ==========================================================================


@pytest.mark.scenario("SC-20")
@pytest.mark.governance
def test_health_checks_cannot_see_a_guard_that_is_merely_WRONG(monkeypatch):
    """
    GIVEN the account-number pattern is broken so it matches nothing
    AND   the guard still reports that it is working
    WHEN  a message containing an account number is checked
    THEN  the account number is NOT found
    AND   the health check still says everything is fine

    This is the hole, stated plainly: being switched OFF is easy to spot;
    being quietly WRONG is not. Health checks cannot see this, which is why
    the scoring harness below is the real defence.
    """
    broken = dict(RegexClassifier._PATTERNS)
    broken["ACCOUNT_NUMBER"] = re.compile(r"(?!x)x")  # matches nothing, ever
    monkeypatch.setattr(RegexClassifier, "_PATTERNS", broken)

    guard = RegexClassifier()

    assert guard.health() is True, "a broken guard still claims to be healthy"
    assert guard.classify("My account 8841-22719 is down").has_pii is False, (
        "sabotage did not take effect"
    )


@pytest.mark.scenario("SC-20")
@pytest.mark.governance
def test_the_scoring_harness_catches_a_quietly_broken_guard(monkeypatch):
    """
    GIVEN a working account-number pattern
    WHEN  it is broken so it matches nothing
    THEN  the measured recall drops
    AND   the drop is large enough to fall below the promotion bar

    The health check cannot see this. The scoring harness can, and that is
    what stops a silent regression from ever shipping.
    """
    rows = _rows()
    account_rows = [r for r in rows if "ACCOUNT_NUMBER" in r["entities"]]
    assert len(account_rows) >= 10, "need enough account-number cases to be meaningful"

    def recall_over(sample: list[dict]) -> float:
        guard = RegexClassifier()
        caught = sum(1 for r in sample if guard.classify(r["text"]).has_pii)
        return caught / len(sample)

    before = recall_over(account_rows)

    broken = dict(RegexClassifier._PATTERNS)
    broken["ACCOUNT_NUMBER"] = re.compile(r"(?!x)x")
    monkeypatch.setattr(RegexClassifier, "_PATTERNS", broken)
    after = recall_over(account_rows)

    assert before > 0.9, f"baseline recall should be high, got {before:.3f}"
    assert after < before, "the sabotage did not change the measured score"

    # Read the bar from the gate itself, so the test and the gate cannot
    # drift apart. A test that hardcodes 0.95 keeps passing after someone
    # quietly lowers the real bar to 0.6.
    if str(REPO_ROOT / "evals") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "evals"))
    from gate import THRESHOLDS

    bar = THRESHOLDS["recall"]
    assert after < bar, (
        f"a silently broken guard scored {after:.3f}, which would still have "
        f"passed the {bar} bar - the gate would not have caught it"
    )
