# Business Case — MigrationGuard

> **Scope of this document:** the *why*. Market numbers, DIRECTV's position, the competitor, and the value math.
> It does **not** describe the system — that is [ARCHITECTURE.md](ARCHITECTURE.md). If a fact about components or
> contracts appears here, it is misplaced.
>
> Audience: the interviewer. This is the document to talk from.
> Last updated: 2026-11-09

## Confidence legend

Every figure below is labelled, because these numbers will be challenged.

- **[S] Solid** — multiple or authoritative sources agree
- **[M] Medium** — single source, or sources conflict on the exact value
- **[D] Derived** — my arithmetic from other figures, not a reported number
- **[H] Historical** — true when reported, may be stale

---

## The case in one line

> DIRECTV cannot out-content or out-spend Google. Their only winning move is operational excellence on the
> subscribers they already have: **don't lose the 8.2M, migrate them to streaming cleanly, monetise them better.**
> All three are non-functional, ML-operations problems — which is exactly what a Director of Quality & MLOps owns.

---

## 1. The market: a shrinking pie

As of end Q1 2026:

| Metric | Figure | |
|---|---|---|
| Total US pay-TV subscribers | 62.23M | **[S]** |
| — Traditional (cable / telco / satellite) | 40.90M | **[S]** |
| — Streaming (vMVPD) | 21.33M | **[S]** |
| Lost in a single quarter | **−2.03M** | **[S]** |

Traditional pay-TV is in structural decline. Whatever growth remains lives in streaming.

## 2. DIRECTV's position

| Metric | Figure | |
|---|---|---|
| TV subscribers (Mar 2026) | ~8.2M | **[M]** |
| Share of remaining pay-TV | ~13.2% | **[M]** |
| 2025 revenue | ~$17.6B | **[M]** |
| Borrowings | >$8B | **[M]** |
| Owner | TPG, $11B, Sept 2024 | **[S]** |
| Q1 2026 — traditional/satellite | **−222K** | **[M]** |
| Q1 2026 — streaming product | **+66K** | **[M]** |

**That last pair is the whole story.** Satellite is bleeding while their streaming product grows. DIRECTV's survival
depends on moving 8.2M paying satellite households onto streaming *without losing them in transit*.

## 3. The competitor: YouTube TV

| Metric | Figure | |
|---|---|---|
| Subscribers | ~9.4M–10M+ | **[M]** (sources conflict) |
| Share of US streaming live-TV | ~44–45% | **[M]** |
| Scale vs rivals | Larger than Hulu+Live, Sling, Fubo and DIRECTV combined | **[M]** |
| Fubo + Hulu+Live combined (June 2026) | 5.75M | **[M]** |

And the symbolic blow: **YouTube took NFL Sunday Ticket from DIRECTV** **[S]** — the retention hook DIRECTV built
its brand on for three decades.

### Why YouTube TV is winning

Not content. Not price. **YouTube TV is a software and data company; DIRECTV is a hardware and logistics company
trying to become one.**

| YouTube TV advantage | Operational consequence |
|---|---|
| Cloud-native delivery | No dish, no truck roll, no installer — sign-up to watching in minutes, far lower acquisition friction |
| Google's ML stack | Recommendations from YouTube's engine; dynamic ad insertion → higher ad yield per viewer |
| Adaptive bitrate at Google scale | Quality degrades gracefully instead of failing — fewer rage-quits |
| No legacy cost base | No satellite fleet, no field workforce, no $8B debt |

DIRECTV's structural drag: satellite fleet capex, an installer workforce, a legacy billing stack, an older and more
rural base, >$8B debt, and a long-standing addressable-advertising gap — their own filings conceded ad revenue per
subscriber trailed competitors because they could not target locally **[H, 2012 10-K]**.

## 4. What DIRECTV still holds

Assets Google cannot easily take:

- **8.2M high-ARPU paying households** — defending them costs far less than acquiring replacements
- **Commercial / business segment** — bars, hotels, restaurants; contractual, sticky, and poorly served by YouTube TV **[H]**
- **Aggregation** — the Gemini device bundles live TV *plus* Netflix and Hulu in one interface **[S]**
- **MyFree DIRECTV** — a FAST ad tier launched Nov 2024 that needs personalisation to monetise **[S]**
- **A receptive roadmap** — their data-science org is already investing in feature stores, MLOps and AIOps **[M]**

## 5. The three imperatives

| # | Imperative | Why it is an ML-operations problem |
|---|---|---|
| 1 | **Don't lose the 8.2M** | Churn must be predicted early enough to intervene |
| 2 | **Migrate them cleanly** | Streaming QoE *is* churn — buffering during migration sends people to YouTube TV |
| 3 | **Monetise them better** | Closing the addressable-ad gap needs personalisation, while staying inside PII rules |

Note that none of these is a feature problem. All three are reliability, quality, cost and compliance problems.

## 6. The money math

| Step | Value | |
|---|---|---|
| Revenue ÷ subscribers | $17.6B ÷ 8.2M | **[D]** |
| Implied ARPU | ~$150–180 / month | **[D]** |
| Annual value per subscriber | ~$1,800–2,100 | **[D]** |
| 1% of 8.2M | 82,000 households | **[D]** |
| **Annual revenue retained by a 1% churn reduction** | **~$150–170M** | **[D]** |

Present ARPU as *derived*, never as a quoted figure — it is revenue divided by subscribers and therefore includes
commercial and advertising revenue, so it overstates residential ARPU.

**The point of the number:** at this scale, quality engineering is a revenue function. A Director of Quality & MLOps
at DIRECTV is not a cost centre.

## 7. Where the POC fits

The anchor is the **satellite→streaming migration**. Every migrated household is a churn-risk moment, and streaming
quality decides whether the migration sticks.

**MigrationGuard** is a control plane in front of every ML and LLM call in that journey, so each one is observable,
guard-railed, cost-routed, compliance-bound and fallback-protected. It demonstrates all three drivers at once —
governance & quality, cost & resilience, compliance & data residency.

The system design, including the signature fail-closed PII gate, is in [ARCHITECTURE.md](ARCHITECTURE.md).

One DIRECTV-specific idea worth calling out in the room: the **migration-regression score** — compare each
household's pre-migration satellite reliability against their post-migration streaming QoE. *If streaming is worse
than the dish they gave up, they churn.* That is not generic anomaly detection; it is their actual problem.

## 8. Objections and answers

| Objection | Answer |
|---|---|
| "Why build a gateway? LiteLLM, Portkey, Kong AI Gateway, Bedrock and Vertex all do this." | The gateway is commodity; the **policy and evidence layer** is the product. I would buy the gateway and build the governance. |
| "Our data-science org already has a feature store, MLOps and AIOps." | I am not rebuilding the platform. I am adding the quality and governance control plane on top of it, model-agnostic by design. |
| "Your churn model is a toy." | Deliberately — it is a stand-in. Swap in yours; the control plane does not care. That is the point. |
| "Why both Kafka and RabbitMQ?" | Kafka is the replayable event log (stream, audit, re-derive). RabbitMQ is the task queue (per-message ack, retry, DLQ) for actions that must happen once. |
| "This runs on a laptop. That isn't scale." | Architecture-correct, scale-simulated. Here are the load-test numbers and the scaling path. |
| "Where's the proof it works?" | The eval harness: precision, recall and a confusion matrix per model, plus an eval gate that blocks promotion below threshold. |
| "Is this fair?" | Retention targeting can systematically disadvantage a region or cohort, so a fairness delta belongs in the eval gate. |
| **"How much does routing on-prem actually save you?"** | **Concede it.** Measured per-call cost extrapolates to ~$4K/yr on short replies and ~$110K/yr with realistic RAG context — against $17.6B revenue. **Cost is the weakest of the three drivers at this scale.** The cost argument that holds is *optionality*, not arbitrage: when a provider raises prices, degrades, or a cheaper model ships, it is a config line rather than a release. |

**Honesty rule:** the POC runs on-prem Kubernetes with a *real* cloud model provider. Never describe it as a hybrid
cloud deployment — that is the one claim that will get caught.

**Do not conflate two different numbers.** The **$150–170M** in section 6 is *churn retention* — revenue kept by
not losing subscribers. That is the large, defensible number. **Inference cost saving is a separate and much
smaller number** (tens to low hundreds of thousands). Presenting the small one as though it were the business case
invites exactly the arithmetic that undoes you. Lead with churn; treat inference cost as a secondary benefit and
say plainly that it is.

## Sources

- [US pay-TV Q1 2026 losses](https://www.lightreading.com/video-streaming/us-pay-tv-sheds-2m-subscribers-in-q1)
- [DIRECTV business and portfolio analysis](https://365247newsletter.substack.com/p/inside-directvs-sports-strategy-business)
- [YouTube TV scale vs rivals](https://cordcuttersnews.com/youtube-tv-is-now-bigger-than-fubo-sling-tv-hulu-live-tv-directv-combined/)
- [YouTube TV vs Comcast/Spectrum, Nielsen](https://cordcuttersnews.com/the-end-of-cable-tv-youtube-tv-is-now-larger-than-comcast-spectrum-combined-according-to-nielsen/)
- [YouTube TV market-share forecast](https://www.forbes.com/sites/bradadgate/2024/04/07/youtube-tv-is-forecast-to-be-the-largest-pay-tv-distributor-in-2026/)
- [Fubo / Hulu+Live subscribers](https://en.wikipedia.org/wiki/FuboTV)
- [MyFree DIRECTV launch](https://www.sportsvideo.org/2024/10/21/directv-to-unveil-myfree-directv-this-november/)
- [DIRECTV Gemini device](https://www.directv.com/glossary)
- [DIRECTV ad-targeting gap, 2012 10-K](https://www.sec.gov/Archives/edgar/data/0001465112/000104746913001381/a2212949z10-k.htm)
- [DIRECTV data science / MLOps focus](https://www.databricks.com/dataaisummit/speaker/malav-shah)

## Known gaps

- No official DIRECTV earnings release was located; subscriber and revenue figures come from third-party analysis
  **[M]**. If asked, say so — do not defend a number harder than its source supports.
- The commercial/business segment and the ad-targeting gap are supported only by dated filings **[H]**.
- No reliable primary source was found for a DIRECTV genAI customer-service programme. Do not assert one.
