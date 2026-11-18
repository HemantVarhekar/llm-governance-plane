"""Integrity of the shared load and soak corpus.

The corpus is a TEST ASSET, and test assets rot silently. A body edited
from four hundred characters down to forty still sits under a heading
that says "long", and every figure sliced by length quietly becomes a
lie. Nothing in the run would notice: the soak would report a clean
number for a band whose contents no longer match its name.

These tests are the thing that notices. They are hermetic - they read a
JSON file and count - so they run in CI on every push, next to the
suite, rather than only when somebody remembers to soak.

Scenario markers reference the two scenarios this corpus exists to
exercise under load:
  SC-07  chain deadline bounds total latency - the deadline is a fixed
         wall, so the corpus must contain bodies long enough to approach
         it. Run 1 did not, which is why its shed rate flattered the
         system.
  SC-23  oversized payloads rejected before any work - the guard was
         proven by a unit test and had never fired under load.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS = REPO_ROOT / "loadtest" / "corpus.json"

# Expected statuses the harnesses declare. Duplicated here deliberately:
# if soak.py and the corpus ever disagree about what a kind may return,
# one of the two is wrong and a test should say so rather than the
# disagreement being resolved silently at run time by whichever file was
# read last.
KINDS = {"ordinary", "pii", "borderline", "attack", "oversize"}

# The detector is only as good as the variety it has seen. Every type the
# offline dataset covers must also appear in the corpus that runs under
# load, or a type is gated and never pressure-tested.
REQUIRED_ENTITIES = {
    "ACCOUNT_NUMBER", "PHONE_NUMBER", "EMAIL_ADDRESS", "PERSON",
    "LOCATION", "CREDIT_CARD", "US_SSN", "DATE_TIME", "IP_ADDRESS",
}


@pytest.fixture(scope="module")
def doc() -> dict:
    assert CORPUS.is_file(), f"{CORPUS} is missing"
    return json.loads(CORPUS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def entries(doc) -> list[dict]:
    return doc["entries"]


@pytest.mark.performance
def test_every_entry_is_well_formed(entries):
    assert entries, "the corpus is empty"
    required = {"id", "kind", "band", "entities", "expect_pii", "text"}
    for e in entries:
        missing = required - set(e)
        assert not missing, f"{e.get('id', '?')} is missing {sorted(missing)}"
        assert e["kind"] in KINDS, f"{e['id']}: unknown kind {e['kind']}"
        assert e["text"].strip(), f"{e['id']}: empty body"


@pytest.mark.performance
def test_ids_are_unique(entries):
    dupes = [k for k, n in Counter(e["id"] for e in entries).items() if n > 1]
    assert not dupes, f"duplicate ids: {dupes}"


@pytest.mark.scenario("SC-07")
@pytest.mark.performance
def test_each_body_actually_falls_in_its_band(doc, entries):
    """The band label and the body must agree.

    This is the test that stops the length slice from going stale. A
    median latency reported for "long" means nothing unless the long
    bodies are still long.
    """
    bands = doc["bands"]
    wrong = []
    for e in entries:
        n = len(e["text"])
        lo = bands[e["band"]]["min_chars"]
        hi = bands[e["band"]]["max_chars"]
        if not lo <= n <= hi:
            wrong.append(f"{e['id']}: band {e['band']} wants {lo}-{hi}, "
                         f"body is {n}")
    assert not wrong, "bands no longer describe their contents:\n" + \
        "\n".join(wrong)


@pytest.mark.scenario("SC-07")
@pytest.mark.performance
def test_the_corpus_reaches_production_length(entries):
    """Run 1's whole corpus was shorter than this test's lower bound.

    Six bodies, 40 to 64 characters. Every latency and shed figure it
    produced was therefore a best case presented without that caveat.
    A corpus that cannot reach production length cannot test a deadline.
    """
    sizes = sorted(len(e["text"]) for e in entries
                   if e["band"] != "oversize")
    median = sizes[len(sizes) // 2]
    assert median >= 150, (
        f"median body is {median} chars; real support messages are "
        f"paragraphs, and a corpus of fragments measures the best case")
    assert max(sizes) >= 900, (
        f"longest body is {max(sizes)} chars; nothing here approaches "
        f"the load a real message puts on the classifier and the model")


@pytest.mark.performance
def test_every_personal_data_type_is_exercised_under_load(entries):
    seen = {x for e in entries for x in e["entities"]}
    missing = REQUIRED_ENTITIES - seen
    assert not missing, (
        f"no load-corpus body carries {sorted(missing)}; that type is "
        f"gated offline and never pressure-tested")


@pytest.mark.performance
def test_multi_entity_bodies_exist(entries):
    """One identifier per message is the easy case.

    Real messages carry a name and an address and a phone number in one
    paragraph, which is where a detector that scores well on fragments
    starts dropping entities.
    """
    multi = [e for e in entries if len(e["entities"]) >= 3]
    assert len(multi) >= 8, (
        f"only {len(multi)} bodies carry three or more identifiers")


@pytest.mark.performance
def test_borderline_bodies_exist_and_claim_no_personal_data(entries):
    """Without these, a false positive rate cannot be measured at all.

    A false positive routes a safe request to the expensive path. With
    no borderline traffic in the corpus, that cost is invisible and the
    saving in the business case is unfalsifiable.
    """
    bor = [e for e in entries if e["kind"] == "borderline"]
    assert len(bor) >= 10, f"only {len(bor)} borderline bodies"
    for e in bor:
        assert e["expect_pii"] is False, (
            f"{e['id']}: a borderline body must expect NO detection, "
            f"otherwise it is just another positive")
        assert not e["entities"], (
            f"{e['id']}: a borderline body must carry no entities")


@pytest.mark.performance
def test_personal_data_bodies_declare_what_they_carry(entries):
    for e in entries:
        if e["kind"] == "pii":
            assert e["expect_pii"] is True, f"{e['id']}: expect_pii is false"
            assert e["entities"], (
                f"{e['id']}: carries personal data but declares no types, "
                f"so a miss cannot be attributed")


@pytest.mark.scenario("SC-23")
@pytest.mark.performance
def test_oversize_bodies_exceed_the_gateway_limit(doc, entries):
    """The limit is 4000 characters and the guard runs before any work.

    One of these is an attack padded past the limit. The size guard must
    fire first - if classification ran before the size check, an attacker
    could make the system pay to read the payload it is about to reject,
    which is the cheapest denial of service there is.
    """
    import os
    limit = int(os.environ.get("MG_MAX_PAYLOAD_CHARS", "4000"))
    over = [e for e in entries if e["kind"] == "oversize"]
    assert len(over) >= 2, "no oversize bodies, so the 413 path is untested"
    for e in over:
        assert len(e["text"]) > limit, (
            f"{e['id']} is {len(e['text'])} chars, not over the "
            f"{limit} limit, so it will be accepted and prove nothing")


@pytest.mark.performance
def test_attacks_are_present_at_every_length(entries):
    """A short injection is the easy one to catch.

    The hard one is an injection at the end of nine hundred characters of
    entirely plausible support text, where a guard that only reads the
    opening sees nothing wrong.
    """
    bands = Counter(e["band"] for e in entries if e["kind"] == "attack")
    for band in ("short", "medium", "long"):
        assert bands.get(band, 0) >= 3, (
            f"only {bands.get(band, 0)} {band} attacks; an injection "
            f"buried in a long body is a different test from a bare one")


@pytest.mark.performance
def test_no_real_contact_details_leaked_into_the_corpus(entries):
    """This file is public.

    Every address must be example.com and every telephone number must be
    in the 555 range reserved for fiction. A corpus of personal-data
    examples is exactly the file where a real one gets pasted by
    accident.
    """
    import re
    bad = []
    for e in entries:
        for addr in re.findall(r"[\w.+-]+@[\w.-]+\.\w+", e["text"]):
            if not addr.lower().endswith("example.com"):
                bad.append(f"{e['id']}: email {addr}")
        for num in re.findall(r"\b\d{3}-\d{3}-\d{4}\b", e["text"]):
            if not num.startswith("555-"):
                bad.append(f"{e['id']}: telephone {num}")
    assert not bad, "real-looking contact details in a public file:\n" + \
        "\n".join(bad)


@pytest.mark.performance
def test_the_send_order_mixes_kinds_and_lengths_together():
    """Two smoke runs, two ordering bugs, both invisible in the totals.

    The file is grouped by kind, and within a kind by length, because
    that is how a person reads and reviews it. Sent in that order:

      - a four-minute run sent 34 requests and every one was ordinary.
        No personal data, no attacks, nothing that could fail. The result
        looked healthy because nothing risky was ever sent.
      - weaving by kind alone fixed that, and a six-minute run then sent
        53 short bodies, 16 medium and NOT ONE long one. 31 of 69 bodies
        were never sent. The length slice - the reason this corpus exists
        - was empty exactly where it mattered.

    Neither showed up as an error. Both would have produced a confident,
    wrong sixty-minute result. This test asserts the property that stops
    it: any short window of the send order contains every kind at every
    length.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_soak", REPO_ROOT / "loadtest" / "soak.py")
    soak = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(soak)

    order = soak.TRAFFIC
    assert len(order) == len(json.loads(
        CORPUS.read_text(encoding="utf-8"))["entries"]), \
        "the send order dropped or duplicated bodies"

    pairs = {(e["kind"], e["band"]) for e in order}
    window = order[:len(pairs)]
    assert {(e["kind"], e["band"]) for e in window} == pairs, (
        f"the first {len(pairs)} requests do not cover every "
        f"kind-and-length combination; the mix arrives in clumps and any "
        f"time-sliced figure compares one kind against another")

    # And it must be stable, or two runs are not comparable.
    again = soak.load_corpus()
    assert [e["id"] for e in again] == [e["id"] for e in order]


@pytest.mark.performance
def test_both_harnesses_read_this_file(entries):
    """The corpus is only shared if both harnesses actually read it.

    They drifted apart once already: the soak carried six strings and the
    k6 script carried fifteen different ones, so their latency figures
    described different traffic and nothing said so.
    """
    soak = (REPO_ROOT / "loadtest" / "soak.py").read_text(encoding="utf-8")
    k6 = (REPO_ROOT / "loadtest" / "gateway.k6.js").read_text(encoding="utf-8")
    assert "corpus.json" in soak, "soak.py no longer reads the corpus"
    assert "corpus.json" in k6, "gateway.k6.js no longer reads the corpus"
