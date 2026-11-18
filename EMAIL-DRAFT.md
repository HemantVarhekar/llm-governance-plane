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
and whether the guardrails were even switched on at the time.

That last one isn't hypothetical. During this build a gateway ran for eight
hours with its guardrails silently unloaded. Every test passed, because every
test ran in-process and not one of them ever asked the live service what it
had actually loaded. Closing that gap is the whole point of this system.

## What it is

A control plane in front of every model call. Personal data is a **hard
constraint**: it filters the provider list before cost or latency are
considered, and if the detector is unavailable, cloud egress is refused rather
than assumed safe. Safety without an outage.

The gateway itself is a commodity; I'd buy one. The part worth building is the
**evidence layer** — the adversarial corpus, the promotion gates, the
traceability, the audit trail that answers *why* a model made a given decision
about a customer.

## What it proves

| Measure | Result |
|---|---|
| Personal data reaching cloud, under load and under attack | **0** |
| Hand-written adversarial prompts held | **976 of 1,000** by the pattern layer; **988** with the safety model added — see the note below on how that second number is derived |
| Model-generated attacks held | **49 of 50**, from 100 generated and 50 kept after curation |
| False alarms by the pattern layer on ordinary messages | **0** — across 20 benign messages locked as tests and re-checked on every push, and the 12 real support messages in the load test's compliance path |
| Personal-data detector | F1 **1.000** on the evaluation set; the gate **refuses 2 of every 3** candidates. **One account-number format only — see below** |
| Tests | **174**, hermetic, green in CI on every push |
| Scenario coverage | **23 of 23**, traced by parsing code and spec |
| Published figures | **45 of 45**, asserted against the run that produced them |
| Longevity, run 1 — 60 minutes under load | **489 requests**: 0 personal data to cloud, 0 attacks answered, latency drift −9.5% |
| Memory under sustained load | **FAIL** — the local inference server grows **1.66 GB/hour** and never releases it |

**On the 988.** 976 is measured: the pattern layer run against the full
thousand. The safety model is slow and expensive, so rather than re-run it
across all 1,000 I ran it against the 24 the patterns miss — the residual that
is the only place it can help. It caught 12, exactly half, which gives 988 of
1,000 for the full chain. I am spelling that out because an interviewer who
runs the corpus will see 976 and should know where the other 12 came from. An
earlier 60-attack sample showed the safety model catching zero, which was
sampling, not truth.

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

That column sums to 175 against a total of 174. One test — oversized payloads
rejected with HTTP 413 — carries both the performance and the security marker,
because it is genuinely both: a denial-of-service control and a resource
bound. I'd rather state that than have you add the column and wonder.

Beyond the unit suite, these gates also run on every push:

| Gate | Scale | What it enforces |
|---|---|---|
| Red team, hand-written | 1,000 attacks | Prompt injection, credential extraction, authority pretexting, obfuscation |
| Red team, model-generated | 50 curated from 100 | Attacks the model invented, curated and frozen for reproducibility |
| Detector promotion gate | 3 candidates, 185 samples | No detector below threshold can be promoted |
| Figure verification | 45 assertions | Every published number checked against its source run |
| Architecture drift | 4 diagrams + 1 invariant | Diagrams regenerated from config and the import graph; **no vendor name may appear outside the adapter layer** |

Three things need a live service or wall-clock time, so CI cannot run them
directly: the deployment preflight, the load test and the longevity soak. None
is counted as a pass. The preflight skips with its reason; the soak is handled
by a gate that checks a run exists, is under 30 days old, and **publishes its
verdict on every build** — currently **FAIL**, printed in the pipeline rather
than buried. All three have been run locally against live services and their
raw results are in the repository.

## What I found by attacking my own evidence

This is the section I would most want to talk through, because it is the
actual job. Four times in one week a quality signal I trusted turned out to be
measuring the wrong thing, and each one was found by questioning the test
data rather than the code.

**1. The corpus was measuring the guard against the corpus.** Every prompt in
the 1,000-attack corpus is bare — the hostile instruction is the whole
message. Rewritten at realistic support-message length (40 to 1,500
characters instead of 40 to 62), the hold rate fell from **100% on short
attacks to 40% on long ones**. Three distinct bodies defeated both the pattern
matcher and the safety model, including one that simply asks the assistant to
play a character in a film. Fifteen attacks across three lengths is a small
sample and I would not quote the *rate* — but each failure reproduces on a
named body, so the gap is real. The same change showed the chain-deadline
defect is worse at realistic length: **50%** of the personal-data path shed,
against the 30.1% I had reported from short inputs.

**2. The detector knows exactly one account-number format.** Probed one
identifier at a time rather than with my evaluation set, `8841-22719` is
detected; `4471-9082-3356` and a bare run of digits are not. It scores F1
1.000 because every account number in both of my datasets happens to be
written in that one format. No customer data reached the cloud in any run —
but one such request was served on-premises because short simple requests
route locally by default, **not** because the privacy rule fired. The right
outcome for the wrong reason, which in a compliance system is a defect even
when the outcome is correct. The fix goes to the dataset before the pattern:
every identifier type has to appear in several surface forms, and recall has
to be reported per form.

**3. The safety model was judging the operator's own prompt as customer
text.** The gateway concatenates every message in a request regardless of
role and hands the result to both input guards. So the system prompt travels
with the customer's complaint, and `llama-guard3:1b` refused an ordinary
buffering complaint as a **violent crime** — HTTP 400 to a paying subscriber.
Isolated to two reproductions: system plus user refuses, user alone is served.
The privacy pipeline was correct in every blocked request; the overlapping
control vetoed work that had already been handled properly. The real fix is
narrower than it looks — restrict the safety model to user-role content while
keeping the pattern matcher across *all* roles — because doing it naively
would let an attacker hide a hostile instruction in a system message.

**4. Two of my own published figures had no source.** Re-deriving every number
in this email from its command, two did not survive: a false-alarm count and a
metric count, both in my README, both outside the 45 the figure gate covers. I
corrected them and the gate's blind spot is now a known gap rather than an
unknown one. A gate that checks 45 figures and leaves 2 unguarded is exactly
the failure mode I am describing in the other three.

Earlier, a load test found a safety model refusing **8 of 12** legitimate
support messages — a 66% false-alarm rate on real customer language — which
133 unit tests had not caught.

## Cost — three numbers, not to be confused

| | Figure | What it is |
|---|---|---|
| **The prize** | **$148–172M / yr** | Revenue retained by a 1% churn reduction on ~8.2M subscribers (82,000 households). This system does **not** deliver that — it makes the programme that does safe to run on customer data |
| Inference saving | **$5K–31K / yr** | 12.3M yearly contacts at the measured 28% on-premises split: $5,418 at short-reply length, $31,498 with realistic retrieval context. Real, and small |
| Production run cost | ~$180K / yr | 2 GPU nodes, observability, 0.5 FTE. Illustrative, not a quote |

I had this at ~$74K until I made the demo print the sensitivity table live.
It doesn't survive contact with the actual token counts, so I changed it.
**Cost is the weakest of the three drivers at this scale and I would rather
say so than inflate it.** The case is risk and optionality: when a provider
raises prices or degrades, switching is a config line rather than a release.

*Subscriber and revenue figures are third-party analysis; each is
confidence-labelled in the repository.*

## Stack

| | POC today | Production |
|---|---|---|
| Drafting | `llama3.2:3b`, Ollama, on-prem | GPU inference nodes |
| Safety | `llama-guard3:1b`, on-prem | same, replicated |
| Cloud | `claude-sonnet-5-5` ($0.003/1k) | unchanged — swappable by config |
| PII detection | Presidio + spaCy, single process | replicated — the measured bottleneck at 2.9 req/s |
| Service | FastAPI / Python 3.13, one node | managed Kubernetes, GitOps, OPA |
| Observability | 49 Prometheus metrics, 195 series with histogram buckets, plus the audit trail | + Grafana, per-segment cost attribution |
| Security testing | 1,000-attack corpus in CI | + garak, PyRIT, mTLS, workload identity |
| Load testing | k6 2.3.0, 3 scenarios, thresholds as code | k6 in CI against this baseline |
| Hardware | one laptop, 2 cores | multi-region, stated RTO/RPO |

Architecture-correct, scale-simulated.

## What is built, and what is not

The control plane is real and running: the gateway, the policy evaluator and
its three enforcement points, the personal-data detector with its fail-closed
gate, the guardrails, the evaluation harness with its promotion gate and
registry, the cost ledger, the audit trail, the load test, the soak and the
adversarial corpora.

The event-streaming data plane is designed and **not built** — no Kafka, no
playback-telemetry producer, no anomaly consumer. The constrained agent is
designed and not built. There are no containers and no cluster: no Dockerfile,
no Compose file, no Kubernetes manifest, no admission policy. The
migration-regression score — comparing a household's post-migration streaming
experience against its own satellite baseline, which is the one genuinely
DIRECTV-specific idea in the design — exists as a sentence, not as code.

`ARCHITECTURE.md` says all of this in the sections that describe those
components, so the document cannot be read as a claim that they exist.

## Time to production

**8 weeks, 2 engineers and 1 QA lead.** Every item in that plan comes from
something measured — the classifier replica count comes from a measured 2.9
request/second ceiling, not a guess.

## Known gaps

No multi-turn attacks. The output path is more lightly tested than the input
path. The 60-minute soak ran against the short corpus, so every performance
figure quoted here is against short inputs and I have already shown what
length does to them. And the evaluation dataset was written by the same person
who wrote the guards, which is the root cause of two of the four findings
above.

## Links

- **Repository:** https://github.com/HemantVarhekar/llm-governance-plane
- **Latest CI run:** https://github.com/HemantVarhekar/llm-governance-plane/actions/runs/44299321841

`python prove.py` runs every gate and writes a categorised evidence catalogue.
CI runs that same command on every push — what you see in the pipeline is what
I'd demo, not a parallel process that drifts from it.

`main` is protected: every change goes through a pull request and cannot merge
until that command passes. The rule applies to administrators, so it applies
to me too.

Happy to walk through any of it.

Best,
Hemant Varhekar
