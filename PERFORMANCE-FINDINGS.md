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
cache settling, or a leak. Five minutes cannot distinguish them. Only a
soak can, which is the gap flagged at the top of the day and still open.

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
