# Project Context — AI Platform / MLOps / Governance Sprint

> Standing reference for the 30-day (then 60-day) learning sprint and the carrying POC.
> Keep this file at the repo root. Last updated: 2026-11-05.

---

## 1. Who this is for

- **Hemant** — Director of QA, ~15 years' experience, sales-driven org, based in India.
- **Strong in Python; new to Rust**, deliberately adding Rust (drawn to its recent improvements vs Python).
- Prefers **direct, committed answers** over hedging.
- Career pivot in progress: QA → AI Quality Engineering / AI Evals, aiming at **AI Platform Leader, Director of Quality + MLOps, and AI Governance leader**.
- **End goal: be fully ready for that role in 60 days.**

---

## 2. Sprint shape

- **30-day plan**, 3–4 hrs/day, then a **second 60-day phase** for offline models + fine-tuning.
- Each day: study one **design pattern**, then apply it across — architecture → small POC → scaling (multithreading, multitenancy, security, scalability, performance) → on-prem + cloud deployment → AI/MLOps use cases → multiagent + AI-governed setup → **AI-agnostic "plug-and-play any model."**
- **Core decision: ONE carrying POC that evolves daily**, not 30 disposable toy POCs.
- **Seed POC:** Kafka (inference event streaming) + RabbitMQ (agent task routing) + LangChain agent in the middle — simulate model inference events, detect anomalies, trigger actions.

### Pattern scope — blended
1. **GoF core (plug-and-play):** Adapter, Strategy, Factory.
2. **Distributed-system patterns:** CQRS, Event Sourcing, Saga, Outbox, Circuit Breaker, Sidecar, Bulkhead.
3. **AI/agent patterns:** RAG, router, eval-gate, guardrail, multiagent orchestration.

### 30-day arc
| Days | Focus |
|------|-------|
| 1–7  | Foundations & plug-and-play core |
| 8–15 | Distributed robustness (multithreading, multitenancy, backpressure) |
| 16–22 | MLOps + governance (registry, evals, drift, guardrails, model cards, policy-as-code) |
| 23–28 | Multiagent + deployment (on-prem vs cloud) |
| 29–30 | Harden, document, turn into portfolio/interview story |

---

## 3. Stack split — 70% Python / 30% Rust

- **Python owns:** LangChain agents, MLflow, eval harness, drift/anomaly detection, guardrails, local-model calls.
- **Rust owns exactly two components** (the multithreading + performance + memory-safety story):
  1. The **AI-agnostic model gateway**.
  2. The **hot-path Kafka inference-event consumer**.
- The two languages meet **only at the gateway boundary**: gateway exposes an OpenAI-compatible endpoint over HTTP/gRPC; Python calls it.
- **Because Rust is new:** build each component in Python first, then **rewrite the gateway in Rust later in week 1** — rewriting the same component is the fastest way to feel what Rust buys.

---

## 4. Hardware & models

- **Local setup:** ~20GB RAM, CPU-only.
- **Local daily driver:** `qwen3:4b` (fast iteration). Headroom to run `qwen3:8b` for more quality (~8–15 tok/s CPU-only).
- **Other viable local picks this tier:** Phi-4-mini (3.8B), Gemma 3 4B (speed); Qwen2.5-Coder 7B (Rust/Python coding help); gpt-oss:20b (MoE, ~14GB) as a slow stretch option.
- **Won't run here:** dense 14B+ and 27B/70B → spin up a cheap **spot cloud GPU** only for the specific large-model day.
- **Ollama** serves at `http://localhost:11434` (OpenAI-compatible at `/v1`). The gateway speaks this.
- **Claude = strong remote provider**, and later the **eval / synthetic-data engine**. It is **NOT fine-tuned**.
- **Fine-tuning (LoRA)** targets the small local open models — scheduled ~week 3.

---

## 5. Day 1 build — AI-agnostic gateway (Adapter + Strategy + Factory)

**Project:** `llm-gateway` · **Deps:** `fastapi uvicorn httpx pyyaml` · **Pre-req:** `ollama pull qwen3:4b`

### File layout & contracts
- **`providers/base.py`** — `ModelProvider(ABC)` with one async method `generate(messages: list[dict], **opts) -> str`. Every vendor is adapted to this single shape; downstream code never knows which vendor it is hitting. *(This is what makes the POC AI-agnostic.)*
- **`providers/ollama_provider.py`** — `OllamaProvider(model, base_url)`; async POST to `{base_url}/api/chat` with `{model, messages, stream: False}`; returns `json()["message"]["content"]`.
- **`providers/anthropic_provider.py`** — `AnthropicProvider(model, max_tokens=1024)`; reads `ANTHROPIC_API_KEY` from env; **splits `system` messages out** of the list into a top-level `system` field (Anthropic requirement), rest go in `messages`; POST `https://api.anthropic.com/v1/messages` with headers `x-api-key`, `anthropic-version: 2023-06-01`, `content-type: application/json`; returns `json()["content"][0]["text"]`.
- **`factory.py`** — `_BUILDERS` dict mapping `"ollama"` / `"anthropic"` → lambda constructing the adapter from a config dict; `build_provider(cfg)` dispatches on `cfg["type"]`, raises on unknown type. **Add a provider = add one builder + one config block, no handler changes.**
- **`routing.py`** — `RoutingStrategy(ABC)` with `choose(requested_model, available) -> str`; `NamedStrategy(default)` returns the requested provider if present in `available`, else the default. Routing policy is a swappable object, not `if/else` in the handler.
- **`config.yaml`:**
  - `default_provider: local`
  - `providers.local` → `{type: ollama, model: qwen3:4b, base_url: http://localhost:11434}`
  - `providers.claude` → `{type: anthropic, model: claude-sonnet-5-5}`
- **`main.py`** — FastAPI app; loads `config.yaml`; builds `PROVIDERS = {name: build_provider(c)}`; instantiates `NamedStrategy(default=...)`; endpoint `POST /v1/chat/completions` taking `ChatRequest{model: str|None, messages: list[dict]}`; picks provider via strategy, calls `generate`, returns `{provider, choices:[{message:{role:"assistant", content}}]}`.

### Pattern-role mapping (lock this in)
- **Adapter** = normalize each vendor's API to one `generate()` → vendor independence.
- **Strategy** = runtime choice of which provider handles a request (default / by-name / cheapest / per-tenant later) → swap policy without touching the handler.
- **Factory** = build the right adapter from config → add a model by editing YAML, not code.

### Run & proof
- `uvicorn main:app --reload`
- Curl with no `model` → routes to local `qwen3:4b`; curl with `{"model":"claude"}` → routes to Claude (needs `ANTHROPIC_API_KEY`).
- **The win to internalize:** the caller changes **one config-level field** and the entire AI backend swaps — zero downstream code touched.

### Design conventions established
- Everything async (`httpx.AsyncClient`, 120s timeout).
- Gateway presents an OpenAI-ish `/v1/chat/completions` surface.
- Config-driven provider registration; downstream is vendor-blind.

---

## 6. Rust taster (Day 1, 20 min)

- `cargo new rust-taster`; `src/main.rs` builds a `Vec` of provider names, takes `.first()` as `Option<&str>`, handles it with `match` (Some/None), iterates with `.enumerate()`, then still uses `providers.len()` after the loop.
- **Two lessons that matter for the gateway rewrite:**
  1. No `null` — you get an `Option` you *must* handle.
  2. `iter()` **borrows** rather than moves, so the vector survives the loop. Borrow-vs-move is the model you'll rebuild the gateway around.

---

## 7. Leader's log

- **`decisions.md`** in the repo. 3 sentences per day on what each day's pattern bought architecturally. *(Day 1: vendor independence, swap-without-redeploy, groundwork for per-tenant/per-cost routing.)*
- Doubles as **interview narrative** and **governance paper trail**.

---

## 8. Queued next

- **Day 2:** add the **Kafka inference-event stream** the (eventually Rust) consumer will read.
