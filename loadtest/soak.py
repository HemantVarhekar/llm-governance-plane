"""Soak test - the one thing a five-minute run cannot tell you.

A short load test answers "how fast" and "does it break". It cannot
answer "does it still work in an hour", and that is a different question
with different failure modes: memory that grows and never comes back,
file handles that are opened and not closed, latency that drifts upward,
a cache that fills, a connection pool that slowly poisons itself.

Those failures are invisible at five minutes BY CONSTRUCTION. This run
holds a deliberately modest, constant load for a long time and watches
the server's own numbers rather than the client's.

    python loadtest/soak.py --minutes 60
    python loadtest/soak.py --minutes 20 --concurrency 2   # a shakedown

WHY THE LOAD IS LOW. The point is duration, not pressure. Saturating the
machine would make every sample noisy and tell us only what the load test
already told us. Two concurrent requests leaves headroom, so a trend in
memory is a trend in memory rather than a trend in queueing.

Writes loadtest/soak-results.json, and a verdict per metric:

    PASS      no significant trend
    SUSPECT   a trend that needs a longer run to call
    FAIL      a trend large enough to matter in production
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "loadtest" / "soak-results.json"

URL = os.environ.get("MG_URL", "http://127.0.0.1:8000")
KEY = os.environ.get("MG_API_KEY", "demo-secret-key")

# The corpus lives in one file that this and the k6 load test both read,
# so a figure from one run can be compared with a figure from the other.
#
# RUN 1 USED SIX STRINGS, ALL UNDER SIXTY-FIVE CHARACTERS. That is the
# single biggest weakness in the run 1 numbers. Real support messages are
# paragraphs, and almost everything a soak measures scales with body
# length: the classifier reads every character, the model's prompt grows,
# and the chain deadline that shed 30.1% of the personal-data path is a
# fixed wall that a longer body hits sooner. Measuring a forty-second
# deadline with fifty-character inputs measures the best case and calls
# it the case.
#
# The corpus also carries two kinds run 1 had no concept of:
#   borderline  text that LOOKS like personal data and is not. A detection
#               here is a false positive, which routes a safe request to
#               the expensive path and quietly deletes the cloud saving
#               the business case is built on. Run 1 could not see these.
#   oversize    past the gateway's payload limit. The size guard runs
#               before classification precisely so an attacker cannot make
#               the system pay for the payload; nothing had ever fired it
#               under load.
CORPUS_PATH = ROOT / "loadtest" / "corpus.json"


def load_corpus() -> list[dict]:
    """Read the corpus and INTERLEAVE it by kind.

    FOUND BY A FOUR-MINUTE SMOKE RUN, and it would have quietly ruined a
    sixty-minute one. The file is grouped by kind because that is how a
    human reads and reviews it. The workers walk it in order. So a short
    run sent thirty-four requests and every single one was ordinary - no
    personal data, no attacks, no refusals - and the result looked
    perfectly healthy because nothing that could fail was ever sent.

    A long run wraps often enough to cover everything, so the totals come
    out right, which is worse: the mix arrives in CLUMPS. Eighteen
    ordinary, then twenty-two carrying personal data, then thirteen
    attacks. Every figure sliced by time then compares one kind against
    another. The soak's own latency-drift check does exactly that - first
    tenth against last tenth - so it would have reported a drift that was
    really just a change of subject.

    Weaving the kinds together means any window of the run contains the
    whole mix. Deterministic, so two runs send the same sequence.

    WEAVING ON KIND ALONE IS NOT ENOUGH, which the second smoke run then
    showed: within a kind the file is ordered short, then medium, then
    long, so seventy-three requests reached 53 short bodies, 16 medium,
    and NOT ONE long one. 31 of the 69 bodies were never sent at all. The
    length slice - the whole reason this corpus exists - would have been
    empty exactly where it matters. So the bucket key is the PAIR, and
    any window contains every kind at every length.
    """
    doc = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    entries = doc["entries"]
    if not entries:
        raise SystemExit(f"{CORPUS_PATH} contains no entries")

    buckets: dict[tuple, list] = {}
    for e in entries:
        buckets.setdefault((e["kind"], e["band"]), []).append(e)
    order = sorted(buckets)
    woven: list[dict] = []
    while any(buckets[k] for k in order):
        for k in order:
            if buckets[k]:
                woven.append(buckets[k].pop(0))
    return woven


TRAFFIC = load_corpus()

# What each kind is allowed to come back as. Declared here, before the
# run, so that a refusal can be a PASS and a shed can be told apart from
# a wrong answer. Without this an aggregate error rate counts the HTTP 400
# on the attack path - which is the system working - as a failure, and
# then somebody "fixes" it.
EXPECTED_STATUS = {
    "ordinary":   {200, 429, 503, 504},
    "pii":        {200, 429, 503, 504},
    "borderline": {200, 429, 503, 504},
    "attack":     {400},
    "oversize":   {413},
}

# Three outcomes, kept apart on purpose.
#
# SERVED   the system did its job. A refusal is in here: HTTP 400 on the
#          attack path and 413 on an oversize body are the controls
#          working, and counting them as errors is how somebody ends up
#          "fixing" the guard.
# SHED     the system declined under pressure. A capacity result. It is
#          not a defect at the request level - load shedding is the
#          designed behaviour - it is a sizing input.
# ERROR    everything else. The system neither answered nor declined
#          cleanly. This is the only bucket that is unambiguously bad,
#          and collapsing it into "shed" hides it, because a shed rate
#          reads as a capacity story that more hardware solves.
SERVED_STATUS = {200, 400, 413}
SHED_STATUS = {429, 503, 504}

# A linear trend smaller than this is noise on a laptop that is also
# running a desktop. Chosen BEFORE the run, so the threshold cannot be
# fitted to the result afterwards.
RSS_SUSPECT_MB_PER_HOUR = 50.0
RSS_FAIL_MB_PER_HOUR = 250.0
LATENCY_SUSPECT_DRIFT_PCT = 20.0
LATENCY_FAIL_DRIFT_PCT = 50.0

# Below this, a per-hour slope is not reported at all.
#
# FOUND BY RUNNING IT. A two-minute shakedown reported Ollama leaking
# 168,032 MB an hour and called it a FAIL. What actually happened was the
# model loading: 76MB to 4,306MB in the first ninety seconds, extrapolated
# thirty-fold. The arithmetic was right and the conclusion was garbage.
#
# A short window cannot distinguish a trend from a transient, so the
# honest output is TOO SHORT rather than a confident number. A harness
# that reports a plausible-looking figure from inadequate data is how
# measurement theatre starts.
MIN_MINUTES_FOR_TREND = 20.0

# Samples inside this window are excluded from every trend. Starting up,
# loading a model and filling a cache are real costs, but they are not
# the steady state a soak is asking about - the same reason the event-loop
# probe keeps startup out of its histogram.
WARMUP_EXCLUDE_MINUTES = 3.0


def get(path: str, timeout: float = 10.0):
    req = urllib.request.Request(
        URL + path, headers={"Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=timeout) as fh:
        return json.load(fh)


def slope_per_hour(times: list[float], values: list[float]) -> float | None:
    """Least-squares slope, in units per hour.

    Ordinary linear regression rather than first-versus-last: a single
    noisy endpoint should not decide whether something is called a leak.
    """
    n = len(times)
    if n < 6:
        return None
    t0 = times[0]
    xs = [(t - t0) / 3600.0 for t in times]
    mx = statistics.fmean(xs)
    my = statistics.fmean(values)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, values)) / denom


def verdict(value: float | None, suspect: float, fail: float,
            minutes: float = 1e9) -> str:
    if minutes < MIN_MINUTES_FOR_TREND:
        return "TOO SHORT"
    if value is None:
        return "NO DATA"
    if abs(value) >= fail:
        return "FAIL"
    if abs(value) >= suspect:
        return "SUSPECT"
    return "PASS"


async def worker(session_id: int, stop: asyncio.Event, record: list) -> None:
    import httpx
    async with httpx.AsyncClient(timeout=120) as client:
        i = 0
        while not stop.is_set():
            entry = TRAFFIC[(i + session_id) % len(TRAFFIC)]
            text = entry["text"]
            i += 1
            started = time.perf_counter()
            zone = has_pii = None
            try:
                res = await client.post(
                    f"{URL}/v1/chat/completions",
                    json={"task": "support_draft",
                          "messages": [{"role": "user", "content": text}]},
                    headers={"Authorization": f"Bearer {KEY}",
                             "Content-Type": "application/json"})
                status = res.status_code
                if status == 200:
                    try:
                        body = res.json()
                        zone = body.get("zone")
                        # Captured so that detector accuracy becomes a
                        # LOAD figure and not only an offline one. A
                        # detector that is right on a quiet machine and
                        # wrong under pressure is the failure nobody
                        # tests for.
                        cls = (body.get("migrationguard") or {}) \
                            .get("classification") or {}
                        if "has_pii" in cls:
                            has_pii = bool(cls["has_pii"])
                    except Exception:
                        pass
            except Exception as exc:                        # noqa: BLE001
                status = f"err:{type(exc).__name__}"
            record.append({
                "t": time.time(),
                "id": entry["id"],
                "kind": entry["kind"],
                "band": entry["band"],
                "chars": len(text),
                "status": status,
                "zone": zone,
                "has_pii": has_pii,
                "expect_pii": entry["expect_pii"],
                "ms": round((time.perf_counter() - started) * 1000, 1),
            })


async def sampler(stop: asyncio.Event, samples: list, interval: float) -> None:
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        try:
            res = await loop.run_in_executor(None, get, "/resources")
            res["t"] = time.time()
            samples.append(res)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def run_soak(minutes: float, concurrency: int, interval: float):
    stop = asyncio.Event()
    requests: list = []
    samples: list = []
    tasks = [asyncio.create_task(worker(i, stop, requests))
             for i in range(concurrency)]
    tasks.append(asyncio.create_task(sampler(stop, samples, interval)))

    started = time.time()
    deadline = started + minutes * 60
    last_report = started
    while time.time() < deadline:
        await asyncio.sleep(5)
        now = time.time()
        if now - last_report >= 60:
            last_report = now
            done = len(requests)
            mins = (now - started) / 60
            rss = (samples[-1]["processes"]["gateway"]["rss_mb"]
                   if samples and "gateway" in samples[-1].get("processes", {})
                   else 0)
            recent = [r["ms"] for r in requests[-40:] if isinstance(r["status"], int)]
            med = statistics.median(recent) if recent else 0
            print(f"    {mins:5.1f} min   {done:5d} requests   "
                  f"median {med:7.0f} ms   gateway RSS {rss:6.1f} MB",
                  flush=True)

    stop.set()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    return requests, samples


def analyse(requests: list, samples: list, minutes: float) -> dict:
    out: dict = {"requests": len(requests), "samples": len(samples),
                 "planned_minutes": minutes}
    if not requests or not samples:
        out["verdict"] = "NO DATA"
        return out

    elapsed_h = (samples[-1]["t"] - samples[0]["t"]) / 3600.0
    out["actual_minutes"] = round(elapsed_h * 60, 1)

    # Drop the warm-up before measuring any trend. Model loading and cache
    # filling belong to starting up, not to running.
    cutoff = samples[0]["t"] + WARMUP_EXCLUDE_MINUTES * 60
    trend_samples = [s for s in samples if s["t"] >= cutoff] or samples
    trend_minutes = ((trend_samples[-1]["t"] - trend_samples[0]["t"]) / 60.0
                     if len(trend_samples) > 1 else 0.0)
    out["trend_window_minutes"] = round(trend_minutes, 1)
    out["warmup_excluded_minutes"] = WARMUP_EXCLUDE_MINUTES
    if trend_minutes < MIN_MINUTES_FOR_TREND:
        out["trend_note"] = (
            f"observation window {trend_minutes:.1f} min is under the "
            f"{MIN_MINUTES_FOR_TREND:g} min needed to call a trend; "
            f"slopes are reported as TOO SHORT rather than guessed at")

    # --- correctness over time, which must not degrade -------------------
    by_status: dict[str, int] = {}
    leaked = 0
    attacks_answered = 0
    oversize_accepted = 0
    unexpected: dict[str, int] = {}
    for r in requests:
        by_status[str(r["status"])] = by_status.get(str(r["status"]), 0) + 1
        kind = r["kind"]
        if kind == "pii" and r["zone"] == "cloud":
            leaked += 1
        if kind == "attack" and r["status"] == 200:
            attacks_answered += 1
        if kind == "oversize" and r["status"] == 200:
            oversize_accepted += 1
        allowed = EXPECTED_STATUS.get(kind)
        if allowed and r["status"] not in allowed:
            key = f"{kind}:{r['status']}"
            unexpected[key] = unexpected.get(key, 0) + 1
    out["status_mix"] = by_status
    out["pii_to_cloud"] = leaked
    out["attacks_answered"] = attacks_answered
    out["oversize_accepted"] = oversize_accepted
    out["unexpected_status"] = unexpected

    # --- what was actually sent ------------------------------------------
    # Printed with the result so that a number can never be read without
    # the corpus that produced it. Run 1's figures are not comparable with
    # these, and the record should make that obvious rather than leaving
    # it to a reader's memory.
    out["corpus"] = {
        "file": "loadtest/corpus.json",
        "entries": len(TRAFFIC),
        "kinds": {k: sum(1 for e in TRAFFIC if e["kind"] == k)
                  for k in sorted({e["kind"] for e in TRAFFIC})},
        "chars_min": min(len(e["text"]) for e in TRAFFIC),
        "chars_median": int(statistics.median(
            [len(e["text"]) for e in TRAFFIC])),
        "chars_max": max(len(e["text"]) for e in TRAFFIC),
    }

    # --- how the system behaved as the body got longer --------------------
    # The slice run 1 could not produce, because every body was the same
    # length. If shedding rises with length, the deadline is the binding
    # constraint and more hardware moves the cliff rather than removing
    # it. If it does not, the cost is elsewhere.
    def slice_by(key: str) -> dict:
        groups: dict[str, dict] = {}
        for r in requests:
            g = groups.setdefault(str(r[key]), {"sent": 0, "served": 0,
                                                "shed": 0, "error": 0,
                                                "ms": [], "chars": []})
            g["sent"] += 1
            g["chars"].append(r["chars"])
            if r["status"] in SERVED_STATUS:
                g["served"] += 1
                g["ms"].append(r["ms"])
            elif r["status"] in SHED_STATUS:
                g["shed"] += 1
            else:
                g["error"] += 1
        outg = {}
        for name, g in sorted(groups.items()):
            ms = sorted(g["ms"])
            n = g["sent"]
            outg[name] = {
                "sent": n,
                "served": g["served"],
                "shed": g["shed"],
                "error": g["error"],
                "served_pct": round(100.0 * g["served"] / n, 1) if n else None,
                "shed_pct": round(100.0 * g["shed"] / n, 1) if n else None,
                "error_pct": round(100.0 * g["error"] / n, 1) if n else None,
                "median_chars": int(statistics.median(g["chars"])),
                "median_ms": round(statistics.median(ms), 1) if ms else None,
                "p95_ms": (round(ms[min(len(ms) - 1, int(len(ms) * 0.95))], 1)
                           if ms else None),
            }
        return outg

    out["by_band"] = slice_by("band")
    out["by_kind"] = slice_by("kind")

    # --- detector accuracy UNDER LOAD -------------------------------------
    # Offline the detector scores F1 1.000 on 185 samples. That is measured
    # on a quiet machine with short inputs. This measures the same thing
    # while the system is busy and the bodies are paragraphs.
    #
    # The two errors are not symmetric and are not treated as such. A false
    # NEGATIVE means personal data was not recognised, so the hard routing
    # constraint never fires and nothing stands between that record and a
    # third-party provider: it joins pii_to_cloud and attacks_answered as a
    # condition that fails the run outright. A false POSITIVE is money, not
    # safety - a safe request pushed onto the expensive path - so it is
    # reported as a figure and left to the promotion gate, which is where
    # detector quality is actually gated. Folding a cost defect into a
    # safety verdict is how a verdict stops meaning anything.
    scored = [r for r in requests
              if r["has_pii"] is not None and r["kind"] in
              ("ordinary", "pii", "borderline")]
    if scored:
        tp = sum(1 for r in scored if r["expect_pii"] and r["has_pii"])
        fn = sum(1 for r in scored if r["expect_pii"] and not r["has_pii"])
        fp = sum(1 for r in scored if not r["expect_pii"] and r["has_pii"])
        tn = sum(1 for r in scored if not r["expect_pii"] and not r["has_pii"])
        neg = fp + tn
        fp_bor = sum(1 for r in scored
                     if r["kind"] == "borderline" and r["has_pii"])
        n_bor = sum(1 for r in scored if r["kind"] == "borderline")
        out["detector"] = {
            "scored": len(scored),
            "true_positive": tp, "false_negative": fn,
            "false_positive": fp, "true_negative": tn,
            "recall": round(tp / (tp + fn), 4) if (tp + fn) else None,
            "precision": round(tp / (tp + fp), 4) if (tp + fp) else None,
            "false_positive_rate": round(fp / neg, 4) if neg else None,
            "borderline_false_positive_pct": (
                round(100.0 * fp_bor / n_bor, 1) if n_bor else None),
        }
    else:
        out["detector"] = {"scored": 0,
                           "note": "no response carried a classification"}

    # --- memory trend per process ----------------------------------------
    times = [s["t"] for s in trend_samples]
    out["memory"] = {}
    for name in ("gateway", "presidio", "ollama"):
        try:
            values = [float(s["processes"][name]["rss_mb"])
                      for s in trend_samples]
            full = [float(s["processes"][name]["rss_mb"]) for s in samples]
        except Exception:
            continue
        slope = slope_per_hour(times, values)
        short = trend_minutes < MIN_MINUTES_FOR_TREND
        out["memory"][name] = {
            "start_mb": round(full[0], 1),
            "end_mb": round(full[-1], 1),
            "min_mb": round(min(full), 1),
            "max_mb": round(max(full), 1),
            # Reported as None rather than a number nobody should act on.
            "slope_mb_per_hour": (None if short or slope is None
                                  else round(slope, 1)),
            "verdict": verdict(slope, RSS_SUSPECT_MB_PER_HOUR,
                               RSS_FAIL_MB_PER_HOUR, trend_minutes),
        }

    # --- handle trend: the other classic leak ----------------------------
    out["handles"] = {}
    for name in ("gateway", "presidio"):
        try:
            values = [float(s["processes"][name]["handles"])
                      for s in trend_samples]
        except Exception:
            continue
        slope = slope_per_hour(times, values)
        out["handles"][name] = {
            "start": int(values[0]), "end": int(values[-1]),
            "slope_per_hour": (None if trend_minutes < MIN_MINUTES_FOR_TREND
                               or slope is None else round(slope, 1)),
            # A process that opens handles and never closes them hits the
            # operating-system limit and then fails in a way that looks
            # like a network problem.
            "verdict": verdict(slope, 50.0, 200.0, trend_minutes),
        }

    # --- latency drift: first tenth versus last tenth ---------------------
    ok = [r for r in requests if isinstance(r["status"], int)
          and r["status"] == 200]
    if len(ok) >= 20:
        tenth = max(5, len(ok) // 10)
        first = statistics.median([r["ms"] for r in ok[:tenth]])
        last = statistics.median([r["ms"] for r in ok[-tenth:]])
        drift = 100.0 * (last - first) / first if first else 0.0
        out["latency"] = {
            "first_decile_median_ms": round(first, 1),
            "last_decile_median_ms": round(last, 1),
            "drift_pct": round(drift, 1),
            "verdict": verdict(drift, LATENCY_SUSPECT_DRIFT_PCT,
                               LATENCY_FAIL_DRIFT_PCT, trend_minutes),
            "sample": len(ok),
        }

    # --- event loop over the whole run ------------------------------------
    try:
        lags = [float(s["event_loop"]["lag_last_ms"]) for s in samples]
        out["event_loop"] = {
            "floor_ms": samples[-1]["event_loop"].get("lag_floor_ms"),
            "median_ms": round(statistics.median(lags), 2),
            "p95_ms": round(sorted(lags)[int(len(lags) * 0.95)], 2),
            "max_ms": round(max(lags), 2),
        }
    except Exception:
        pass

    verdicts = [v["verdict"] for v in out.get("memory", {}).values()]
    verdicts += [v["verdict"] for v in out.get("handles", {}).values()]
    if "latency" in out:
        verdicts.append(out["latency"]["verdict"])
    # The compliance absolutes. Each one means something got past a control
    # that exists to stop it, and no amount of good trend data redeems that.
    if (leaked or attacks_answered or oversize_accepted
            or out["detector"].get("false_negative")):
        verdicts.append("FAIL")
    out["verdict"] = ("FAIL" if "FAIL" in verdicts else
                      "SUSPECT" if "SUSPECT" in verdicts else
                      "INCONCLUSIVE" if "TOO SHORT" in verdicts else
                      "PASS" if verdicts else "NO DATA")
    return out


def report(a: dict) -> None:
    print("\n" + "=" * 74)
    print(f"  SOAK RESULT: {a.get('verdict')}")
    print("=" * 74)
    print(f"  {a.get('requests')} requests over "
          f"{a.get('actual_minutes')} minutes, "
          f"{a.get('samples')} server samples")
    c = a.get("corpus") or {}
    if c:
        print(f"  corpus: {c.get('entries')} entries, "
              f"{c.get('chars_min')}-{c.get('chars_max')} chars "
              f"(median {c.get('chars_median')}), {c.get('kinds')}")
    print(f"  status mix: {a.get('status_mix')}")
    if a.get("unexpected_status"):
        print(f"  UNEXPECTED STATUS: {a['unexpected_status']}")
    if a.get("trend_note"):
        print(f"  NOTE: {a['trend_note']}")
    print(f"  personal data to cloud: {a.get('pii_to_cloud')}    "
          f"attacks answered: {a.get('attacks_answered')}    "
          f"oversize accepted: {a.get('oversize_accepted')}")

    d = a.get("detector") or {}
    if d.get("scored"):
        print("\n  DETECTOR, UNDER LOAD "
              f"({d['scored']} classified responses)")
        print(f"    true positive {d['true_positive']:<5} "
              f"false negative {d['false_negative']:<5} "
              f"<- a miss here is a compliance failure")
        print(f"    true negative {d['true_negative']:<5} "
              f"false positive {d['false_positive']:<5} "
              f"<- a hit here costs the cloud saving")
        if d.get("recall") is not None:
            print(f"    recall {d['recall']:.4f}   "
                  f"precision {d['precision'] if d['precision'] is not None else 'n/a'}"
                  f"   false positive rate "
                  f"{d['false_positive_rate'] if d['false_positive_rate'] is not None else 'n/a'}")
        if d.get("borderline_false_positive_pct") is not None:
            print(f"    borderline bodies misread as personal data: "
                  f"{d['borderline_false_positive_pct']}%")

    for label, key in (("BY LENGTH", "by_band"), ("BY KIND", "by_kind")):
        g = a.get(key) or {}
        if not g:
            continue
        print(f"\n  {label}")
        print(f"    {'':<12}{'sent':>6}{'served':>8}{'shed':>6}{'error':>7}"
              f"{'error %':>9}{'chars':>8}{'median ms':>12}{'p95 ms':>10}")
        for name, s in g.items():
            med = f"{s['median_ms']:.0f}" if s['median_ms'] is not None else "-"
            p95 = f"{s['p95_ms']:.0f}" if s['p95_ms'] is not None else "-"
            print(f"    {name:<12}{s['sent']:>6}{s['served']:>8}"
                  f"{s['shed']:>6}{s['error']:>7}{s['error_pct']:>8.1f}%"
                  f"{s['median_chars']:>8}{med:>12}{p95:>10}")

    if a.get("memory"):
        print("\n  MEMORY (the reason a soak exists)")
        print(f"    {'process':<10}{'start':>9}{'end':>9}{'peak':>9}"
              f"{'per hour':>11}   verdict")
        for name, m in a["memory"].items():
            slope = m["slope_mb_per_hour"]
            print(f"    {name:<10}{m['start_mb']:>8.0f}M{m['end_mb']:>8.0f}M"
                  f"{m['max_mb']:>8.0f}M"
                  f"{(f'{slope:+.1f}M' if slope is not None else 'n/a'):>11}"
                  f"   {m['verdict']}")

    if a.get("handles"):
        print("\n  HANDLES")
        for name, h in a["handles"].items():
            rate = (f"{h['slope_per_hour']:+.1f}/hour"
                    if h['slope_per_hour'] is not None else "n/a")
            print(f"    {name:<10} {h['start']:>6} -> {h['end']:<6} "
                  f"{rate:>12}   {h['verdict']}")

    if a.get("latency"):
        l = a["latency"]
        print("\n  LATENCY DRIFT (first tenth vs last tenth)")
        print(f"    {l['first_decile_median_ms']:.0f} ms -> "
              f"{l['last_decile_median_ms']:.0f} ms   "
              f"{l['drift_pct']:+.1f}%   {l['verdict']}")

    if a.get("event_loop"):
        e = a["event_loop"]
        print(f"\n  EVENT LOOP   median {e['median_ms']}ms   "
              f"p95 {e['p95_ms']}ms   max {e['max_ms']}ms   "
              f"(floor {e['floor_ms']}ms)")
    print("=" * 74 + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Long-running soak test.")
    ap.add_argument("--minutes", type=float, default=60.0)
    ap.add_argument("--concurrency", type=int, default=2,
                    help="kept low on purpose: this measures duration, "
                         "not pressure")
    ap.add_argument("--sample-interval", type=float, default=10.0)
    args = ap.parse_args()

    try:
        health = get("/healthz", timeout=5)
    except Exception as exc:                                # noqa: BLE001
        print(f"  gateway not answering on {URL}: {exc}")
        return 2
    guards = (health.get("guardrails") or {}).get("input") or []
    if not guards:
        print("  gateway has NO input guardrails loaded - refusing to soak "
              "a misconfigured deployment")
        return 2
    if not (health.get("sampler") or {}).get("available"):
        print("  server-side sampling unavailable - a soak without memory "
              "numbers is just a long load test")
        return 2

    print(f"\n  SOAK  {args.minutes:g} minutes at concurrency "
          f"{args.concurrency}, sampling every {args.sample_interval:g}s")
    print(f"  guards: {guards}")
    print(f"  Nothing else should run on this machine for the duration.\n")

    started = datetime.now(timezone.utc)
    requests, samples = asyncio.run(
        run_soak(args.minutes, args.concurrency, args.sample_interval))
    analysis = analyse(requests, samples, args.minutes)
    analysis["started_at"] = started.isoformat()
    analysis["concurrency"] = args.concurrency
    report(analysis)

    payload = dict(analysis)
    payload["raw_requests"] = requests
    payload["raw_samples"] = samples

    # A SHAKEDOWN MUST NOT OVERWRITE A PUBLISHED RESULT.
    #
    # Found the hard way: a four-minute smoke run replaced a published
    # sixty-minute result in loadtest/soak-results.json. The write-once
    # archive under loadtest/runs/ was untouched, which is exactly why
    # that design was worth the duplication - but the live file is what a
    # reader opens first, and for a few minutes it claimed the system had
    # been soaked for four minutes and come back INCONCLUSIVE.
    #
    # A run too short to produce a trend is a diagnostic, not a result,
    # so it is written somewhere that says so in its name.
    out = OUT
    if (analysis.get("trend_window_minutes") or 0) < MIN_MINUTES_FOR_TREND:
        out = OUT.with_name("soak-smoke.json")
        print(f"  this run is too short to be a result "
              f"({analysis.get('trend_window_minutes')} min of trend window, "
              f"{MIN_MINUTES_FOR_TREND:g} needed)")
        print(f"  NOT overwriting {OUT.name} - writing {out.name} instead")
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"  wrote {out.relative_to(ROOT)}\n")

    return 1 if analysis.get("verdict") == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
