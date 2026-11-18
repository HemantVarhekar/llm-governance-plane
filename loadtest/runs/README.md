# Run archive

Each folder here is one **execution**, archived write-once on the day it ran.
Nothing in this directory is ever edited after it lands — if a number is
wrong, the fix is another run, not a correction to a past one.

| Run | Date | What | Verdict |
|---|---|---|---|
| [`load-2026-11-12/`](load-2026-11-12/) | 2026-11-12 | k6 load ladder, 1→16 virtual users, 401 samples | saturation found at concurrency 1 |
| [`run1/`](run1/) | 2026-11-17 | 60-minute soak, 2 virtual users, 489 samples | **FAIL** — memory retained per request; 30.1% of the personal-data path shed |

## Why there are two copies of each file

`loadtest/*.json` is **the latest run**. That is what `prove.py`,
`dashboard/collect.py` and `dashboard/verify.py` read, and what the next
execution overwrites. This directory is the **archive**, so a past result
survives the next run.

They are byte-identical when a run has just finished, and they diverge the
moment another one starts. **The archive is authoritative for a specific
dated claim; the live path is authoritative for "what does the gate say
now".** Any published figure that names a date should be traceable to a
folder here.

## Reproducing an execution

```
python loadtest/preflight.py
python loadtest/run_k6.py
python loadtest/soak.py --minutes 60 --concurrency 2 --sample-interval 10
```

The soak requires a live gateway, Presidio and Ollama, exits non-zero on a
FAIL verdict, excludes the first 3 minutes as warm-up, and refuses to report
a per-hour slope from under 20 minutes of data.

**A soak needs a quiet machine.** Both local models and the load generator
share two cores, so anything else running enters the measurement. This has
already invalidated one run.
