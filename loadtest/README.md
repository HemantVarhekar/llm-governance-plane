# Load testing

Two harnesses, on purpose.

| | `run.py` | `run_k6.py` + `gateway.k6.js` |
|---|---|---|
| Needs | nothing beyond the virtual environment | the k6 binary |
| Used for | the originally published figures | every figure from 12 November on |
| Labelled | `"harness": "... NOT k6"` in results.json | k6's own summary format |

`dashboard/verify.py` asserts that label, so a Python-harness number can
never quietly be presented as a k6 one.

## Getting k6

The binary is **not** committed — it is 64MB, and a tool belongs in the
environment rather than in the repository. `.gitignore` excludes
`loadtest/bin/`.

```bash
winget install GrafanaLabs.k6
```

Then either leave it on `PATH`, or keep the project self-contained:

```bash
mkdir -p loadtest/bin && cp "/c/Program Files/k6/k6.exe" loadtest/bin/
```

`run_k6.py` prefers `loadtest/bin/k6.exe`, then `PATH`, then `--k6 <path>`.
On Linux or in CI, `grafana/setup-k6-action` does the same job.

## Running it

```bash
python loadtest/run_k6.py --minutes 5
```

The profile is an input, not a property of the script:

| Flag | Default | What it does |
|---|---|---|
| `--minutes` | — | total run time; divides evenly across the stages |
| `--step` | 22 | seconds per stage, if you would rather set it directly |
| `--scenarios` | all three | `control_plane`, `end_to_end`, `compliance` |
| `--cp-stages` | `1,4,8,16` | control-plane concurrency ladder |
| `--e2e-stages` | `1,2,4` | end-to-end ladder |
| `--pii-stages` | `1,2,4` | compliance ladder |
| `--no-warm` | off | skip pre-warming (the first requests measure a cold model) |
| `--no-raw` | off | skip the per-sample file, and the saturation chart with it |

The k6 script reads the same settings from the environment, so it also
runs standalone:

```bash
K6_STEP_S=30 K6_CP_STAGES=1,4,8,16 loadtest/bin/k6.exe run loadtest/gateway.k6.js
```

## What the runner does that k6 alone does not

**It refuses to measure the wrong thing.** Before any load it checks the
gateway is up, that input guardrails are actually loaded, and that the
classifier is healthy. A gateway with no guards once served attacks for
eight hours while every in-process test passed; that failure cannot
silently become a load-test result.

**It pre-warms the model.** Cold load is 145 seconds on this hardware. A
run started cold measures the model loading.

**It watches the server.** k6 measures the client side and cannot say why
throughput stopped climbing. `run_k6.py` polls `/resources` every two
seconds and records CPU per process, resident memory and event-loop lag
beside the throughput figures. That is how the 2.9-per-second ceiling was
attributed to Presidio rather than the gateway, and how a missing 3.7GB of
Ollama was caught.

## Output

| File | Contents |
|---|---|
| `k6-summary.json` | k6's own summary: thresholds, percentiles, counters |
| `k6-raw.json` | every sample, tagged with `level`, `vus` and `scenario` |
| `k6-server.json` | server-side samples taken during the run |
| `k6-run.log` | console transcript |

`k6-raw.json` is the saturation chart's input. Each request carries the
concurrency level it was sent at, so throughput against concurrency can be
rebuilt after the fact.

## Reading the numbers honestly

**Each level is held flat.** `ramping-vus` interpolates continuously
between targets, so a naive ladder never sits still at any concurrency and
produces a smear rather than four points. Each level is a zero-second jump
followed by a hold.

**Scenarios never overlap.** Two load profiles sharing two cores measure
each other. Start times are derived from the preceding scenario's length,
so changing the step cannot make them collide.

**Percentiles below 20 samples are flagged.** The compliance path waits on
a model and yields single-figure sample counts in a five-minute run; its
p95 is the maximum wearing a lab coat, and the summary says so.

**Failure thresholds are per scenario as well as global.** A 43% failure
rate on a 7-request path is 0.7% of a 429-request run. The aggregate alone
passes it.

**Expected statuses are declared per scenario.** HTTP 400 is the correct
answer to an attack, so on the control plane it is a success. 429 is
expected everywhere — shedding at capacity is designed behaviour.

**Nothing else may run on the machine.** Two cores. A competing process
measures itself into the result; it has already invalidated one run.
