"""Promotion gate — a classifier may not ship unless it clears the bar.

This is enforcement point 3 from ARCHITECTURE.md section 8: CI.

The thresholds are deliberately ASYMMETRIC. Recall is set higher than
precision because the two errors are not equally expensive:

    a MISSED PII payload  -> customer data reaches the cloud. A breach.
    a FALSE ALARM         -> a harmless question is served on-prem.
                             Slower and cheaper. Nobody is harmed.

A gate that treats those as equivalent is a gate written by someone who has
not thought about which way the system should fail.

Run:
    python evals/gate.py                 # gate the configured classifier
    python evals/gate.py --all           # report every candidate
    python evals/gate.py --promote composite
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "llm-gateway"))
sys.path.insert(0, str(Path(__file__).parent))

from compare import CANDIDATES, evaluate, load_rows  # noqa: E402

REGISTRY = Path(__file__).parent / "registry.json"

# The bar. Recall dominates because a miss is a breach and a false alarm is
# merely an inconvenience.
THRESHOLDS = {"recall": 0.95, "precision": 0.90, "f1": 0.92}


def gate(result: dict) -> tuple[bool, list[str]]:
    failures = [
        f"{metric} {result[metric]:.3f} < required {required:.2f}"
        for metric, required in THRESHOLDS.items()
        if result[metric] < required
    ]
    return (not failures), failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="evaluate every candidate")
    ap.add_argument("--promote", help="record this candidate as promoted")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = load_rows()
    results = [evaluate(n, c, rows) for n, c in CANDIDATES.items()]

    registry = {
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": "evals/dataset.jsonl",
        "dataset_size": len(rows),
        "thresholds": THRESHOLDS,
        "candidates": {},
    }

    exit_code = 0
    if not args.json:
        print()
        print("=" * 78)
        print("  PROMOTION GATE")
        print(f"  required: recall >= {THRESHOLDS['recall']}, "
              f"precision >= {THRESHOLDS['precision']}, f1 >= {THRESHOLDS['f1']}")
        print("=" * 78)
        print()

    for r in results:
        passed, failures = gate(r)
        registry["candidates"][r["classifier"]] = {
            "precision": r["precision"], "recall": r["recall"], "f1": r["f1"],
            "entity_recall": r["entity_recall"],
            "median_latency_ms": r["median_latency_ms"],
            "gate": "PASS" if passed else "BLOCKED",
            "blocking_reasons": failures,
            "promoted": args.promote == r["classifier"],
        }
        if not args.json:
            mark = "PASS   " if passed else "BLOCKED"
            print(f"  {mark}  {r['classifier']:<12} "
                  f"recall={r['recall']:.3f}  precision={r['precision']:.3f}  "
                  f"f1={r['f1']:.3f}  p50={r['median_latency_ms']:.0f}ms")
            for f in failures:
                print(f"           -> {f}")

    REGISTRY.write_text(json.dumps(registry, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(registry, indent=2))
        return 0

    promoted = [n for n, c in registry["candidates"].items() if c["promoted"]]
    print()
    if args.promote:
        entry = registry["candidates"].get(args.promote)
        if entry is None:
            print(f"  unknown candidate {args.promote!r}")
            return 2
        if entry["gate"] != "PASS":
            print(f"  REFUSED to promote {args.promote}: it did not clear the gate")
            for f in entry["blocking_reasons"]:
                print(f"     {f}")
            exit_code = 1
        else:
            print(f"  promoted: {args.promote}")
    print(f"  registry written: {REGISTRY.relative_to(ROOT)}")
    blocked = [n for n, c in registry["candidates"].items() if c["gate"] != "PASS"]
    if blocked:
        print(f"  BLOCKED from promotion: {', '.join(blocked)}")
    print("=" * 78)
    print()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
