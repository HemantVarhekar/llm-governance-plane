# Email draft — to the DIRECTV hiring panel

**Subject:** MigrationGuard — a working control plane for AI decisions on customer data

---

Hi [Name], [Name],

A short note on something I built, and on what it actually proves. Everything
below is runnable and every number has a command behind it; links at the end.
If the work and the approach look worth discussing, I would welcome a
conversation whenever it suits you.

## The problem I set out to fix

DIRECTV's winning move is operational: hold the subscribers you have and move
them to streaming without losing them in transit. Churn scoring, playback
anomaly detection and support drafting all now run through an ML or LLM call,
and every one of those calls touches customer data.

The risk isn't that a model gets something wrong. It's that **you cannot prove
what it did** — whose data went where, why a provider was allowed to see it,
and whether the guardrails were even switched on at the time. That last one
isn't hypothetical: during this build a gateway ran for **eight hours with its
guardrails silently unloaded**, and every test passed, because every test ran
in-process and not one of them ever asked the live service what it had
actually loaded. Closing that gap is the whole point of this system.

## What it is

| | |
|---|---|
| **In one line** | A control plane in front of every model call |
| **The governance move** | Personal data is a **hard constraint** — it filters the provider list *before* cost or latency are considered |
| **Fail-closed** | If the detector is unavailable, cloud egress is refused rather than assumed safe. Safety without an outage |
| **Build vs buy** | The gateway is a commodity; I'd buy one. The part worth building is the **evidence layer** — the adversarial corpus, the promotion gates, the traceability, the audit trail that answers *why* a model made a given decision about a customer |

## What it proves

| Measure | Result |
|---|---|
| Personal data reaching cloud, under load and under attack | **0** |
| Hand-written adversarial prompts held | **976 of 1,000** by the pattern layer; **988** with the safety model added |
| Model-generated attacks held | **49 of 50** — from 100 generated, 50 kept after curation |
| False alarms by the pattern layer on ordinary messages | **0** — across 20 benign messages locked as tests and the 12 real support messages in the load test's compliance path |
| Same messages, judged by the **safety model** instead | **8 of 12 refused** before it was narrowed — a 66% false-alarm rate on real customer language |
| Personal-data detector | F1 **1.000** on the evaluation set; the gate **refuses 2 of every 3** candidates |
| Tests | **174**, hermetic, green in CI on every push |
| Scenario coverage | **23 of 23**, traced by parsing code and spec |
| Published figures | **45 of 45**, asserted against the run that produced them |
| Longevity, run 1 — 60 minutes under load | **489 requests**: 0 personal data to cloud, 0 attacks answered, latency drift −9.5% |
| Memory under sustained load | **FAIL** — the local inference server grows **1.66 GB/hour** and never releases it |

**Where the 988 comes from.** 976 is measured across the full thousand. The
safety model is slow, so rather than re-run it on all 1,000 I ran it against
the **24 the patterns miss** — the only place it can help. It caught 12,
exactly half, which gives 988. Anyone who runs the corpus sees 976, so I'd
rather say where the other 12 came from than let you find the gap.

## Coverage, by discipline

Every test carries a discipline marker — none are uncategorised — and every
one of these runs on every push.

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
| | | **175** | sums to one more than 174: the oversized-payload test carries **both** the performance and the security marker, because it is genuinely both | |

## Gates that also run on every push

| Gate | Scale | What it enforces |
|---|---|---|
| Red team, hand-written | 1,000 attacks | Prompt injection, credential extraction, authority pretexting, obfuscation |
| Red team, model-generated | 50 curated from 100 | Attacks the model invented, curated and frozen for reproducibility |
| Detector promotion gate | 3 candidates, 185 samples | No detector below threshold can be promoted |
| Figure verification | 45 assertions | Every published number checked against its source run |
| Architecture drift | 4 diagrams + 1 invariant | Diagrams regenerated from config and the import graph; **no vendor name may appear outside the adapter layer** |

| Cannot run in CI | Why | How it is handled |
|---|---|---|
| Deployment preflight | needs a live service | Skips with its reason printed — never counted as a pass |
| Load test | needs a live service | Run locally, raw results committed |
| Longevity soak | needs wall-clock time | A gate checks a run exists and is under 30 days old, and **publishes its verdict on every build** — currently **FAIL**, printed in the pipeline rather than buried |

## What I found by attacking my own evidence

This is the section I'd most want to talk through, because it is the actual
job. Four times in one week a quality signal I trusted turned out to be
measuring the wrong thing — and each one was found by questioning the **test
data**, not the code.

| # | What I believed | What was actually true | How it surfaced | Why it matters |
|---|---|---|---|---|
| 1 | 97.6% of attacks held | **100% on short attacks, 40% on long ones.** Three bodies defeated both the pattern matcher and the safety model — one simply asks the assistant to play a character in a film | Rewrote the corpus at realistic support-message length: 40–1,500 characters instead of 40–62 | Every prompt in the corpus was *bare* — the hostile instruction was the whole message. **The corpus was measuring the guard against the corpus.** The same change showed the chain-deadline defect is worse at length: **50%** of the personal-data path shed, against the 30.1% I'd reported |
| 2 | Detector F1 1.000, entity recall 1.000 | **It recognises exactly one account-number format.** `8841-22719` is detected; `4471-9082-3356` and a bare run of digits are not | Probed it one identifier at a time instead of with my evaluation set | It scores 1.000 because every account number in *both* my datasets is written in that one format. No data leaked — but one such request stayed on-premises because short requests route locally **by default**, not because the privacy rule fired. **The right outcome for the wrong reason**, which in a compliance system is still a defect. Fix the dataset before the pattern |
| 3 | The safety model judges customer messages | **It was judging the operator's own system prompt.** The gateway concatenates every message regardless of role, so `llama-guard3:1b` refused an ordinary buffering complaint as a **violent crime** — HTTP 400 to a paying subscriber | Isolated to two reproductions: system + user refuses, user alone is served | The privacy pipeline was correct in every blocked request; an overlapping control vetoed work already handled properly. The real fix is narrow — restrict the safety model to **user-role** content while keeping the pattern matcher across *all* roles — because doing it naively lets an attacker hide an instruction in a system message |
| 4 | Every published figure is gate-checked | **Two had no source at all** — a false-alarm count and a metric count, both in my README, both outside the 45 the figure gate covers | Re-derived every number in this email from its command before sending it | A gate that checks 45 figures and leaves 2 unguarded is exactly the failure mode in the other three. Both are corrected; the blind spot is now a known gap rather than an unknown one |

Earlier, a load test found the safety model refusing **8 of 12** legitimate
support messages, which 133 unit tests had not caught.

## Cost — three numbers, not to be confused

| | Figure | What it is |
|---|---|---|
| **The prize** | **$148–172M / yr** | Revenue retained by a 1% churn reduction on ~8.2M subscribers (82,000 households). This system does **not** deliver that — it makes the programme that does **safe to run on customer data** |
| Inference saving | **$5K–31K / yr** | 12.3M yearly contacts at the measured 28% on-premises split: $5,418 at short-reply length, $31,498 with realistic retrieval context |
| Production run cost | ~$180K / yr | 2 GPU nodes, observability, 0.5 FTE. Illustrative, not a quote |

I had the middle row at ~$74K until the demo printed the sensitivity table
live. It doesn't survive contact with real token counts, so I changed it.
**Cost is the weakest of the three drivers at this scale and I'd rather say so
than inflate it.** The case is risk and optionality: when a provider raises
prices or degrades, switching is a config line rather than a release.

*Subscriber and revenue figures are third-party analysis; each is
confidence-labelled in the repository.*

## Stack — architecture-correct, scale-simulated

| | POC today | Production |
|---|---|---|
| Drafting | `llama3.2:3b`, Ollama, on-prem | GPU inference nodes |
| Safety | `llama-guard3:1b`, on-prem | same, replicated |
| Cloud | `claude-sonnet-5-5` ($0.003/1k) | unchanged — swappable by config |
| PII detection | Presidio + spaCy, single process | replicated — the measured bottleneck at **2.9 req/s** |
| Service | FastAPI / Python 3.13, one node | managed Kubernetes, GitOps, OPA |
| Observability | 49 Prometheus metrics, 195 series with histogram buckets, plus the audit trail | + Grafana, per-segment cost attribution |
| Security testing | 1,000-attack corpus in CI | + garak, PyRIT, mTLS, workload identity |
| Load testing | k6 2.3.0, 3 scenarios, thresholds as code | k6 in CI against this baseline |
| Hardware | one laptop, 2 cores | multi-region, stated RTO/RPO |

## What is built, and what is not

| Component | State |
|---|---|
| Gateway, policy evaluator, three enforcement points | **built and running** |
| Personal-data detector + fail-closed gate | **built and running** |
| Guardrails, injection scanning | **built and running** |
| Evaluation harness, promotion gate, model registry | **built and running** |
| Cost ledger, audit trail | **built and running** |
| Load test, 60-minute soak, two adversarial corpora | **built and run** |
| Kafka event-streaming data plane, telemetry producer, anomaly consumer | **designed, not built** |
| The one constrained agent | **designed, not built** |
| Dockerfiles, Compose file, Kubernetes manifests, admission policy | **not built** — no containers, no cluster |
| Migration-regression score — a household's streaming experience against its own satellite baseline, the one genuinely DIRECTV-specific idea here | **a sentence, not code** |

`ARCHITECTURE.md` says this in the sections that describe those components, so
the document can't be read as a claim that they exist.

## Time to production

| | |
|---|---|
| **Estimate** | **8 weeks — 2 engineers and 1 QA lead** |
| Why it's credible | Every item comes from something measured. The classifier replica count comes from a measured **2.9 request/second** ceiling, not a guess |

## Known gaps

| Gap | Where it stands |
|---|---|
| Multi-turn attacks | Not tested. Single-turn only |
| Output path | More lightly tested than the input path |
| Soak corpus | The 60-minute run used the **short** corpus, so every performance figure here is against short inputs — and I've shown what length does to them |
| Dataset authorship | The evaluation dataset was written by the same person who wrote the guards. This is the root cause of two of the four findings above |

## Links

| | |
|---|---|
| Repository | https://github.com/HemantVarhekar/llm-governance-plane |
| Latest CI run | https://github.com/HemantVarhekar/llm-governance-plane/actions/runs/44301601123 |
| One command for all of it | `python prove.py` — runs every gate and writes a categorised evidence catalogue |

CI runs that same command on every push, so what you see in the pipeline is
what I'd demo, not a parallel process that drifts from it. `main` is
protected: every change goes through a pull request and cannot merge until
that command passes. The rule applies to administrators, so it applies to me
too.

Happy to walk through any of it.

Best,
Hemant Varhekar
