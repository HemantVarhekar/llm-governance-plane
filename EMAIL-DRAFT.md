# Email draft — to VP / CTO

**Subject:** MigrationGuard — a working control plane for AI decisions on customer data

---

Hi [Name], [Name],

Ahead of Monday, a short note on what I built and what it proves. Everything
below is runnable; links at the end.

## The problem I set out to fix

DIRECTV's winning move is operational: hold the subscribers you have and move
them to streaming without losing them in transit. Churn scoring, playback
anomaly detection and support drafting all now run through ML or LLM calls —
and every one touches customer data.

The risk isn't that a model is wrong. It's that **you cannot prove what it
did**: whose data went where, why a provider was allowed to see it, and
whether the guardrails were actually switched on at the time.

That last one isn't hypothetical. During this build, a gateway ran for eight
hours with its guardrails silently unloaded. Every test passed — because every
test ran in-process and none ever asked the live service what it had loaded.
That gap is what this closes.

## What it is

A control plane in front of every model call. Personal data is a **hard
constraint** that filters the provider list before cost or latency are
considered — and if the detector is unavailable, cloud egress is refused
rather than assumed safe. Safety without an outage.

The gateway itself is commodity; I'd buy one. The part worth building is the
**evidence layer**: the adversarial corpus, the promotion gates, the
traceability, the audit trail that answers *why* a model made a decision about
a customer.

## What the POC proves

| | Measured |
|---|---|
| Adversarial prompts (hand-written) | 1,000 — **976 held** by patterns, **988** by the full chain |
| Model-generated attacks, curated | 50 — **49 held** |
| False alarms on ordinary customer messages | 136 tested — **0** |
| Personal data reaching cloud under load | **0** |
| Personal-data detector | F1 **1.000**; the gate **refuses 2 of 3** candidates |
| Tests | **159**, hermetic, green in CI on every push |
| Scenario coverage | **23 of 23**, traced by parsing code and spec |
| Published figures | **44 of 44** asserted against their source runs |

Three customer-facing defects were found by load testing that 133 unit tests
missed — including a safety model rejecting **66% of legitimate support
messages**. All three documented with the fix.

## Coverage, by discipline

This is the part I'd want scrutinised. Every test carries a discipline marker
— none are uncategorised — and every one of these runs on every push.

| Discipline | Scenarios | Tests | What is proven | In CI |
|---|---|---|---|---|
| Governance | 6 | 25 | Data residency, auditability, refusal behaviour, fail-closed on classifier loss | yes |
| Security | 5 | 63 | Authentication, caller override, prompt injection, obfuscation, timing side-channel | yes |
| Performance | 3 | 32 | Chain deadlines, load shedding, latency bounds, saturation | yes |
| Quality | 1 | 15 | Output drift, reply-format guardrails, detector promotion gate | yes |
| Cost | 2 | 7 | Routing economics and the cost ledger | yes |
| Resilience | 2 | 7 | Fallback chains, error taxonomy, graceful degradation | yes |
| Architecture | 2 | 3 | Adapter / Factory / Strategy guarantees, enforced vendor boundary | yes |
| Reliability | 1 | 5 | Startup configuration validation | yes |
| Operability | 1 | 3 | Live configuration reload without restart | yes |

Beyond the unit suite, these gates also run on every push:

| Gate | Scale | What it enforces |
|---|---|---|
| Red team, hand-written | 1,000 attacks | Prompt injection, credential extraction, authority pretexting, obfuscation |
| Red team, model-generated | 50 curated | Attacks the model invented, curated and frozen for reproducibility |
| Detector promotion gate | 3 candidates, 185 samples | No detector below threshold can be promoted |
| Figure verification | 44 assertions | Every published number checked against its source run |
| Architecture drift | 4 diagrams + 1 invariant | Diagrams regenerated from config and the import graph; **no vendor name may appear outside the adapter layer** |

Three gates are honestly reported as **not** covered by CI, because they need a
live service or wall-clock time: the deployment preflight, the load test, and
the longevity soak. CI marks them skipped with the reason rather than counting
them as passes.

## Cost — two numbers, not to be confused

| | Figure | What it is |
|---|---|---|
| **The prize** | **$148–172M / yr** | Revenue retained by a 1% churn reduction on ~8.2M subscribers (82,000 households). This system does **not** deliver that — it makes the programme that does safe to run on customer data |
| Inference saving | ~$74K / yr | Two-thirds of 12.3M yearly contacts on-prem. Real, and small. I wouldn't fund this on cost arbitrage |
| Production run cost | ~$180K / yr | 2 GPU nodes, observability, 0.5 FTE. Illustrative, not a quote |

The case is risk and optionality. When a provider raises prices or degrades,
switching is a config line rather than a release.

*Subscriber and revenue figures are third-party analysis; each is
confidence-labelled in the repo.*

## Stack

| | POC today | Production |
|---|---|---|
| Drafting | `llama3.2:3b`, Ollama, on-prem | GPU inference nodes |
| Safety | `llama-guard3:1b`, on-prem | same, replicated |
| Cloud | `claude-sonnet-5-5` ($0.003/1k) | unchanged — swappable by config |
| PII detection | Presidio + spaCy, single process | replicated — this is the measured bottleneck at 2.9 req/s |
| Service | FastAPI / Python 3.13, one node | managed Kubernetes, GitOps, OPA |
| Observability | 107 Prometheus metrics, audit trail | + Grafana, per-segment cost attribution |
| Security testing | 1,000-attack corpus in CI | + garak, PyRIT, mTLS, workload identity |
| Load testing | k6 2.3.0, 3 scenarios, thresholds as code | k6 in CI against this baseline |
| Hardware | one laptop, 2 cores | multi-region, stated RTO/RPO |

Architecture-correct, scale-simulated.

## Time to production

**8 weeks, 2 engineers and 1 QA lead.** Every item in that plan comes from
something measured — the classifier replicas come from a measured 2.9
request/second ceiling, not a guess.

## Known gaps

No multi-turn attacks; the output path is more lightly tested than the input
path; no longevity test yet — five minutes cannot distinguish a memory leak
from a cache settling; and the evaluation dataset was written by the same
person who wrote the guards.

## Links

- **Repository:** https://github.com/HemantVarhekar/llm-governance-plane
- **Latest CI run:** https://github.com/HemantVarhekar/llm-governance-plane/actions/runs/43194710037

`python prove.py` runs every gate and writes a categorised evidence catalogue.
CI runs that same command on every push — what you see in the pipeline is what
I'd demo, not a parallel process that drifts from it.

Happy to walk through any of it on Monday.

Best,
Hemant Varhekar
