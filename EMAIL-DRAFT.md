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
| Adversarial prompts (hand-written) | 1,000 — **976 held** by patterns, **988** by the full chain. **All short; see below** |
| Model-generated attacks, curated | 50 — **49 held** |
| False alarms on ordinary customer messages | 136 tested — **0** |
| Personal data reaching cloud under load | **0** |
| Personal-data detector | F1 **1.000** on the evaluation set; the gate **refuses 2 of 3** candidates. **One account-number format only; see below** |
| Tests | **174**, hermetic, green in CI on every push |
| Scenario coverage | **23 of 23**, traced by parsing code and spec |
| Published figures | **45 of 45** asserted against their source runs |
| Longevity, run 1 — 60 minutes under load | **489 requests**, 0 personal data to cloud, 0 attacks answered |

Three customer-facing defects were found by load testing that 133 unit tests
missed — including a safety model rejecting **66% of legitimate support
messages**. All three documented with the fix.

## Coverage, by discipline

This is the part I'd want scrutinised. Every test carries a discipline marker
— none are uncategorised — and every one of these runs on every push.

| Discipline | Scenarios | Tests | What is proven | In CI |
|---|---|---|---|---|
| Governance | 6 | 27 | Data residency, auditability, refusal behaviour, fail-closed on classifier loss | yes |
| Security | 5 | 63 | Authentication, caller override, prompt injection, obfuscation, timing side-channel | yes |
| Performance | 3 | 45 | Chain deadlines, load shedding, latency bounds, saturation | yes |
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
| Figure verification | 45 assertions | Every published number checked against its source run |
| Architecture drift | 4 diagrams + 1 invariant | Diagrams regenerated from config and the import graph; **no vendor name may appear outside the adapter layer** |

Three things need a live service or wall-clock time, so CI cannot run them
directly: the deployment preflight, the load test, and the longevity soak.
None is counted as a pass. The preflight skips with its reason; the soak is
handled by a gate that checks a run exists, is under 30 days old, and
**publishes its verdict on every build** — currently **FAIL**, printed in the
pipeline rather than buried. All three have been run locally against live
services and their raw results are in the repository.

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
path; and the evaluation dataset was written by the same person who wrote the
guards.

**The most useful thing I did after that was lengthen the test data, and it
broke my own headline figure.** Every prompt in the 1,000-attack corpus is
short — the hostile instruction is the whole message. Rewriting the corpus at
realistic support-message length, 40 to 1,500 characters instead of 40 to 62,
dropped the hold rate from **100% on short attacks to 40% on long ones**.
Three distinct bodies defeated both the pattern matcher and the safety model,
including one that simply asks the assistant to play a character in a film.
Fifteen attacks across three lengths is a small sample and I would not quote
the rate — but each failure reproduces on demand, so the gap is real. **The
corpus was measuring the guard against the corpus.** The same change found a
legitimate customer complaint being refused as a violent crime, and confirmed
the chain-deadline defect is worse at realistic length: 50% of the
personal-data path shed, against the 30.1% I reported from short inputs.

**The same question asked of the detector found the same shape of defect.**
Probing it with one identifier at a time rather than with my evaluation set,
it recognises **exactly one account-number format**. `8841-22719` is detected;
`4471-9082-3356` and a bare run of digits are not. It scores F1 1.000 because
every account number in both my datasets is written in that one format. No
customer data reached the cloud in any run — but one such request was served
on-premises because short simple requests route locally by default, not
because the privacy rule fired. The right outcome for the wrong reason. The
fix is to the dataset before the pattern: every identifier type has to appear
in several surface forms, and recall has to be reported per form.

I would rather show you this than a clean number I had not stress-tested.

**The longevity gap is now closed, and it found two defects.** A 60-minute
run on 2026-11-17 showed one component retaining **~4.0MB per request and
never releasing it**, and **30.1% of the personal-data path** shed at the
40-second chain deadline — while every compliance assertion held. I am
reporting this rather than waiting to fix it first: the measurement, and
what it changes about the production design, is the more useful thing to
show you. Full detail in `PERFORMANCE-REPORT.md` in the repository.

## Links

- **Repository:** https://github.com/HemantVarhekar/llm-governance-plane
- **Latest CI run:** https://github.com/HemantVarhekar/llm-governance-plane/actions/runs/43570418816

`python prove.py` runs every gate and writes a categorised evidence catalogue.
CI runs that same command on every push — what you see in the pipeline is what
I'd demo, not a parallel process that drifts from it.

`main` is protected: every change goes through a pull request and cannot merge
until that command passes. The rule applies to administrators, so it applies to
me too.

Happy to walk through any of it on Monday.

Best,
Hemant Varhekar
