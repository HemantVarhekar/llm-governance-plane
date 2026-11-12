"""Run the k6 load test with the server watching itself.

k6 measures the client side: how long a request took and how many got
through. That alone cannot tell you WHY throughput stopped climbing. The
answer is on the server, and the gateway now exports it - CPU per process,
resident memory, event-loop lag.

This runs both at once and writes them out together, so every throughput
number has the machine state that produced it sitting next to it.

    python loadtest/run_k6.py                    # the published baseline
    python loadtest/run_k6.py --minutes 5        # a five-minute run
    python loadtest/run_k6.py --scenarios control_plane --step 30

Nothing here is hard-coded: --step and the three ladders map onto the
environment variables the k6 script reads, so the profile is an input.

Writes:
    loadtest/k6-summary.json   k6's own summary (thresholds, percentiles)
    loadtest/k6-raw.json       every sample, tagged with concurrency
    loadtest/k6-server.json    server-side samples taken during the run
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "loadtest"
SCRIPT = HERE / "gateway.k6.js"

SUMMARY = HERE / "k6-summary.json"
RAW = HERE / "k6-raw.json"
SERVER = HERE / "k6-server.json"

DEFAULT_URL = os.environ.get("MG_URL", "http://127.0.0.1:8000")
KEY = os.environ.get("MG_API_KEY", "demo-secret-key")

# A request that warms the local model. Cold load is 145s on this hardware;
# a load test started cold measures the model loading, not the system.
WARM_PAYLOAD = {
    "task": "support_draft",
    "messages": [{"role": "user", "content": "My stream keeps buffering."}],
}


def k6_binary(explicit: str | None) -> str | None:
    """Prefer the copy inside the project over anything on PATH."""
    if explicit:
        return explicit if Path(explicit).is_file() else None
    local = HERE / "bin" / ("k6.exe" if platform.system() == "Windows" else "k6")
    if local.is_file():
        return str(local)
    return shutil.which("k6")


def get(path: str, timeout: float = 10.0):
    req = urllib.request.Request(
        DEFAULT_URL + path, headers={"Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=timeout) as fh:
        return json.load(fh)


def post(path: str, body: dict, timeout: float):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        DEFAULT_URL + path, data=data,
        headers={"Authorization": f"Bearer {KEY}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as fh:
            return fh.status, json.load(fh)
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception:
        return None, None


# --------------------------------------------------------------------------
# Pre-checks
# --------------------------------------------------------------------------


def preflight() -> bool:
    """Refuse to produce numbers against the wrong thing."""
    try:
        health = get("/healthz")
    except Exception as exc:
        print(f"  FAIL  gateway not answering on {DEFAULT_URL}: {exc}")
        return False

    guards = (health.get("guardrails") or {}).get("input") or []
    print(f"    gateway up, input guards: {guards or 'NONE'}")
    if not guards:
        print("  FAIL  no input guardrails loaded - this is the stale "
              "deployment failure. Restart the gateway.")
        return False

    sampler = health.get("sampler") or {}
    if not sampler.get("available"):
        print("    WARN  server-side sampling unavailable (psutil missing); "
              "the run will have client-side numbers only")
    else:
        print(f"    sampler up, {sampler.get('samples')} samples so far")

    cls = health.get("classifier") or {}
    print(f"    classifier {cls.get('name')} healthy={cls.get('healthy')}")
    if not cls.get("healthy"):
        print("  FAIL  classifier unhealthy - the compliance path would "
              "measure the failure mode, not the system.")
        return False
    return True


def warm(timeout: float) -> bool:
    """Load the local model before measuring anything."""
    print(f"    warming the local model (up to {timeout:.0f}s)...", end="",
          flush=True)
    started = time.perf_counter()
    status, _ = post("/v1/chat/completions", WARM_PAYLOAD, timeout)
    took = time.perf_counter() - started
    if status == 200:
        print(f" ready in {took:.0f}s")
        return True
    print(f" HTTP {status} after {took:.0f}s")
    return False


# --------------------------------------------------------------------------
# Server-side sampling, alongside the run
# --------------------------------------------------------------------------


class ServerWatch(threading.Thread):
    """Poll /resources while k6 runs.

    Deliberately cheap and deliberately on a thread: one small request
    every two seconds adds nothing measurable to a test whose ceiling is
    three requests a second, and the alternative - inferring CPU from
    throughput - is guessing.
    """

    def __init__(self, interval_s: float = 2.0) -> None:
        super().__init__(daemon=True)
        self.interval = interval_s
        self.samples: list[dict] = []
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                res = get("/resources", timeout=8)
                res["t"] = time.time()
                self.samples.append(res)
            except Exception:
                pass
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()


def summarise_server(samples: list[dict]) -> dict:
    """Condense the server-side samples into something readable."""
    if not samples:
        return {"samples": 0}

    def series(fn):
        out = []
        for s in samples:
            try:
                value = fn(s)
                if value is not None:
                    out.append(float(value))
            except Exception:
                continue
        return out

    def stats(values):
        if not values:
            return None
        ordered = sorted(values)
        return {
            "min": round(ordered[0], 2),
            "median": round(ordered[len(ordered) // 2], 2),
            "max": round(ordered[-1], 2),
        }

    procs = {}
    for name in ("gateway", "presidio", "ollama"):
        cpu = series(lambda s, n=name: s["processes"][n]["cpu_percent_host"])
        rss = series(lambda s, n=name: s["processes"][n]["rss_mb"])
        if cpu or rss:
            procs[name] = {"cpu_percent_host": stats(cpu),
                           "rss_mb": stats(rss)}

    first, last = samples[0], samples[-1]
    return {
        "samples": len(samples),
        "duration_s": round(last["t"] - first["t"], 1),
        "host_cpu_percent": stats(series(lambda s: s["host"]["cpu_percent"])),
        "host_memory_used_percent":
            stats(series(lambda s: s["host"]["memory_used_percent"])),
        "event_loop_lag_ms": stats(series(
            lambda s: s["event_loop"]["lag_last_ms"])),
        "event_loop_lag_floor_ms": last["event_loop"].get("lag_floor_ms"),
        "event_loop_lag_max_ms": last["event_loop"].get("lag_max_ms"),
        "processes": procs,
        # Resident memory at the start and the end. Over a long run this is
        # the first place a leak shows itself.
        "rss_drift_mb": {
            name: round(last["processes"][name]["rss_mb"]
                        - first["processes"][name]["rss_mb"], 1)
            for name in procs
            if name in first.get("processes", {})
            and name in last.get("processes", {})
        },
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run the k6 load test with server-side sampling.")
    ap.add_argument("--k6", help="path to the k6 binary")
    ap.add_argument("--step", type=int, default=22,
                    help="seconds per stage (default 22)")
    ap.add_argument("--minutes", type=float,
                    help="target total run time; sets --step to fit")
    ap.add_argument("--scenarios",
                    default="control_plane,end_to_end,compliance")
    ap.add_argument("--cp-stages", default="1,4,8,16",
                    help="control-plane concurrency ladder")
    ap.add_argument("--e2e-stages", default="1,2,4")
    ap.add_argument("--pii-stages", default="1,2,4")
    ap.add_argument("--no-warm", action="store_true",
                    help="skip pre-warming (the first requests will be slow)")
    ap.add_argument("--warm-timeout", type=float, default=300.0)
    ap.add_argument("--no-raw", action="store_true",
                    help="skip the per-sample JSON (no saturation chart)")
    args = ap.parse_args()

    binary = k6_binary(args.k6)
    if not binary:
        print("  k6 not found.\n"
              "  Put it at loadtest/bin/k6.exe, or pass --k6 <path>.\n"
              "  Without it, loadtest/run.py is the Python harness.")
        return 2

    wanted = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    ladders = {"control_plane": args.cp_stages, "end_to_end": args.e2e_stages,
               "compliance": args.pii_stages}
    total_stages = sum(len([x for x in ladders[s].split(",") if x.strip()])
                       for s in wanted if s in ladders)
    if not total_stages:
        print("  nothing to run: no scenarios selected")
        return 2

    step = args.step
    if args.minutes:
        # Spread the requested time evenly over however many stages the
        # chosen ladders add up to.
        step = max(5, int(round(args.minutes * 60 / total_stages)))

    planned = step * total_stages
    print(f"\n  k6 {binary}")
    print(f"  scenarios : {', '.join(wanted)}")
    print(f"  profile   : {total_stages} stages x {step}s "
          f"= {planned}s ({planned/60:.1f} min)")
    for name in wanted:
        if name in ladders:
            print(f"    {name:<14} concurrency {ladders[name]}")
    print()

    print("  PRE-FLIGHT")
    if not preflight():
        return 1
    if not args.no_warm and ("end_to_end" in wanted or "compliance" in wanted):
        if not warm(args.warm_timeout):
            print("  FAIL  model did not warm; the run would measure a cold "
                  "load. Use --no-warm to override.")
            return 1
    print()

    env = dict(os.environ)
    env.update({
        "MG_URL": DEFAULT_URL,
        "MG_API_KEY": KEY,
        "K6_STEP_S": str(step),
        "K6_SCENARIOS": ",".join(wanted),
        "K6_CP_STAGES": args.cp_stages,
        "K6_E2E_STAGES": args.e2e_stages,
        "K6_PII_STAGES": args.pii_stages,
        "K6_SUMMARY_OUT": str(SUMMARY).replace("\\", "/"),
    })

    cmd = [binary, "run", "--quiet"]
    if not args.no_raw:
        cmd += ["--out", f"json={str(RAW).replace(chr(92), '/')}"]
    cmd.append(str(SCRIPT))

    watch = ServerWatch()
    watch.start()
    print(f"  RUNNING - about {planned/60:.1f} minutes. Do not run anything "
          f"else on this machine:\n  two cores, and a competing process "
          f"measures itself into the result.\n")
    started = time.time()
    # k6 writes straight to the inherited stdout. Without this flush our
    # own buffered output lands after k6's and the log reads backwards.
    sys.stdout.flush()
    try:
        proc = subprocess.run(cmd, env=env, cwd=str(ROOT))
        sys.stdout.flush()
    finally:
        watch.stop()
        watch.join(timeout=5)
    elapsed = time.time() - started

    server = summarise_server(watch.samples)
    server["started_at"] = started
    server["elapsed_s"] = round(elapsed, 1)
    server["profile"] = {"step_s": step, "scenarios": wanted,
                         "ladders": {k: ladders[k] for k in wanted
                                     if k in ladders}}
    server["raw_samples"] = watch.samples
    SERVER.write_text(json.dumps(server, indent=2), encoding="utf-8")

    print(f"\n  SERVER SIDE during the run ({server['samples']} samples)")
    if server.get("host_cpu_percent"):
        h = server["host_cpu_percent"]
        print(f"    host CPU        median {h['median']:>6}%   peak {h['max']}%")
    for name, data in (server.get("processes") or {}).items():
        cpu, rss = data["cpu_percent_host"], data["rss_mb"]
        if cpu and rss:
            print(f"    {name:<9} CPU   median {cpu['median']:>6}%   "
                  f"peak {cpu['max']:>6}%    RSS {rss['median']:.0f}MB")
    lag = server.get("event_loop_lag_ms")
    if lag:
        print(f"    event-loop lag  median {lag['median']}ms   peak "
              f"{lag['max']}ms   (floor "
              f"{server.get('event_loop_lag_floor_ms')}ms)")
    drift = server.get("rss_drift_mb") or {}
    if drift:
        print("    memory drift    "
              + "   ".join(f"{k} {v:+.1f}MB" for k, v in drift.items()))

    print(f"\n  wrote {SUMMARY.name}, "
          + (f"{RAW.name}, " if not args.no_raw else "")
          + f"{SERVER.name}  in {elapsed:.0f}s")
    if proc.returncode != 0:
        print(f"\n  k6 exited {proc.returncode} - a threshold failed. That is "
              f"the harness working, not a broken run.")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
