"""Classifier comparison — efficacy, measured.

Runs every classifier over the same labelled set and reports precision,
recall and F1.

WHY THE PRIMARY METRIC IS BINARY (has_pii), not entity-level accuracy:
the POLICY decision is binary — this payload either may reach the cloud or
it may not. A classifier that finds PERSON but mislabels it LOCATION still
routes correctly. Entity-level recall is reported as a secondary signal
because it tells you WHY a classifier failed, but the gate is on the
decision, because the decision is what carries the risk.

Run:
    python evals/compare.py
    python evals/compare.py --json   # machine-readable, for the gate
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = ROOT / "llm-gateway"
sys.path.insert(0, str(GATEWAY))

from factory import build_classifier  # noqa: E402

DATASET = Path(__file__).parent / "dataset.jsonl"


def load_rows() -> list[dict]:
    return [json.loads(l) for l in DATASET.read_text(encoding="utf-8").splitlines() if l.strip()]


def evaluate(name: str, cfg: dict, rows: list[dict]) -> dict:
    clf = build_classifier(cfg)
    healthy = bool(clf.health())

    tp = fp = tn = fn = 0
    entity_hits = entity_total = 0
    misses: list[dict] = []
    false_alarms: list[dict] = []
    latencies: list[float] = []

    for row in rows:
        started = time.perf_counter()
        result = clf.classify(row["text"])
        latencies.append((time.perf_counter() - started) * 1000)

        predicted, actual = bool(result.has_pii), bool(row["has_pii"])
        if actual and predicted:
            tp += 1
        elif actual and not predicted:
            fn += 1
            misses.append({"id": row["id"], "text": row["text"], "expected": row["entities"]})
        elif not actual and predicted:
            fp += 1
            false_alarms.append({"id": row["id"], "text": row["text"], "found": list(result.entities)})
        else:
            tn += 1

        for want in row["entities"]:
            entity_total += 1
            if want in result.entities:
                entity_hits += 1

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    return {
        "classifier": name,
        "healthy": healthy,
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "entity_recall": round(entity_hits / entity_total, 3) if entity_total else 0.0,
        "median_latency_ms": round(sorted(latencies)[len(latencies) // 2], 1),
        "misses": misses,
        "false_alarms": false_alarms,
    }


CANDIDATES = {
    "regex": {"type": "regex"},
    "presidio": {"type": "presidio"},
    "composite": {"type": "composite", "members": ["regex", "presidio"]},
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = load_rows()
    results = [evaluate(n, c, rows) for n, c in CANDIDATES.items()]

    if args.json:
        print(json.dumps({"dataset_size": len(rows), "results": results}, indent=2))
        return 0

    pos = sum(1 for r in rows if r["has_pii"])
    print()
    print("=" * 78)
    print(f"  PII CLASSIFIER COMPARISON   {len(rows)} labelled payloads "
          f"({pos} positive, {len(rows) - pos} negative)")
    print("=" * 78)
    print()
    print(f"  {'classifier':<12} {'prec':>6} {'recall':>7} {'F1':>6} "
          f"{'ent.rec':>8} {'TP':>3} {'FP':>3} {'FN':>3} {'p50 ms':>8}  health")
    print(f"  {'-'*12} {'-'*6} {'-'*7} {'-'*6} {'-'*8} {'-'*3} {'-'*3} {'-'*3} {'-'*8}  ------")
    for r in results:
        print(f"  {r['classifier']:<12} {r['precision']:>6.3f} {r['recall']:>7.3f} "
              f"{r['f1']:>6.3f} {r['entity_recall']:>8.3f} {r['tp']:>3} {r['fp']:>3} "
              f"{r['fn']:>3} {r['median_latency_ms']:>8.1f}  {'up' if r['healthy'] else 'DOWN'}")

    for r in results:
        if not (r["misses"] or r["false_alarms"]):
            continue
        print()
        print(f"  --- {r['classifier']} ---")
        for m in r["misses"]:
            print(f"      MISSED      {m['id']}  {m['expected']}  {m['text'][:52]!r}")
        for f in r["false_alarms"]:
            print(f"      FALSE ALARM {f['id']}  found {f['found']}  {f['text'][:46]!r}")

    print()
    print("=" * 78)
    best = max(results, key=lambda r: r["f1"])
    print(f"  Best F1: {best['classifier']} ({best['f1']})")
    print("  Primary metric is BINARY has_pii, because the policy decision is")
    print("  binary: this payload either may reach the cloud, or it may not.")
    print("=" * 78)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
