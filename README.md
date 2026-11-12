# LLM Governance Plane

[![Evidence](https://github.com/HemantVarhekar/llm-governance-plane/actions/workflows/evidence.yml/badge.svg)](https://github.com/HemantVarhekar/llm-governance-plane/actions/workflows/evidence.yml)

A control plane that sits in front of every machine-learning and
large-language-model call, so each one is **observable, guard-railed,
cost-routed, compliance-bound and fallback-protected** — and so every
decision it made can be proved afterwards.

The reference implementation is called **MigrationGuard**, built around a
specific problem: a pay-television operator moving millions of households
from satellite to streaming, where every migration is a churn risk and
every support interaction carries personal data.

```bash
python prove.py          # run every gate and write the evidence catalogue
```

---

## The thesis

The gateway is commodity. LiteLLM, Portkey, Kong AI Gateway and the cloud
vendors' own gateways all route and fall back, and I would buy one.

**What nobody sells is the evidence layer**: the adversarial corpus, the
promotion gates, the traceability, the audit trail that answers *why* a
model made a particular decision about a particular customer. That is the
part worth building, and it is what this repository is.

---

## What is actually here

| | |
|---|---|
| **Policy as code** | One versioned file. Personal data is a **hard constraint** evaluated before cost or latency — a preference can be outvoted, a constraint cannot |
| **Fail closed** | If the personal-data classifier is unavailable, cloud egress is refused entirely and the request degrades on-premises. Safety without an outage |
| **Guardrails** | Prompt-injection and credential-extraction patterns on the way in, format and leak checks on the way out, per model |
| **Fallback chains** | Driven by an error taxonomy: retryable and non-retryable failures are not the same thing |
| **Promotion gate** | A detector below threshold cannot be promoted. It currently blocks two of three candidates |
| **Adversarial corpus** | 1,000 hand-written attacks plus a curated, frozen, model-generated set |
| **Traceability** | Scenarios and tests reconciled by *parsing both sides*, never by maintaining a table |
| **Observability** | 107 metric series in Prometheus format: CPU and memory per process, event-loop lag, in-flight requests, shed rate, provider health |
| **Load testing** | k6, with each concurrency level held flat, plus server-side sampling so throughput has the machine state beside it |

Design patterns are load-bearing rather than decorative: Adapter at the
provider boundary (no vendor name appears in the pipeline), Factory for
construction, Strategy for routing, and a frozen value object for results.

---

## Prove it

Everything is runnable. `prove.py` runs every gate and writes
`evidence/catalogue.json`, `.md` and `.txt`.

```bash
python prove.py                    # everything needing no running service
python prove.py --full             # also preflight and the load test
python prove.py --category security
```

A check reports one of four outcomes and they are kept apart:

| | |
|---|---|
| `PASS` | ran, and met its criterion |
| `FAIL` | ran, and did not |
| `SKIP` | **could not run, and says why** |
| `ERROR` | the check itself broke |

A harness that folds `SKIP` into `PASS` is how a suite comes to report
green while proving nothing. That is not hypothetical here — see the
findings below.

Continuous integration runs **this same command**, so what passes on every
push is the thing demonstrated in a room, not a parallel pipeline that
drifts from it.

---

## The stack it runs on

Four models, each with a different job. None of them is hard-coded
anywhere in the pipeline — the Adapter boundary means a provider is a
config entry, and swapping one is a YAML line rather than a release.

| Role | Model | Runtime | Zone | Cost /1k |
|---|---|---|---|---|
| Drafting | **`llama3.2:3b`** | Ollama | on-prem | $0.00 |
| Safety classification | **`llama-guard3:1b`** | Ollama | on-prem | $0.00 |
| Cloud drafting | **`claude-sonnet-5-5`** | Anthropic API | cloud | $0.003 |
| Personal-data detection | **Presidio + spaCy `en_core_web_sm`** | HTTP service | on-prem | $0.00 |

The cloud cost being non-zero is the point: it makes routing a real
economic choice rather than a demonstration.

**Guardrails** — input: `injection`, `safety_model`. Output: `format`.
**Classifier**: `composite` over `regex` + `presidio`, `require_all_healthy: true`, so the gate fails closed.

### The promotion gate, on 185 labelled samples

Thresholds set *before* evaluation: recall ≥ 0.95, precision ≥ 0.90, F1 ≥ 0.92.
Asymmetric on purpose — a miss is a data breach, a false alarm is a slower answer.

| Candidate | Recall | Precision | F1 | Gate |
|---|---|---|---|---|
| `regex` | 0.676 | 1.000 | 0.806 | **BLOCKED** |
| `presidio` | 0.784 | 1.000 | 0.879 | **BLOCKED** |
| `composite` | 1.000 | 1.000 | 1.000 | **promoted** |

Neither detector alone clears the bar. That is the finding: regex cannot
find a name, Presidio alone misses structured identifiers, and the
composite is the only thing that passes. A gate that promoted everything
would prove nothing.

### Scenarios — 23, every one traced to a test

`governance` 6 · `security` 5 · `performance` 3 · `cost` 2 · `resilience` 2 · `architecture` 2 · `reliability` 1 · `operability` 1 · `quality` 1

### Libraries

`Python 3.13` · `fastapi 0.162.2` · `uvicorn 0.54.0` · `pydantic 2.14.3` · `httpx 0.28.1` · `PyYAML 6.0.3` · `presidio-analyzer 2.2.366` · `spacy 3.8.18` · `psutil 6.1.1` · `pytest 9.2.0` · `k6 2.3.0`

Pinned from the environment that passes, not from memory. The first
version of `requirements.txt` was hand-written and seven of its nine pins
were wrong; CI died in twelve seconds and the commit message lists every
one of them.

---

## Measured

Every figure traceable to the run that produced it. `python
dashboard/verify.py` asserts each one against its source file, so a number
that drifts fails a check instead of being found by whoever is listening
to the presentation.

**The latest CI run, in full:**

```
15 passed · 0 failed · 1 skipped · 0 errored   in 53.5s

tests 159 passed | scenarios 23/23 | redteam 976/1000 held (24 residual)
generated 49/50 held | gate blocked 2/3 | figures 44/44
```

The single skip is the deployment preflight, which needs a live gateway
and says so rather than counting itself as a pass.

| | |
|---|---|
| Adversarial prompts, hand-written | 1,000 — **976 held** by patterns, **988** by the full chain |
| Model-generated, after curation | 50 — **49 held** |
| Benign messages, false alarms | 136 — **0** |
| Personal data reaching cloud, under load | **0** |
| Composite classifier | F1 **1.000**; the gate blocks 2 of 3 candidates |
| Tests | **159 pass in CI**, 158 locally — hermetic, no service required |
| Scenario coverage | **23 of 23** traced |
| Control-plane throughput | **~3.1 per second**, flat from 1 to 16 concurrent |

Tests pass 159 in CI and 158 locally because the timing side-channel test
skips itself when Presidio is not running. CI starts Presidio, so CI
tests slightly *more* than a laptop does.

That last row is a finding, not a boast. See below.

---

## Findings

The interesting output of this project is the defects, including the ones
in its own instruments.

**The deployed gateway had no guards switched on.** A process left running
for eight hours predated the guard wiring and answered *"ignore all
previous instructions"* while every in-process test passed. The red team
ran in-process, so nothing ever asked the live service whether it had
guards loaded. Fixed by `loadtest/preflight.py`, which compares the
*running* service against its configuration and sends a canary through the
real HTTP path.

**The safety model blocked 66% of real customers.** Every message
containing a name or an address came back as hazard S7, "Privacy". The
model was right; it is just useless as a gate, because that describes most
support traffic. Two controls had overlapping scope and the cruder one was
vetoing the classifier that already owned the decision. S7 is now
advisory — still audited, no longer blocking. False alarms 66% → 0%,
attack detection unchanged, compliance-path median 23.5s → 10.2s.

**The control plane is saturated at concurrency 1.** Throughput is flat at
about 3.1 per second from 1 to 16 concurrent while latency grows linearly.
Little's law holds within 2–8% at every rung, so the plateau is real rather
than a harness artefact. The cause is one event loop on one core while
three sit idle — a configuration fix, now measurable against a baseline.

**My own metrics were missing the biggest consumer on the machine.** Host
CPU peaked at 100% while the per-process breakdown explained a quarter of
it, and Ollama reported 78MB for a loaded three-billion-parameter model.
The model runner is a separate executable that did not match the name
pattern: 3.7GB and nearly all the inference CPU were invisible. The
breakdown now publishes `mg_host_cpu_unattributed_percent`, so a gap of
this kind sets off its own alarm instead of inviting a confident wrong
answer.

**An aggregate threshold hid a 71% failure rate.** One path failed 5 of 7
requests while the run reported all thresholds passed, because those 5
were 1.25% of 400 total. The busiest scenario was voting on behalf of the
quietest. Thresholds are now per scenario as well as global.

**Five tests passed only because a service happened to be running.** They
had an undeclared dependency on the classifier; on a clean machine they
failed. The suite is now hermetic.

More in [PERFORMANCE-FINDINGS.md](PERFORMANCE-FINDINGS.md) and
[redteam/FINDINGS.md](redteam/FINDINGS.md).

---

## Honest limits

- Runs on a laptop: **architecture-correct, scale-simulated**. The shape of
  the findings holds; the absolute numbers are two-core numbers
- On-premises Kubernetes with a *real* cloud model provider — **not** a
  hybrid cloud deployment, and never described as one
- No multi-turn attacks; the output path is more lightly tested than the
  input path
- garak and PyRIT were not run
- The evaluation dataset was written by the same person who wrote the
  guards, which is a genuine weakness in the evidence
- **No longevity test yet.** Five minutes cannot distinguish a memory leak
  from a cache settling, and one component drifted 91MB in that window
- The churn model is a deliberate stand-in

---

## Layout

| | |
|---|---|
| `llm-gateway/` | the service: policy, routing, guardrails, providers, metrics |
| `redteam/` | adversarial corpora, the runner, and the findings |
| `evals/` | evaluation harness, promotion gate, model registry |
| `loadtest/` | k6 scenarios, the runner, and the deployment preflight |
| `dashboard/` | evidence page, collectors, and the figure verifier |
| `prove.py` | one command that runs every gate |
| `SCENARIOS.md` | the scenario catalogue the tests are traced against |
| `ARCHITECTURE.md` | how it is put together, and why |
| `BUSINESS_CASE.md` | the problem, with every figure labelled by confidence |

## Running it

```bash
pip install -r llm-gateway/requirements.txt
python -m spacy download en_core_web_sm
```

```bash
cd llm-gateway && uvicorn main:app --port 8000
```

[DEMO.md](DEMO.md) has the full sequence, including the failure
demonstrations — killing the classifier mid-request to show the gate fail
closed rather than fail open.
