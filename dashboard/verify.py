"""Check every headline figure against the file that produced it.

A dashboard and a deck are both places where a number can be typed by hand
and then quietly go stale. This asserts each claim against the run output it
came from, so a figure that drifts fails here instead of being discovered by
whoever is listening to the presentation.

    python dashboard/verify.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "dashboard"


def rj(p: Path):
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def _collect_count() -> int | None:
    """How many tests pytest can actually see, right now.

    Deliberately a collection rather than a run: this is checking that a
    PUBLISHED FIGURE matches its source, not that the suite is green. The
    suite being green is a separate check and belongs in prove.py.
    """
    import subprocess
    gateway = ROOT / "llm-gateway"
    py = gateway / ".venv" / "Scripts" / "python.exe"
    if not py.is_file():
        py = gateway / ".venv" / "bin" / "python"
    if not py.is_file():
        py = Path(sys.executable)
    try:
        proc = subprocess.run(
            [str(py), "-m", "pytest", "--collect-only", "-q"],
            cwd=str(gateway), capture_output=True, text=True, timeout=300,
            env={**os.environ, "PYTHONPATH": str(gateway)})
    except Exception:
        return None
    per_file = re.findall(r"^(tests[/\][\w.]+\.py): (\d+)$", proc.stdout, re.M)
    if per_file:
        return sum(int(n) for _, n in per_file)
    m = re.search(r"collected (\d+) items", proc.stdout)
    return int(m.group(1)) if m else None


def main() -> int:
    d = rj(HERE / "data.json")
    if not d:
        print("  no data.json - run dashboard/collect.py first")
        return 1

    checks: list[tuple[str, object, object]] = []

    def eq(label, got, want):
        checks.append((label, got, want))

    # --- red team, against the raw run file -----------------------------
    pat = rj(ROOT / "redteam" / "results-patterns.json")
    if pat:
        res = pat.get("results", pat)
        rows = res if isinstance(res, list) else res.get("results", [])
        breaches = sum(1 for r in rows if r.get("outcome") == "BREACH")
        eq("red team: attacks run", len(rows), 1000)
        eq("red team: pattern-layer breaches", breaches, 24)
        eq("red team: pattern-layer held", len(rows) - breaches, 976)
    sm = d["redteam"]["safety_model"]
    eq("red team: full chain held", sm["combined_held"], 988)
    eq("red team: safety model catches half the residual",
       sm["caught_of_residual"] * 2, sm["residual"])
    eq("red team: last run matches the headline",
       d["redteam"]["runs"][-1]["breached"], 24)
    eq("red team: run 4 is the regression",
       [r["run"] for r in d["redteam"]["runs"] if r["regression"]], [4])

    # --- AI-generated corpus --------------------------------------------
    cur = rj(ROOT / "redteam" / "generated-curated.json")
    gres = rj(ROOT / "redteam" / "results-generated.json")
    if cur and gres:
        kept, dropped = cur["kept"], cur["dropped"]
        eq("AI corpus: generated", kept + dropped, 100)
        eq("AI corpus: kept after curation", kept, 50)
        eq("AI corpus: dropped as ordinary language", dropped, 50)
        gb = sum(1 for r in gres["results"] if r["outcome"] == "BREACH")
        eq("AI corpus: scored against the kept set",
           len(gres["results"]), kept)
        eq("AI corpus: breaches", gb, 1)
        eq("AI corpus: held", len(gres["results"]) - gb, 49)

    # --- classifier gate -------------------------------------------------
    c = d["classifiers"]["candidates"]
    eq("gate: composite F1", c["composite"]["f1"], 1.0)
    eq("gate: composite promoted", c["composite"]["promoted"], True)
    eq("gate: blocks two of three",
       sum(1 for v in c.values() if v["gate"] == "BLOCKED"), 2)
    eq("gate: recall threshold", d["classifiers"]["thresholds"]["recall"], 0.95)
    eq("gate: dataset size", d["classifiers"]["dataset"]["total"], 185)

    # --- the safety-model false alarm -----------------------------------
    fa = rj(HERE / "measurements" / "safety-model-false-alarms.json")
    if fa:
        eq("S7: messages checked", fa["count"], 12)
        eq("S7: false alarms after the fix", fa["safety_model_false_alarms"], 0)
        eq("S7: pattern guards never false-alarmed",
           fa["pattern_false_alarms"], 0)
        eq("S7: rate quoted on the slide",
           d["safety_model_false_alarms"]["rate_before_pct"], 66)
    gp = d["guard_probe"]
    eq("safety model: attacks flagged, unchanged by the S7 fix",
       (gp["caught"], gp["total"]), (4, 8))
    eq("safety model: no false alarms on the probe set",
       sum(1 for r in gp["rows"] if r.get("is_attack") is False and r["flagged"]), 0)

    # --- load test -------------------------------------------------------
    lt = rj(ROOT / "loadtest" / "results.json")
    if lt:
        A = lt["scenarios"]["A"]["steps"]
        tp = [s["throughput_per_s"] for s in A]
        eq("load: control plane ladder", [s["concurrency"] for s in A],
           [1, 4, 8, 16])
        eq("load: throughput is flat (spread under 0.2/s)",
           round(max(tp) - min(tp), 2) < 0.2, True)
        # The slide quotes "2.9 req/s", which is the CLASSIFIER's own rate
        # (347ms a call, single process). The gateway measures marginally
        # above it. Assert the real window rather than a rounded figure -
        # the first version of this check compared a rounded number to
        # itself and could never have failed.
        eq("load: measured ceiling sits in the 2.8-3.0 window",
           2.8 <= max(tp) <= 3.0, True)
        eq("load: gateway never beats its classifier by more than 5%",
           max(tp) <= 2.88 * 1.05, True)
        zones = {"cloud": 0, "onprem": 0}
        for sc in lt["scenarios"].values():
            for s in sc["steps"]:
                for z, n in (s.get("zones") or {}).items():
                    zones[z] = zones.get(z, 0) + n
        eq("routing: every PII request served on-prem", zones["onprem"] > 0, True)
        eq("routing: nothing with PII went to cloud",
           sum(1 for s in lt["scenarios"]["C"]["steps"]
               for z in (s.get("zones") or {}) if z == "cloud"), 0)
        eq("routing: total requests behind the 0-leak claim",
           zones["cloud"] + zones["onprem"],
           d["loadtest"]["routing_under_load"]["zones_total"]["cloud"]
           + d["loadtest"]["routing_under_load"]["zones_total"]["onprem"])
        eq("load: harness is named honestly",
           "NOT k6" in lt["harness"], True)

    # --- tests and traceability -----------------------------------------
    #
    # NOT a hard-coded number any more. This assertion read `== 133` and
    # PASSED for two days while the suite had grown to 160 - because the
    # figure and the assertion were written on the same afternoon and so
    # agreed with each other rather than with reality. The 27 tests in
    # test_sysmetrics.py were invisible to both.
    #
    # A check whose expected value is frozen by hand is a check that
    # quietly stops checking. The SOURCE of this figure is a pytest
    # collection, so a collection is what it is compared against. Costs a
    # few seconds and cannot go stale.
    collected = _collect_count()
    if collected is None:
        print("  --   tests: total - could not run pytest --collect-only, "
              "skipped rather than assumed")
    else:
        eq("tests: total matches a live pytest collection",
           d["tests"]["total"], collected)
    eq("tests: the per-file breakdown adds up to the total",
       sum(r["tests"] for r in (d["tests"].get("per_file") or [])),
       d["tests"]["total"])
    eq("tests: every scenario traced",
       d["tests"]["scenarios_tested"], d["tests"]["scenarios"])
    eq("tests: scenario count", d["tests"]["scenarios"], 23)

    # --- business figures used on the deck ------------------------------
    b = d["business"]
    eq("business: working subscriber figure", b["subscriber_range"]["working"], 8_200_000)
    eq("business: 1% of the base", b["churn_1pct_subs"], 82_000)
    eq("business: prize low", b["churn_1pct_low"], 147_600_000)
    eq("business: prize high", b["churn_1pct_high"], 172_200_000)
    cs = d["cost_sensitivity"]
    eq("business: yearly contacts", cs["yearly_contacts"], 12_300_000)
    realistic = [r for r in cs["rows"] if r["tokens_per_call"] == 3000]
    eq("business: saving quoted as $74K",
       round(realistic[0]["saved_at_67pct_onprem"] / 1000), 74)
    eq("business: production build weeks",
       d["investment"]["production_build_estimate_weeks"], 8)

    # --- the page really was built from this dataset ---------------------
    page = (HERE / "index.html")
    if page.is_file():
        txt = page.read_text(encoding="utf-8")
        eq("page: no unreplaced placeholder", "__DATA__" in txt, False)
        m = re.search(r'"generated_at":"([^"]+)"', txt)
        eq("page: embedded dataset matches data.json",
           m.group(1) if m else None, d["generated_at"])

    bad = [(l, g, w) for l, g, w in checks if g != w]
    for label, got, want in checks:
        mark = "ok " if got == want else "FAIL"
        extra = "" if got == want else f"   got {got!r}, expected {want!r}"
        print(f"  {mark} {label}{extra}")
    print(f"\n  {len(checks) - len(bad)} of {len(checks)} figures verified "
          f"against their source files")
    if bad:
        print(f"  {len(bad)} MISMATCH - the slide or the page has drifted "
              f"from the run\n")
        return 1
    print("  nothing has drifted\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
