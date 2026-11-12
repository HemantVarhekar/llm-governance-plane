"""Output guardrails — drift detection.

SC-16. See SCENARIOS.md.
"""

from __future__ import annotations

import asyncio

import pytest

from guardrails import FAIL_CLOSED, FormatGuard, GuardChain, OUT, Verdict


def run(coro):
    return asyncio.run(coro)


GOOD = ("We're sorry about the buffering since your move to streaming. "
        "A technician will review your connection today.")

PLACEHOLDERS = [
    pytest.param("Hello, my name is [Name] and I'm calling about your account.",
                 id="square-bracket-name"),
    pytest.param("Your account {{account_id}} has been noted.", id="mustache"),
    pytest.param("Please call us on XXXXXXX to continue.", id="xxxx-redaction"),
    pytest.param("Dear <CUSTOMER NAME>, we apologise.", id="angle-caps"),
]

PREAMBLES = [
    pytest.param("Here's a script: We're sorry about the buffering.", id="heres-a-script"),
    pytest.param("Hmm, the user wants an apology. We're sorry.", id="hmm"),
    pytest.param("We are writing a concise apology for the subscriber.", id="we-are"),
    pytest.param("Okay, I need to apologise and offer a technician.", id="okay"),
]


@pytest.mark.scenario("SC-16")
@pytest.mark.quality
def test_a_clean_reply_passes_with_no_detections():
    """
    GIVEN a reply that follows the brief
    WHEN  the output guardrail inspects it
    THEN  it is allowed
    AND   no detections are recorded
    """
    v = run(FormatGuard().check(GOOD))
    assert v.allowed is True
    assert v.detections == ()
    assert v.direction == OUT


@pytest.mark.scenario("SC-16")
@pytest.mark.quality
@pytest.mark.parametrize("text", PLACEHOLDERS)
def test_unfilled_placeholder_blocks_the_reply(text):
    """
    GIVEN a reply containing an unfilled template placeholder
    WHEN  the output guardrail inspects it
    THEN  the reply is BLOCKED

    Sending "[Name]" to a subscriber is a defect, not a style preference.
    """
    v = run(FormatGuard().check(text))
    assert v.allowed is False
    assert any(d.startswith("placeholder") for d in v.detections)
    assert "placeholder" in (v.reason or "")


@pytest.mark.scenario("SC-16")
@pytest.mark.quality
@pytest.mark.parametrize("text", PREAMBLES)
def test_reasoning_preamble_is_recorded_but_allowed(text):
    """
    GIVEN a reply in which the model narrates its own reasoning
    WHEN  the output guardrail inspects it
    THEN  the reply is ALLOWED
    AND   the drift is still recorded

    Ugly is not the same as unsafe. A guardrail that blocks everything it
    dislikes is a guardrail people switch off.
    """
    v = run(FormatGuard().check(text))
    assert v.allowed is True
    assert "reasoning-preamble" in v.detections
    assert v.reason and "drift" in v.reason


@pytest.mark.scenario("SC-16")
@pytest.mark.quality
def test_overlong_reply_is_recorded_but_allowed():
    """
    GIVEN a reply longer than the configured sentence budget
    WHEN  the output guardrail inspects it
    THEN  it is allowed, with the length recorded
    """
    long = " ".join(f"Sentence number {i}." for i in range(8))
    v = run(FormatGuard(max_sentences=4).check(long))
    assert v.allowed is True
    assert any(d.startswith("too-long") for d in v.detections)


@pytest.mark.scenario("SC-16")
@pytest.mark.quality
def test_a_guard_that_itself_errors_fails_closed():
    """
    GIVEN a guardrail whose own check raises
    WHEN  the chain runs it
    THEN  its declared fail_mode decides the outcome

    A guard that ERRORS is not the same as a guard that DENIES. The PII gate
    fails closed for exactly this reason.
    """

    class Exploding:
        name = "exploding"
        direction = OUT
        fail_mode = FAIL_CLOSED

        async def check(self, payload: str) -> Verdict:
            raise RuntimeError("guard crashed")

    allowed, verdicts = run(GuardChain([Exploding()]).run("anything", OUT))
    assert allowed is False
    assert "fail-closed" in (verdicts[0].reason or "")


@pytest.mark.scenario("SC-16")
@pytest.mark.quality
def test_detections_are_serialisable_for_the_audit_log():
    """
    GIVEN a verdict with detections
    WHEN  it is serialised
    THEN  the detections survive into the record
    """
    v = run(FormatGuard().check("Here's a script: hello [Name]."))
    rec = v.as_dict()
    assert rec["guard"] == "format"
    assert rec["allowed"] is False
    assert len(rec["detections"]) >= 1
