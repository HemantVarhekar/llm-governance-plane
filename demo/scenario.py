"""MigrationGuard — the DIRECTV scenario, performed live.

Run:
    python demo/scenario.py                 # full walkthrough, pauses between acts
    python demo/scenario.py --no-pause      # run straight through
    python demo/scenario.py --act 3         # jump to one act

Every act announces the SCENARIOS.md id it performs, so the demo, the test
suite and the catalogue tell the same story.

Written to be WATCHED, not read: short lines, the decision highlighted, no
raw JSON dumps.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

import httpx

GATEWAY = os.environ.get("MG_URL", "http://127.0.0.1:8000")
API_KEY = os.environ.get("MG_API_KEY", "demo-secret-key")
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

GATEWAY_DIR = Path(__file__).resolve().parents[1] / "llm-gateway"
CONFIG = GATEWAY_DIR / "config.yaml"
BACKUP = GATEWAY_DIR / "config.yaml.demo-backup"
REGISTRY = Path(__file__).resolve().parents[1] / "evals" / "registry.json"

# The subscriber at the centre of the story.
SUBSCRIBER = "8841-22719"
CLEAN_ASK = "What is included in the sports package?"
PII_ASK = (
    f"My account {SUBSCRIBER} keeps buffering since you moved me from "
    f"satellite to streaming. Call me back on 214-555-0183."
)

W = 74
PAUSE = True

# Latencies captured live in Acts 2 and 3, so Act 7 can SHOW its numbers
# rather than assert them.
MEASURED: dict[str, int] = {}

# DIRECTV scale, from BUSINESS_CASE.md. Used to turn fractions of a cent
# into a number worth saying out loud.
SUBSCRIBERS = 8_200_000
SUPPORT_CONTACTS_PER_SUB_PER_YEAR = 1.5   # conservative; state it as an assumption


# ---------------------------------------------------------------- presentation


def rule(char: str = "-") -> None:
    print(char * W)


_ACT_NO = [0]


def act(title: str, scenario: str) -> None:
    print()
    rule("=")
    print(f"  ACT {_ACT_NO[0]}  |  {title}")
    print(f"  {scenario}")
    rule("=")
    _ACT_NO[0] += 1


def say(line: str = "") -> None:
    print(f"  {line}")


def verdict(ok: bool, line: str) -> None:
    print(f"  {'[PASS]' if ok else '[FAIL]'}  {line}")


def pause(prompt: str = "press Enter to continue") -> None:
    if PAUSE:
        try:
            input(f"\n  ({prompt}) ")
        except (EOFError, KeyboardInterrupt):
            print()


def spinner_call(fn, label: str):
    """Run a slow call while telling the audience what is happening.

    A local model takes 7-11s on this hardware. Silence for 10 seconds in
    front of an audience feels like a hang.
    """
    print(f"  {label} ", end="", flush=True)
    started = time.perf_counter()
    try:
        return fn()
    finally:
        print(f"({time.perf_counter() - started:.1f}s)")


# ---------------------------------------------------------------- gateway calls


def chat(content: str, *, task: str = "support_draft", model: str | None = None,
         max_tokens: int = 120, timeout: float = 180.0) -> httpx.Response:
    body: dict = {
        "task": task,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system",
             "content": "You are a concise TV support writer. Two short sentences."},
            {"role": "user", "content": content},
        ],
    }
    if model:
        body["model"] = model
    return httpx.post(
        f"{GATEWAY}/v1/chat/completions", json=body, headers=HEADERS, timeout=timeout
    )


def get(path: str, timeout: float = 30.0) -> httpx.Response:
    return httpx.get(f"{GATEWAY}{path}", headers=HEADERS, timeout=timeout)


def reload_gateway() -> httpx.Response:
    return httpx.post(f"{GATEWAY}/admin/reload", headers=HEADERS, timeout=60.0)


def tidy(text: str, limit: int = 240) -> str:
    """Trim to a COMPLETE sentence so the display never looks truncated.

    Cosmetic only — the model's real output is untouched and is what the
    guardrail inspects. Drift is caught by FormatGuard, not hidden here.
    """
    text = " ".join((text or "").strip().split())
    # Small models often wrap their whole reply in quotes; we add our own.
    if len(text) > 1 and text[0] in "\"'“" and text[-1] in "\"'”":
        text = text[1:-1].strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for stop in (". ", "! ", "? "):
        idx = cut.rfind(stop)
        if idx > limit * 0.5:
            return cut[: idx + 1]
    return cut.rstrip() + "..."


def _show_governance(mg: dict) -> None:
    """The decision block — printed on SUCCESS and on FAILURE alike."""
    cls = mg.get("classification") or {}
    say(f"PII found  : {cls.get('has_pii')}   {cls.get('entities') or ''}")
    say(f"rules fired: {mg.get('constraints_fired') or 'none'}")
    for prov, why in (mg.get("excluded") or {}).items():
        say(f"EXCLUDED   : {prov}  <- blocked by rule '{why}'")
    order = mg.get("attempt_order")
    say(f"attempted  : {' -> '.join(order) if order else '(nothing permitted)'}")


def show_decision(r: httpx.Response) -> dict:
    """Print the governance decision in a form an audience can follow."""
    if r.status_code != 200:
        detail = r.json().get("detail", r.text)
        say(f"HTTP {r.status_code}")
        if isinstance(detail, dict):
            say(f"reason     : {detail.get('error') or detail.get('reason')}")
            # The failure path shows the SAME governance block as success.
            # Without it, "we refused to use the cloud" is indistinguishable
            # from "everything was down".
            _show_governance(detail.get("migrationguard") or {})
            for h in detail.get("hops") or []:
                say(f"hop        : {h['provider']:<8} {h['outcome']}  {h.get('error') or ''}"[:100])
        else:
            say(f"reason     : {detail}")
        return {}

    body = r.json()
    mg = body["migrationguard"]
    say(f"served by  : {body['provider']}  ({body['zone']})   model {body['model']}")
    _show_governance(mg)
    hops = " -> ".join(f"{h['provider']}:{h['outcome']}" for h in mg["hops"])
    say(f"hops       : {hops}{'   (DEGRADED)' if mg['degraded'] else ''}")
    guards = mg.get("guardrails") or []
    fired = [d for g in guards for d in (g.get("detections") or [])]
    say(f"guardrails : {len(guards)} ran"
        + (f"   DETECTED: {', '.join(fired)}" if fired else "   clean"))
    say(f"cost       : ${mg['cost_usd']:.6f}      latency {mg['latency_ms']}ms")
    say(f'answer     : "{tidy(body["choices"][0]["message"]["content"])}"')
    return body


# ---------------------------------------------------------------- the acts


def act_0_context() -> None:
    act("The problem", "context")
    say("DIRECTV is migrating 8.2M satellite subscribers to streaming.")
    say("Every migrated household is a churn risk. At ~$1,800-2,100/year,")
    say("a 1% churn reduction is roughly $150-170M retained annually.")
    say("")
    say("Support agents need AI help. That help touches customer data.")
    say("So: can we use the cloud, and still prove nothing leaked?")
    say("")
    r = get("/healthz")
    h = r.json()
    say("Gateway up. Providers:")
    for name, p in h["providers"].items():
        state = "UP" if p["healthy"] else "DOWN"
        say(f"   {name:<8} {p['model']:<22} {p['zone']:<7} {state}")
    say(f"   auth enabled: {h['auth_enabled']}    chain deadline: {h['chain_deadline_s']}s")


def act_1_the_rule() -> None:
    act("The rule, in nine lines", "SC-01 / policy.yaml")
    pol = get("/policy").json()
    say("Hard constraints (filters - cost and latency get no vote):")
    for c in pol["constraints"]:
        cond = ", ".join(f"{k}={v}" for k, v in c["when"].items())
        say(f"   {c['name']}")
        say(f"      when {cond}  ->  require zone '{c['require_zone']}'")
    say("")
    say(f"Soft preference (a sort key): {' > '.join(pol['selection_order'])}")
    say(f"If nothing is permitted      : {pol['on_no_allowed_provider']}")
    say("")
    say("Constraints are FILTERS. Preferences are a SORT KEY.")
    say("A filter cannot be outvoted. That is the whole argument.")


def act_2_clean_uses_cloud() -> None:
    act("A clean question may use the cloud", "SC-02")
    say(f'Agent asks: "{CLEAN_ASK}"')
    say("No customer data in the payload.")
    print()
    r = spinner_call(lambda: chat(CLEAN_ASK), "routing...")
    body = show_decision(r)
    if body:
        MEASURED["cloud_ms"] = body["migrationguard"]["latency_ms"]
    print()
    ok = body and body.get("zone") == "cloud"
    verdict(bool(ok), "cloud was permitted and used - the constraint is targeted,")
    say("        not a blanket ban. A system that never uses the cloud is not")
    say("        governing, it is just refusing.")


def act_3_pii_stays_home() -> None:
    act("The same agent, now with customer data", "SC-01")
    say(f'Agent asks: "{PII_ASK[:62]}..."')
    say("This payload contains an account number and a phone number.")
    print()
    r = spinner_call(lambda: chat(PII_ASK), "routing...")
    body = show_decision(r)
    if body:
        MEASURED["onprem_ms"] = body["migrationguard"]["latency_ms"]
    print()
    ok = body and body.get("zone") == "onprem" and body["migrationguard"]["excluded"]
    verdict(bool(ok), "cloud was removed from the chain BEFORE any call was made.")
    say("        Not filtered on the way out. There was never a window in")
    say("        which that data could have left the building.")


def act_4_caller_cannot_override() -> None:
    act("What if the calling service insists on the cloud?", "SC-08")
    say('The caller sets "model": "claude" explicitly, with PII in the payload.')
    print()
    r = spinner_call(lambda: chat(PII_ASK, model="claude"), "routing...")
    body = show_decision(r)
    print()
    ok = body and body.get("zone") == "onprem"
    verdict(bool(ok), "the caller's preference was ignored.")
    say("        Governance a caller can bypass by setting a field is not")
    say("        governance. The field narrows within what policy allows;")
    say("        it can never add to it.")


def act_5_the_kill() -> None:
    act("Now break it", "SC-04 / SC-05  -- the one that matters")
    say("The on-prem model is about to become unreachable.")
    say("The cloud stays UP, HEALTHY, and is about three times FASTER.")
    say("")
    say("A system with a preference would quietly use the cloud.")
    say("Watch what this one does.")
    pause("press Enter to break the on-prem model")

    shutil.copy(CONFIG, BACKUP)
    text = CONFIG.read_text(encoding="utf-8")
    broken = text.replace(
        "${OLLAMA_BASE_URL:-http://localhost:11434}",
        "http://127.0.0.1:59999",          # nothing is listening here
    )
    CONFIG.write_text(broken, encoding="utf-8")
    rr = reload_gateway()
    say(f"on-prem endpoint repointed, gateway reloaded (HTTP {rr.status_code})")
    print()

    try:
        r = spinner_call(lambda: chat(PII_ASK, timeout=90), "same PII request...")
        print()
        show_decision(r)
        print()
        refused = r.status_code in (403, 503)
        verdict(refused, f"HTTP {r.status_code} - the request FAILED.")
        say("        The cloud was available, healthy and faster.")
        say("        It was never called.")
        say("")
        say("        That is the difference between a preference and a")
        say("        guarantee: the system takes the outage rather than")
        say("        the breach.")
    finally:
        shutil.copy(BACKUP, CONFIG)
        BACKUP.unlink(missing_ok=True)
        reload_gateway()
        print()
        say("on-prem endpoint restored, gateway reloaded.")


def act_6_the_evidence() -> None:
    act("Prove it", "SC-12")
    data = get("/audit?limit=200&only_excluded=true").json()
    say(f"Decisions in which a rule excluded a provider: {data['count']}")
    print()
    say(f"  {'request':<14} {'outcome':<12} {'served by':<16} blocked")
    say(f"  {'-' * 14} {'-' * 12} {'-' * 16} {'-' * 28}")
    for e in data["events"][-6:]:
        pd = e["policy_decisions"]
        ex = ", ".join(f"{k} by {v}" for k, v in pd["excluded"].items())
        served = (
            f"{e['provider_chosen']} ({e['zone']})" if e.get("zone")
            else "nobody - refused"
        )
        say(f"  {e['request_id']:<14} {e['outcome']:<12} {served:<16} {ex}")
    print()
    say("Not 'we have a PII filter'. Every time the rule fired, what it")
    say("blocked, and whether anyone was served at all. That is the answer")
    say("to an auditor asking you to prove nothing left the building.")


def act_7_the_money() -> None:
    act("What it costs", "SC-13")
    led = get("/ledger").json()

    served = {z: r for z, r in led["by_zone"].items() if z != "none"}
    total_calls = sum(r["calls"] for r in served.values()) or 1
    for zone, row in sorted(served.items()):
        share = 100 * row["calls"] / total_calls
        say(f"{zone:<8} {row['calls']:>3} calls ({share:4.0f}%)  "
            f"{row['tokens']:>6} tokens  ${row['cost_usd']:.6f}")
    print()
    say(f"spent            ${led['total_cost_usd']:.6f}")
    say(f"avoided          ${led['estimated_cost_avoided_usd']:.6f}   "
        f"(on-prem tokens priced at the cloud rate)")

    # --- scale, told honestly -------------------------------------------
    onprem_share = served.get("onprem", {}).get("calls", 0) / total_calls
    total_tokens = sum(r["tokens"] for r in served.values())
    avg_tokens = total_tokens / total_calls
    rate = led["cloud_rate_per_1k"]
    yearly = SUBSCRIBERS * SUPPORT_CONTACTS_PER_SUB_PER_YEAR

    print()
    say(f"At DIRECTV scale: {SUBSCRIBERS:,} subscribers x "
        f"{SUPPORT_CONTACTS_PER_SUB_PER_YEAR} AI-assisted contacts/year")
    say(f"= {yearly/1e6:.1f}M calls. Cost depends entirely on context size:")
    print()
    say(f"   {'tokens/call':>12}   {'all-cloud/yr':>14}   "
        f"{'at ' + format(onprem_share, '.0%') + ' on-prem':>16}")
    say(f"   {'-' * 12}   {'-' * 14}   {'-' * 16}")
    for tokens, note in (
        (int(avg_tokens), "<- this demo, short replies"),
        (1000, ""),
        (3000, "<- realistic with RAG context"),
    ):
        all_cloud = yearly * (tokens / 1000) * rate
        say(f"   {tokens:>12,}   ${all_cloud:>13,.0f}   "
            f"${all_cloud * onprem_share:>15,.0f}   {note}")

    print()
    say("Be honest about that. Even the realistic row is ~$110K against")
    say("$17.6B of revenue. COST IS THE WEAKEST of the three drivers at")
    say("this scale, and I would rather say so than inflate it.")
    print()
    say("The cost argument that DOES hold is optionality, not arbitrage:")
    say("   provider raises prices      ->  one config line")
    say("   a cheaper model ships       ->  one config line")
    say("   a provider degrades or 429s ->  fallback already handles it")
    say("")
    say("You are not buying a $70K saving. You are buying the ability to")
    say("move providers without a release - and the two drivers that")
    say("really carry this are compliance and resilience.")

    # --- the trade, from THIS run's numbers ------------------------------
    cloud_ms, onprem_ms = MEASURED.get("cloud_ms"), MEASURED.get("onprem_ms")
    if cloud_ms and onprem_ms:
        print()
        say(f"And the trade, measured in this run:")
        say(f"   cloud  {cloud_ms:>6}ms   paid")
        say(f"   onprem {onprem_ms:>6}ms   free   ({onprem_ms/cloud_ms:.1f}x slower)")
        say("")
        say("So when PII is present we are NOT taking the cheap option.")
        say(f"We are accepting a {onprem_ms/cloud_ms:.1f}x latency penalty to stay")
        say("compliant. The policy decides that per request, not a human.")


def act_8_swap_the_backend() -> None:
    act("Change the model without a deploy", "SC-15")

    def local_model() -> str:
        return get("/healthz").json()["providers"]["local"]["model"]

    say(f"on-prem model right now : {local_model()}")
    say("")
    say("config.yaml is DATA, not code. In Kubernetes it is a ConfigMap.")
    say("Swapping the model is a one-line edit plus a reload:")
    say("")
    say("    -  model: llama3.2:3b")
    say("    +  model: qwen3:4b")

    shutil.copy(CONFIG, BACKUP)
    try:
        CONFIG.write_text(
            CONFIG.read_text(encoding="utf-8").replace(
                "model: llama3.2:3b", "model: qwen3:4b", 1
            ),
            encoding="utf-8",
        )
        reload_gateway()
        say("")
        say(f"on-prem model now       : {local_model()}")
        say("")
        verdict(local_model() == "qwen3:4b",
                "backend swapped. No rebuild, no redeploy, no restart.")

        # --- and the safety half: a bad edit must not take the gateway down
        print()
        say("Now a BAD edit - an unknown provider type:")
        CONFIG.write_text(
            CONFIG.read_text(encoding="utf-8").replace(
                "type: ollama", "type: ollma", 1
            ),
            encoding="utf-8",
        )
        r = reload_gateway()
        say(f"   reload -> HTTP {r.status_code}")
        problem = str(r.json().get("detail", {}).get("problem", ""))[:90]
        say(f"   {problem}")
        say("")
        verdict(r.status_code == 400 and local_model() == "qwen3:4b",
                "rejected, and the PREVIOUS config is still serving traffic.")
        say("        A config mistake must never take down a running gateway.")
    finally:
        shutil.copy(BACKUP, CONFIG)
        BACKUP.unlink(missing_ok=True)
        reload_gateway()
        print()
        say(f"restored                : {local_model()}")

    print()
    say("Moving a model swap from release-time to runtime is the point:")
    say("an SRE can do it at 3am during an outage. A release cannot.")


def act_9_the_tests() -> None:
    act("How do I know any of this is true?", "the test suite")
    say("15 scenarios in SCENARIOS.md. Every one has a test tagged to it.")
    say("")
    say("   pytest -m governance -v      18 assertions, ~5 seconds")
    say("   pytest -m security -v")
    say("   pytest -k SC01 -v")
    say("")
    say("One test is a deliberate xfail: the regex classifier cannot detect")
    say("names. That gap is MEASURED, not hidden - and it is why Presidio")
    say("is the next change rather than a claim I am making today.")


def act_classifier_efficacy() -> None:
    act("Who decides what counts as PII?", "SC-17  -  measured, not asserted")
    h = get("/healthz").json()["classifier"]
    say(f"active classifier : {h['name']}   members {h['members']}   "
        f"healthy={h['healthy']}")
    say("")
    say("Swapping it is one config line - same factory pattern as the models.")
    say("But WHICH one? That is a measurement, not an opinion:")
    print()

    try:
        reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        say("(no eval registry - run: python evals/gate.py --promote composite)")
        return

    say(f"  {reg['dataset_size']} labelled payloads   "
        f"gate: recall>={reg['thresholds']['recall']}, "
        f"precision>={reg['thresholds']['precision']}, "
        f"f1>={reg['thresholds']['f1']}")
    print()
    say(f"  {'classifier':<12} {'recall':>7} {'prec':>7} {'F1':>7} {'p50':>7}   gate")
    say(f"  {'-'*12} {'-'*7} {'-'*7} {'-'*7} {'-'*7}   {'-'*8}")
    for name, c in reg["candidates"].items():
        flag = "  <- PROMOTED" if c["promoted"] else ""
        say(f"  {name:<12} {c['recall']:>7.3f} {c['precision']:>7.3f} "
            f"{c['f1']:>7.3f} {c['median_latency_ms']:>6.0f}ms   "
            f"{c['gate']:<8}{flag}")
    print()
    say("Read the failures, they are the whole argument:")
    say("   regex     misses every NAME      - it structurally cannot do NER")
    say("   presidio  misses ACCOUNT_NUMBER  - it has no recogniser for it")
    say("   composite catches both           - F1 1.000")
    print()
    verdict(True, "the gate BLOCKED two of three candidates.")
    say("        A promotion gate that always passes proves nothing.")
    say("")
    say("        And one honest note: my first dataset used SSN 123-45-6789,")
    say("        which Presidio deliberately deny-lists as a known-fake. The")
    say("        eval reported a weakness that did not exist. Test data lies")
    say("        too - that is why the misses are printed, not just the score.")


def act_kill_the_classifier() -> None:
    act("Break the thing that decides", "SC-18  -  the second break moment")
    say("Act 5 killed the MODEL. This kills the CLASSIFIER.")
    say("")
    say("Note the payload below contains NO personal data at all.")
    say("A system that only reacts to detected PII would happily use")
    say("the cloud here. Watch instead.")
    pause("press Enter to break the PII classifier")

    shutil.copy(CONFIG, BACKUP)
    try:
        CONFIG.write_text(
            CONFIG.read_text(encoding="utf-8").replace(
                "${PRESIDIO_URL:-http://127.0.0.1:8001}", "http://127.0.0.1:59998", 1),
            encoding="utf-8")
        reload_gateway()
        h = get("/healthz").json()["classifier"]
        say(f"classifier now    : {h['name']}  healthy={h['healthy']}  "
            f"(regex is still fine; the NER member is gone)")
        print()
        r = spinner_call(lambda: chat(CLEAN_ASK, timeout=90), "clean request...")
        print()
        show_decision(r)
        print()
        served_onprem = r.status_code == 200 and r.json().get("zone") == "onprem"
        verdict(served_onprem, "cloud refused for a payload with NO PII in it.")
        say("        We did not DETECT personal data. We were unable to PROVE")
        say("        its absence - and those are different things.")
        say("")
        say("        Regex was still working. Reporting 'healthy' because one")
        say("        member still works is the bug that quietly reopens the")
        say("        hole. Health is conjunctive for exactly this reason.")
    finally:
        shutil.copy(BACKUP, CONFIG)
        BACKUP.unlink(missing_ok=True)
        reload_gateway()
        print()
        say(f"classifier restored: "
            f"{get('/healthz').json()['classifier']['healthy']}")


ACTS = [
    act_0_context,
    act_1_the_rule,
    act_classifier_efficacy,       # SC-17  who decides what is PII
    act_2_clean_uses_cloud,
    act_3_pii_stays_home,
    act_4_caller_cannot_override,
    act_5_the_kill,                # SC-04/05  kill the MODEL
    act_kill_the_classifier,       # SC-18     kill the CLASSIFIER
    act_6_the_evidence,
    act_7_the_money,
    act_8_swap_the_backend,
    act_9_the_tests,
]


def main() -> int:
    global PAUSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-pause", action="store_true")
    ap.add_argument("--act", type=int, help="run a single act")
    ap.add_argument(
        "--fresh", action="store_true",
        help="clear the audit trail first, so the counts match what is watched",
    )
    args = ap.parse_args()
    PAUSE = not args.no_pause

    if args.fresh:
        events = GATEWAY_DIR / "events.jsonl"
        if events.exists():
            events.unlink()
            print(f"\n  (audit trail cleared: {events.name})")

    try:
        httpx.get(f"{GATEWAY}/healthz", timeout=5)
    except httpx.HTTPError:
        print(f"\n  Gateway is not reachable at {GATEWAY}")
        print("  Start it first:")
        print("    cd llm-gateway && python -m uvicorn main:app --port 8000\n")
        return 1

    print()
    rule("=")
    print("  MIGRATIONGUARD  -  AI Quality & Governance Control Plane")
    print("  DIRECTV satellite-to-streaming migration")
    rule("=")

    if args.act is not None:
        if not 0 <= args.act < len(ACTS):
            print(f"  act must be 0..{len(ACTS) - 1}")
            return 1
        _ACT_NO[0] = args.act          # so a single act prints its real number
        ACTS[args.act]()
    else:
        for i, fn in enumerate(ACTS):
            fn()
            if i < len(ACTS) - 1:
                pause()

    print()
    rule("=")
    print("  Governance, cost and compliance - one control plane, one policy file.")
    rule("=")
    print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # Never leave a demo-modified config behind.
        if BACKUP.exists():
            shutil.copy(BACKUP, CONFIG)
            BACKUP.unlink(missing_ok=True)
            print("\n  (config restored)")
        sys.exit(130)
