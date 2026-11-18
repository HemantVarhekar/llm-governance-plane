# Performance findings — from the process and event-loop metrics

Added 12 November. Everything here was measured on this machine: Windows 11,
2 physical cores, 4 logical, 20GB. Every figure has the command that
produced it.

The instrument is `llm-gateway/sysmetrics.py`, read through
`GET /resources` or scraped from `GET /metrics`. It exports 107 series.

---

## Know the instrument before trusting it

Two numbers have to be established before any lag reading means anything,
and both are measured rather than assumed.

**Timer noise floor: 5.4–6.4ms.** Windows runs a ~15.6ms system timer, so
`asyncio.sleep()` overshoots on a completely idle loop. The gateway
measures this at startup on a known-idle loop, before the sampler takes
its first process scan, and publishes it as
`mg_event_loop_lag_floor_seconds`. **Lag is only signal above the floor.**
Reporting a 6ms reading as "event-loop lag" would be reporting the clock.

**Startup is not steady state: 183–420ms.** Building the pipeline, the
first provider health sweep and the first process-table scan all land in
the first seconds. These are excluded from the histogram and reported
separately as `mg_event_loop_lag_startup_max_seconds`. A lifetime maximum
would otherwise be pinned to that spike, and a one-hour soak would report
its worst case as something that happened before it served a request.

**Cost of measuring:** 16.9ms median per sample, 54ms worst, every 5
seconds — a duty cycle of **0.34%**. The first sample after start costs
417ms, which is why it is kept out of the steady-state figures.

---

## F-P1 — The gateway is saturated on ONE core, not blocked and not starved

**The strongest finding, and the one that reframes the throughput ceiling.**

96 requests that are refused by the injection guard — no model call at all,
no Presidio call — sustained only **3.1 per second** at 8 concurrent, with
event-loop lag reaching **2.36 seconds** against a 5.4ms floor.

Three hypotheses, and the measurement kills two of them:

| Hypothesis | Prediction | Measured | Verdict |
|---|---|---|---|
| A blocking call on the request path | lag high, gateway CPU low | gateway at 95% of a core | **no** |
| Other processes starving it | host CPU pinned near 100% | host 60% median, 64% peak | **no** |
| Single event loop, single core | gateway ≈ 1.00 core, host has slack | **23.8% of a 4-logical host = 0.95 cores** | **yes** |

```
  host CPU %   gateway  presidio   ollama    lag ms
        64.2      23.5       0.9      0.2    1311.95
        59.6      23.3       1.1      0.4     657.28
        59.5      23.8       1.2      0.0     644.42
```

Presidio at ~1% and Ollama at ~0% while the gateway pins a core: the work
is the guard chain itself — text normalisation and roughly fifteen regular
expression families — and it is CPU-bound code running **on** the event
loop. Three logical cores sit idle because one uvicorn process has one
event loop.

Checked and ruled out: every provider uses `httpx.AsyncClient`, every
guard is awaited, and the guard chain returns on the first refusal, so the
safety model is never reached on this path.

**Remediation, in order of honesty about cost:** run uvicorn with workers
equal to physical cores; or move pattern matching to a process pool. Both
are measurable against this baseline, which is the point of having it.

**This also qualifies the earlier "2.9 requests per second belongs to
Presidio" finding.** That remains true of the full compliance path. The
guard-only path has a *separate* ceiling at ~3.1 per second that is the
gateway's own single core. Two different bottlenecks on two different
paths, and only measuring per-process CPU separates them.

---

## F-P2 — The background health sweep stalls the event loop every 15 seconds

On a completely idle gateway, lag spikes of **89ms and 213ms** recur with a
measured gap of **16.1 seconds**, against `MG_HEALTH_INTERVAL_S = 15`.

```
   t= 44.3s   lag_last=  212.89 ms
   t= 60.4s   lag_last=   88.73 ms
   gaps between spikes: [16.1]
```

Every request in flight when the sweep runs pays that delay, and nothing
in the request's own timing explains where it went. The sweep is already
`async`, so this is contention rather than a blocking call — three
provider health checks, one of them across the network, competing for one
core with everything else.

**Not fixed, deliberately.** Changing the sweep now would destroy the
baseline the load test is about to establish. Fix it after, and show the
before and after.

---

## F-P3 — Thread count is the wrong question for this service

`mg_py_threads` reads **2** and `mg_proc_threads{proc="gateway"}` reads
**6**, flat, under all load. That is not a healthy signal — it is a
meaningless one. Uvicorn serves on one event loop in one thread, so there
is no worker pool filling and draining.

The metrics that answer what "threads active versus inactive" is reaching
for, on an asyncio service:

| Instead of | Use | Reads |
|---|---|---|
| threads active | `mg_inflight` | concurrency actually carried |
| pool saturation | `mg_event_loop_lag_seconds` | 5.4ms floor → 2.36s under load |
| queue depth | `mg_shed_total` | requests refused at capacity |
| — | `mg_proc_cpu_percent_host` | **which process** owns the CPU |

---

## F-P4 — The per-process breakdown was missing the biggest consumer

**Found by the load test, in my own instrument.**

The first k6 run showed host CPU peaking at **100%** while the gateway,
Presidio and Ollama together accounted for about a quarter of it. Ollama
reported **78MB** resident — impossible for a loaded three-billion-
parameter model.

The model runner is `llama-server.exe`, living under the Ollama
installation. The group matched `ollama*`, so it found the 78MB supervisor
and missed **3.7GB** and nearly all the inference CPU. Corrected, Ollama
resolves to 4 processes, 3,898MB, 58 threads, and 41.5% of the host at
median under load.

Two fixes, and the second matters more:

1. The group matches both names, scoped to the Ollama install directory so
   an unrelated `llama-server` is not charged to us.
2. **The breakdown now publishes what it cannot explain.**
   `mg_host_cpu_unattributed_percent` is host CPU minus everything
   attributed. A breakdown that silently omits the largest consumer is
   worse than none, because it invites a confident wrong conclusion.

That residual still reads **25.6% median, 62.6% peak** — correctly. It is
the Claude desktop application, Chrome, Windows Defender and a stray JVM
on a two-core laptop. On a server it would be near zero. The point is that
nobody has to guess: the number is on the page.

---

## F-P5 — The control plane is saturated at concurrency 1

The ladder, measured with each level held flat for 30 seconds:

| Concurrency | requests | median | p95 | throughput |
|---|---|---|---|---|
| 1 | 96 | 288ms | 589ms | 3.20/s |
| 4 | 92 | 1,225ms | 2,317ms | 3.07/s |
| 8 | 97 | 2,518ms | 3,121ms | 3.23/s |
| 16 | 114 | 4,297ms | 5,955ms | 3.80/s |

Concurrency rises 16-fold. Throughput moves from 3.20 to 3.80 per second.
Latency grows almost exactly linearly with concurrency.

**Little's law confirms the measurement is self-consistent** — in a stable
system, concurrency equals throughput times latency:

| Level | throughput × latency | error |
|---|---|---|
| 1 | 0.92 | 8% |
| 4 | 3.76 | 6% |
| 8 | 8.13 | 2% |
| 16 | 16.33 | 2% |

Within 2–8% at every rung. The plateau is real, not an artefact of the
harness. Every unit of concurrency added beyond the first becomes queue
time rather than work — consistent with F-P1, the single event loop on a
single core.

---

## F-P6 — An aggregate failure threshold hid a 43% failure on one path

The run reported **all thresholds passed**. It should not have.

The compliance path returned **HTTP 503 on 3 of its 7 requests**. Those
three were 0.7% of the 429 requests in the run as a whole, so they passed
under a 5% global limit. An aggregate rate across scenarios with wildly
different request counts lets the busiest path vote for the quietest one —
and the control plane sent 399 of those 429.

Two separate defects behind it:

- **No per-scenario thresholds.** Added:
  `http_req_failed{scenario:<name>} rate<0.10` for every scenario in the
  run.
- **k6 did not know what success meant.** By default any non-2xx is a
  failure, so the control plane — where HTTP 400 is the *correct* outcome —
  looked 100% failed the moment a real per-scenario threshold was applied.
  Each request now declares its expected statuses, so `http_req_failed`
  means "the gateway broke" rather than "the response was not 200". 429 is
  expected everywhere: shedding at capacity is designed behaviour.

The 503s themselves are a capacity finding, not a bug: the compliance path
had a median of 36 seconds under concurrent load and exceeded the chain
deadline. **The on-prem path cannot serve four concurrent requests carrying
personal data within its deadline on this hardware.**

---

## F-P7 — Ollama's resident memory grows with traffic and is never released

The question a soak exists to answer, answered. Two earlier five-minute
runs saw Ollama gain 91MB and 14MB and could not tell a cache settling
from a leak. A 60-minute run can.

| | |
|---|---|
| Resident memory, minute 10 → minute 60 | **3,980MB → 5,634MB** |
| Least-squares slope over the 57-minute trend window | **+1,662MB/hour** |
| Shape | **monotone, no plateau** |
| Processes / threads / handles | **3 / 41 / flat** throughout |
| Host memory | 77% → **88%** |

Not a cache settling: a cache approaches a plateau, and this did not bend
over fifty minutes. Not process churn and not a descriptor leak either —
the process, thread and handle counts never moved, so the growth is inside
a stable process set.

**But it is not a time-based leak.** Measured again after the run, with the
services still up and no traffic at all, resident memory was **byte-identical
across 100 seconds, five minutes after the last request**:

```
llama-server 1744 (llama-guard3:1b)  1,616.93MB  ->  1,616.93MB
llama-server 9860 (llama3.2:3b)      3,977.97MB  ->  3,977.97MB
```

So the growth is proportional to **requests served**, not to time, and the
memory is never returned: roughly **4.0MB per request** retained across the
411 requests in the growth window. That is consistent with per-request
context or KV-cache allocation accumulating in `llama-server`, though this
instrument cannot prove the mechanism — see the limitation below.

**The operational consequence does not depend on the mechanism.** At this
rate, the remaining headroom on a 16GB host is about four more hours of
this traffic. The requirement is therefore not a bug fix but a deployment
rule: **the model server needs a resident-memory ceiling and a scheduled
recycle, and the control plane has to drain in-flight work before it.**
Treating a model server as a long-lived process without a recycle policy is
the actual defect.

**Limitation of the instrument, not of the finding.** The sampler groups
`ollama.exe` and both `llama-server.exe` processes into one `ollama` total,
so it can say the group grew 1,654MB but cannot attribute it per model. The
idle snapshot above had to come from outside the sampler. Per-model
attribution is the next change to `sysmetrics.py` — the same class of gap
as F-P4, where the group matcher missed the process holding the weights.

---

## F-P8 — Enforcing the privacy constraint cost 30% of the personal-data path

Forty-nine of 489 requests returned HTTP 503. **Every one of them was on the
personal-data path**, and none on any other:

| Path | Requests | Outcome |
|---|---|---|
| ordinary | 245 | **200**, cloud, median 8,698ms |
| personal data | 114 | **200**, on-prem, median 21,590ms |
| personal data | **49** | **503** — 30% of that path |
| attack | 81 | **400**, refused, 81 of 81 |

Aggregate error rate: **10%**. Per-path: **30%**. F-P6 is why that
distinction is now instrumented rather than discovered twice.

The audit trail names the cause without ambiguity — 49 events,
`outcome: unavailable`, `all providers in the chain were unavailable` — and
the mechanism follows from the policy:

1. Personal data is a **hard constraint** that filters cloud out of the
   provider list before cost or latency are considered.
2. What remains is **one** on-prem provider. By design there is no fallback,
   because a fallback would be the cloud.
3. On-prem on two CPU cores runs at a median of **21.6s** against the cloud
   path's 8.7s, with **p95 38.3s** and a maximum of 41.9s.
4. The chain deadline is **40s** — sitting directly on top of that upper
   tail. Requests past it are shed.

`policy.yaml` had already flagged this number as the weak point, before any
of it was measured:

> `PROVISIONAL: 40s is a judgement, not a measurement. One local call costs`
> `6-10s on this hardware, so a two-hop chain can legitimately need ~20s.`
> `TO BE REVISITED after load testing - this number should come from a`
> measurement.

This is that measurement. A guessed deadline that is annotated as a guess
gets checked; a guessed deadline that reads as a decision does not.

The failures were **evenly spread across the whole hour** (four or five per
five-minute bucket, first bucket to last), so this is a constant structural
rate at this concurrency, not a degradation over time.

**The safety properties held throughout.** Zero personal-data items reached
cloud. Zero attacks were answered. Every one of the 81 injection attempts
was refused. The 503 is a fail-closed refusal — availability was lost and
compliance was kept, which is the correct direction to fail — but a 30%
error rate is not a shippable answer, and "it failed safe" is not a defence
of it.

**This is the number the production architecture has to answer**, and it is
the honest cost of the governance claim: the constraint that makes the
system compliant is the same constraint that makes it slow, because it
moves work onto the scarcer resource and removes the fallback. Replicated
classifiers and GPU inference nodes are not a wish-list item here; they are
what this measurement requires. The alternative — raising the deadline —
hides the queue rather than clearing it.

---

## F-P9 — An hour of observation confirmed the single-core finding, and found no latency drift

| | Measured | Verdict |
|---|---|---|
| Event-loop lag, median | **16.42ms** against a measured **6.36ms** floor | blocked |
| Event-loop lag, p95 | **1,857ms** | blocked |
| Event-loop lag, max | **7,064ms** | blocked |
| Latency, first tenth → last tenth | 10,298ms → **9,319ms**, **−9.5%** | PASS |
| Gateway memory | +10.0MB/hour | PASS |
| Presidio memory | −5.5MB/hour | PASS |
| Handles, gateway / Presidio | +0.3/hour / +0.0/hour | PASS |

The loop stalls for whole seconds at a time — F-P1 and F-P5, now observed
over an hour rather than inferred from a ladder.

**Latency did not drift, and that is the point worth keeping.** The system
got slightly *faster* across the hour while Ollama was accumulating 1.6GB.
A latency-based or response-time-based soak would have returned a clean
pass. Only a memory-trend instrument caught it — which is the argument for
measuring resources alongside responses rather than inferring one from the
other.

---

## Measurement caveats, stated rather than buried

**Sample size.** The compliance path produced **7 samples** across four
concurrency levels, and end-to-end produced 23. A p95 over seven
observations *is* the maximum. The summary now flags any metric under 20
samples and says to read the median and the maximum instead. These paths
wait on a model, so more samples means a longer run, not more load.

**Contention.** Two physical cores, and roughly a quarter of them belong to
the desktop environment. Anything else running during a measurement enters
the result — this already invalidated the timing side-channel test once.
`run_k6.py` says so before it starts.

**Ollama grew 91MB of resident memory in five and a half minutes.** Model
cache settling, or a leak. Five minutes cannot distinguish them. **Closed by
the 60-minute soak on 2026-11-17 — see F-P7.** It is neither: the memory
grows with requests served, not with time, and is never released.

**The soak measured one concurrency on one host.** Concurrency 2, two
physical cores, 489 requests. The 30% refusal rate in F-P8 is that
configuration's rate, not a property of the design; the per-request memory
retention in F-P7 is a rate per request and would scale with throughput, not
with uptime. Neither figure transfers to different hardware without
re-measuring.

---

## What is now exported

107 series. Per process (`gateway`, `presidio`, `ollama`, each a *group* of
operating-system processes): CPU seconds by user and system mode, CPU
percent of a core and of the host, resident and virtual memory, threads,
handles, read and write bytes, process count, start time, liveness. Host:
CPU overall and per core, physical and logical core count, memory, swap.
Runtime: garbage collection by generation, Python threads, asyncio tasks.
Event loop: lag histogram, last, steady-state max, startup max, measured
floor, warm flag.

Design decisions worth defending:

- **CPU is a counter of seconds, not only a percent gauge.** A percent is
  already a derivative over a window the scraper did not choose.
- **Group counters accumulate non-negative per-process deltas.** Summing
  members' lifetime totals would make the counter *fall* whenever Ollama
  retires a model runner, and Prometheus reads a falling counter as a
  restart — a phantom rate spike on every model unload. Tested.
- **A dead component publishes `mg_proc_up 0` rather than disappearing.** A
  series that vanishes is indistinguishable from a failed scrape.
- **Host details are behind authentication at `/resources`.** `/healthz`
  stays open because liveness probes must not need a credential, so it
  says only whether telemetry works, never how much memory the box has.
- **psutil is optional.** If it is missing the gateway boots and serves
  normally and reports `mg_sampler_available 0`. Monitoring must not be
  able to take down the thing it monitors.

## Reproduce

```bash
curl -s -H "Authorization: Bearer $MG_API_KEY" http://127.0.0.1:8000/resources
```

```bash
python -m pytest tests/test_sysmetrics.py -q
```
