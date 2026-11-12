"""MigrationGuard gateway — the six-stage pipeline, wired.

Notice what this module does NOT import: no vendor, no model name. It calls
build_all() and receives a registry. The vendor-blindness claim in
BUSINESS_CASE.md is structural, not aspirational.

Pipeline (ARCHITECTURE.md section 3):
    1 CLASSIFY   2 POLICY   3 SELECT   4 GUARD-IN   5 CALL   6 GUARD-OUT
    then emit one event to the audit trail, always.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from factory import (
    ConfigError,
    build_all,
    build_classifier,
    build_guardrails,
    load_config,
)
from guardrails import FormatGuard, GuardChain, NoOpGuard, IN, OUT
from observability import EventSink, Metrics
from policy import Policy, PolicyError, RegexClassifier
import sysmetrics
from routing import (
    CheapestFirstStrategy,
    LocalFirstStrategy,
    PolicyStrategy,
    execute_with_fallback,
)

# --------------------------------------------------------------------------
# Operational limits
# --------------------------------------------------------------------------

# Max concurrent in-flight requests before we shed load with 429.
# On 2 physical cores with 6-10s inference, unbounded concurrency means
# memory growth and uniform timeouts - a load test would learn nothing except
# that it overwhelmed us. Shedding gives a meaningful saturation point.
MAX_INFLIGHT = int(os.environ.get("MG_MAX_INFLIGHT", "8"))

# Largest payload we will even look at.
#
# FOUND BY THE RED TEAM, and it is a denial-of-service vector rather than a
# tidiness rule. The safety model costs ~2.2ms per character, measured:
#
#      200 chars -> 2.5s      4,000 -> 12.4s
#    1,000 chars -> 5.4s      8,000 -> 25.9s
#    2,000 chars -> 6.6s     20,000 -> 44.5s
#
# With MAX_INFLIGHT=8, eight 20,000-character payloads hold every slot for
# 44 seconds and every real customer gets a 429. Repeat and the gateway is
# permanently unavailable, for the cost of eight HTTP requests.
#
# 4,000 characters is generous for a support message - a long complaint with
# account history is under 2,000 - and bounds the guard at ~12s.
MAX_PAYLOAD_CHARS = int(os.environ.get("MG_MAX_PAYLOAD_CHARS", "4000"))

# How often the background task refreshes provider health. Used for ORDERING
# only - attempting a call is still the real health check.
HEALTH_INTERVAL_S = float(os.environ.get("MG_HEALTH_INTERVAL_S", "15"))

STRATEGIES = {
    "policy": PolicyStrategy,
    "cheapest-first": CheapestFirstStrategy,
    "local-first": LocalFirstStrategy,
}


def _classifier_healthy() -> bool | None:
    try:
        return bool(app.state.classifier.health())
    except Exception:
        return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


def require_auth(authorization: str | None = Header(default=None)) -> None:
    """Bearer-token auth, enabled by setting MG_API_KEY.

    Unset = open, for local development, and /healthz reports that honestly
    so an open gateway is never a silent condition.
    """
    expected = os.environ.get("MG_API_KEY")
    if not expected:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    presented = authorization[7:].strip()
    # Constant-time compare: a timing side channel on token comparison is a
    # real finding in a pen test, and the fix costs nothing.
    import hmac

    if not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=403, detail="invalid token")


# --------------------------------------------------------------------------
# Startup / shutdown
# --------------------------------------------------------------------------


def _build_state(app: FastAPI) -> None:
    """Load config + policy and construct everything. Raises on bad input."""
    config = load_config(os.environ.get("MG_CONFIG", "config.yaml"))
    policy = Policy.load(os.environ.get("MG_POLICY", "policy.yaml"))
    providers = build_all(config)

    strategy_name = os.environ.get("MG_STRATEGY", "policy")
    strategy_cls = STRATEGIES.get(strategy_name)
    if strategy_cls is None:
        raise ConfigError(
            f"unknown MG_STRATEGY {strategy_name!r}, supported: {sorted(STRATEGIES)}"
        )
    strategy = (
        strategy_cls(policy) if strategy_cls is PolicyStrategy else strategy_cls()
    )

    app.state.config = config
    app.state.policy = policy
    app.state.providers = providers
    app.state.strategy = strategy
    app.state.strategy_name = strategy_name
    # Plug-and-play compliance layer: regex | presidio | composite,
    # chosen in config.yaml exactly like a model provider is.
    app.state.classifier = build_classifier(config.get("classifier"))
    app.state.classifier_name = getattr(
        app.state.classifier, "name", type(app.state.classifier).__name__
    )
    # Both stages are now real and config-driven. The input chain is
    # layered on purpose: the heuristic guard catches structural hijacks
    # that llama-guard measurably misses, llama-guard catches harmful
    # content the patterns do not describe.
    guard_cfg = config.get("guardrails") or {}
    app.state.guards_in = GuardChain(
        build_guardrails(guard_cfg.get("input"), providers)
        or [NoOpGuard(IN)]
    )
    app.state.guards_out = GuardChain(
        build_guardrails(guard_cfg.get("output"), providers)
        or [NoOpGuard(OUT)]
    )
    app.state.guard_names = {
        "input": [getattr(g, "name", type(g).__name__)
                  for g in app.state.guards_in.guards],
        "output": [getattr(g, "name", type(g).__name__)
                   for g in app.state.guards_out.guards],
    }
    app.state.health = {name: True for name in providers}


async def _health_sweep(app: FastAPI) -> None:
    """One pass over every provider, updating the cache and the gauge."""
    for name, provider in app.state.providers.items():
        try:
            app.state.health[name] = await provider.health()
        except Exception:
            app.state.health[name] = False
        app.state.metrics.set_gauge(
            "mg_provider_up",
            1 if app.state.health[name] else 0,
            {"provider": name},
        )


async def _health_loop(app: FastAPI) -> None:
    """Refresh cached provider health. Ordering input only."""
    while True:
        await asyncio.sleep(HEALTH_INTERVAL_S)
        try:
            await _health_sweep(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast: a bad config crashes the boot, not the first customer request.
    _build_state(app)
    app.state.metrics = Metrics()
    app.state.sink = EventSink(
        app.state.config.get("events", {}).get("path", "events.jsonl")
    )
    app.state.inflight = 0
    app.state.started = time.time()
    # One sweep BEFORE serving. Without it the first HEALTH_INTERVAL_S seconds
    # have no health data, so the strategy cannot demote a dead provider and
    # /metrics reports no mg_provider_up at all.
    try:
        await _health_sweep(app)
    except Exception:
        pass
    task = asyncio.create_task(_health_loop(app))

    # Process, host and event-loop metrics. Deliberately started AFTER the
    # pipeline is built and health has swept: if telemetry cannot start,
    # the gateway must still serve traffic, so this is allowed to fail.
    sampler = monitor = None
    sample_tasks: list[Any] = []
    try:
        sampler, monitor, sample_tasks = sysmetrics.start(app.state.metrics)
    except Exception:
        app.state.metrics.inc("mg_sampler_errors_total", {"stage": "start"})
    app.state.sampler = sampler
    app.state.loop_monitor = monitor

    try:
        yield
    finally:
        for running in [task, *sample_tasks]:
            running.cancel()
        for running in [task, *sample_tasks]:
            try:
                await running
            except asyncio.CancelledError:
                pass
            except Exception:
                pass


app = FastAPI(title="MigrationGuard Gateway", version="1.0", lifespan=lifespan)


# --------------------------------------------------------------------------
# Request / response
# --------------------------------------------------------------------------


class ChatRequest(BaseModel):
    messages: list[dict[str, str]] = Field(..., min_length=1)
    # OpenAI-compatible: names a PROVIDER. A PREFERENCE, never an override -
    # it narrows within the allowlist and can never add to it.
    model: str | None = None
    task: str = "default"
    max_tokens: int | None = None
    temperature: float | None = None


def _emit(
    app: FastAPI,
    *,
    request_id: str,
    task: str,
    outcome: str,
    decision: Any = None,
    routing: Any = None,
    guard_verdicts: list[Any] | None = None,
    cost_usd: float = 0.0,
    duration_s: float = 0.0,
    error: str | None = None,
) -> dict[str, Any]:
    """One record per request. The audit trail AND the cost ledger.

    Called on EVERY path including unexpected errors - the request you most
    need recorded is the one that went wrong.
    """
    provider = getattr(routing, "provider_used", None)
    zone = None
    if provider and provider in app.state.providers:
        zone = app.state.providers[provider].zone
    completion = getattr(routing, "completion", None)

    record = {
        "request_id": request_id,
        "timestamp": _now(),
        "task": task,
        "outcome": outcome,
        "provider_chosen": provider,
        "zone": zone,
        "model": getattr(completion, "model", None),
        "classification": decision.classification.as_dict() if decision else None,
        "classifier": getattr(app.state, "classifier_name", None),
        "policy_decisions": decision.as_dict() if decision else None,
        "guardrails_fired": [v.as_dict() for v in (guard_verdicts or [])],
        "fallback_hops": [h.as_dict() for h in getattr(routing, "hops", ())],
        "degraded": getattr(routing, "degraded", False),
        "latency_ms": int(duration_s * 1000),
        "tokens_in": getattr(completion, "tokens_in", 0),
        "tokens_out": getattr(completion, "tokens_out", 0),
        "cost_usd": round(cost_usd, 6),
        "error": error,
    }
    app.state.sink.emit(record)

    m = app.state.metrics
    m.inc(
        "mg_requests_total",
        {"outcome": outcome, "provider": provider or "none", "zone": zone or "none"},
    )
    m.observe(duration_s, {"outcome": outcome})
    if decision is not None:
        for rule in decision.constraints_fired:
            m.inc("mg_constraints_fired_total", {"rule": rule})
        if decision.refused:
            m.inc("mg_policy_refusals_total", {"reason": "no_allowed_provider"})
    for hop in getattr(routing, "hops", ()):
        m.inc("mg_fallback_hops_total", {"outcome": hop.outcome, "provider": hop.provider})
    if getattr(routing, "degraded", False):
        m.inc("mg_degraded_total")
    if cost_usd:
        m.inc("mg_cost_usd_total", {"zone": zone or "none"}, by=cost_usd)
    if completion is not None:
        m.inc("mg_tokens_total", {"direction": "in", "provider": provider}, by=completion.tokens_in)
        m.inc("mg_tokens_total", {"direction": "out", "provider": provider}, by=completion.tokens_out)
    return record


# --------------------------------------------------------------------------
# The gateway
# --------------------------------------------------------------------------


@app.post("/v1/chat/completions", dependencies=[Depends(require_auth)])
async def chat_completions(req: ChatRequest, response: Response) -> dict[str, Any]:
    request_id = uuid.uuid4().hex[:12]
    response.headers["X-Request-Id"] = request_id
    started = time.perf_counter()

    # Load shedding BEFORE any work. Unbounded concurrency on 2 cores means
    # memory growth and uniform timeouts; shedding gives a real saturation
    # point and makes backpressure demonstrable.
    if app.state.inflight >= MAX_INFLIGHT:
        app.state.metrics.inc("mg_shed_total")
        _emit(app, request_id=request_id, task=req.task, outcome="shed",
              duration_s=time.perf_counter() - started,
              error=f"max in-flight {MAX_INFLIGHT} reached")
        raise HTTPException(
            status_code=429,
            detail=f"gateway at capacity ({MAX_INFLIGHT} in flight)",
            headers={"Retry-After": "5", "X-Request-Id": request_id},
        )

    app.state.inflight += 1
    app.state.metrics.set_gauge("mg_inflight", app.state.inflight)
    decision = routing_result = None
    verdicts: list[Any] = []

    try:
        text = "\n".join(m.get("content", "") for m in req.messages)

        # ---- 0 SIZE LIMIT ------------------------------------------------
        # Before ANY work. Checking after classification would mean paying
        # the cost the attacker is trying to inflict.
        if len(text) > MAX_PAYLOAD_CHARS:
            app.state.metrics.inc("mg_oversize_total")
            _emit(app, request_id=request_id, task=req.task, outcome="oversize",
                  duration_s=time.perf_counter() - started,
                  error=f"payload {len(text)} chars exceeds {MAX_PAYLOAD_CHARS}")
            raise HTTPException(
                status_code=413,
                detail={
                    "error": "payload too large",
                    "chars": len(text),
                    "limit": MAX_PAYLOAD_CHARS,
                    "request_id": request_id,
                },
            )

        # ---- 1 CLASSIFY + 2 POLICY ---------------------------------------
        decision = app.state.policy.decide(
            text=text,
            task=req.task,
            providers=app.state.providers,
            classifier=app.state.classifier,
        )
        if decision.refused:
            _emit(app, request_id=request_id, task=req.task, outcome="refused",
                  decision=decision, duration_s=time.perf_counter() - started)
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "refused by policy",
                    "reason": decision.reason,
                    "request_id": request_id,
                    "migrationguard": {
                        "classification": decision.classification.as_dict(),
                        "constraints_fired": list(decision.constraints_fired),
                        "excluded": dict(decision.excluded),
                        "attempt_order": [],
                    },
                },
            )

        # ---- 3 SELECT ----------------------------------------------------
        order = app.state.strategy.order(
            decision, app.state.providers, app.state.health
        )
        if req.model:
            narrowed = tuple(n for n in order if n == req.model)
            # A caller preference NARROWS within the allowlist. If the named
            # provider was excluded by policy, the preference is ignored and
            # the audit record shows why. Governance a caller can bypass by
            # setting a field is not governance.
            order = narrowed or order

        # ---- 4 GUARD-IN --------------------------------------------------
        allowed_in, in_verdicts = await app.state.guards_in.run(text, IN)
        verdicts.extend(in_verdicts)
        if not allowed_in:
            # A guard that DENIED is the caller's problem (400). A guard that
            # ERRORED and failed closed is ours (503). Both block; they are
            # not the same incident.
            broke = any(v.errored for v in in_verdicts if not v.allowed)
            _emit(app, request_id=request_id, task=req.task,
                  outcome="guard_unavailable" if broke else "blocked_input",
                  decision=decision, guard_verdicts=verdicts,
                  duration_s=time.perf_counter() - started,
                  error=next((v.reason for v in in_verdicts if not v.allowed), None))
            raise HTTPException(
                status_code=503 if broke else 400,
                detail={
                    "error": ("input guard unavailable, failing closed" if broke
                              else "blocked by input guardrail"),
                    "verdicts": [v.as_dict() for v in in_verdicts],
                    "request_id": request_id,
                    # Every failure path carries the governance decision.
                    "migrationguard": {
                        "classification": decision.classification.as_dict(),
                        "constraints_fired": list(decision.constraints_fired),
                        "excluded": dict(decision.excluded),
                        "attempt_order": list(order),
                    },
                },
            )

        # ---- 5 CALL ------------------------------------------------------
        # Speak the NEUTRAL vocabulary only. Each adapter translates it into
        # its vendor's spelling — see providers/base.py. The handler must not
        # know that Ollama calls this num_predict and Anthropic max_tokens.
        opts: dict[str, Any] = {}
        if req.max_tokens is not None:
            opts["max_output_tokens"] = req.max_tokens
        if req.temperature is not None:
            opts["temperature"] = req.temperature

        routing_result = await execute_with_fallback(
            order, app.state.providers, req.messages,
            deadline_s=app.state.policy.chain_deadline_s, **opts,
        )

        if routing_result.refused or routing_result.completion is None:
            rejected = any(h.outcome == "rejected" for h in routing_result.hops)
            status = 400 if rejected else 503
            _emit(app, request_id=request_id, task=req.task,
                  outcome="rejected" if rejected else "unavailable",
                  decision=decision, routing=routing_result, guard_verdicts=verdicts,
                  duration_s=time.perf_counter() - started,
                  error=routing_result.reason)
            raise HTTPException(
                status_code=status,
                detail={
                    "error": routing_result.reason,
                    "hops": [h.as_dict() for h in routing_result.hops],
                    "request_id": request_id,
                    # The FAILURE path must carry the governance decision too.
                    # Otherwise "we failed rather than use the cloud" is
                    # indistinguishable from "everything was down" — and that
                    # distinction is the entire point of the guarantee.
                    "migrationguard": {
                        "classification": decision.classification.as_dict(),
                        "constraints_fired": list(decision.constraints_fired),
                        "excluded": dict(decision.excluded),
                        "attempt_order": list(order),
                    },
                },
            )

        completion = routing_result.completion

        # ---- 6 GUARD-OUT -------------------------------------------------
        allowed_out, out_verdicts = await app.state.guards_out.run(completion.text, OUT)
        verdicts.extend(out_verdicts)
        if not allowed_out:
            _emit(app, request_id=request_id, task=req.task, outcome="blocked_output",
                  decision=decision, routing=routing_result, guard_verdicts=verdicts,
                  duration_s=time.perf_counter() - started)
            raise HTTPException(
                status_code=502,
                detail={"error": "blocked by output guardrail",
                        "verdicts": [v.as_dict() for v in out_verdicts],
                        "request_id": request_id},
            )

        provider = app.state.providers[routing_result.provider_used]
        cost = provider.cost_of(completion)
        duration = time.perf_counter() - started
        _emit(app, request_id=request_id, task=req.task, outcome="ok",
              decision=decision, routing=routing_result, guard_verdicts=verdicts,
              cost_usd=cost, duration_s=duration)

        return {
            "id": request_id,
            "object": "chat.completion",
            "provider": routing_result.provider_used,
            "zone": provider.zone,
            "model": completion.model,
            "choices": [
                {"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": completion.text}}
            ],
            "usage": {
                "prompt_tokens": completion.tokens_in,
                "completion_tokens": completion.tokens_out,
                "total_tokens": completion.tokens_total,
            },
            # Governance metadata inline, so the decision is visible in the
            # curl output without tailing a log.
            "migrationguard": {
                "request_id": request_id,
                "classification": decision.classification.as_dict(),
                "constraints_fired": list(decision.constraints_fired),
                "excluded": dict(decision.excluded),
                "strategy": app.state.strategy_name,
                # policy_chain is what POLICY permitted; attempt_order is what
                # the STRATEGY actually resolved after demoting known-down
                # providers. Showing both makes "the chain said claude but we
                # called local" legible instead of looking contradictory.
                "policy_chain": list(decision.chain),
                "attempt_order": list(order),
                "hops": [h.as_dict() for h in routing_result.hops],
                "degraded": routing_result.degraded,
                # Guard detections are surfaced even when they ALLOWED the
                # response. "We looked and found nothing" and "we never
                # looked" must not be indistinguishable.
                "guardrails": [v.as_dict() for v in verdicts],
                "cost_usd": round(cost, 6),
                "latency_ms": int(duration * 1000),
            },
        }

    except HTTPException:
        raise
    except Exception as exc:
        # Catch-all that STILL emits. The one request you most want recorded
        # is the one that went wrong.
        _emit(app, request_id=request_id, task=req.task, outcome="error",
              decision=decision, routing=routing_result, guard_verdicts=verdicts,
              duration_s=time.perf_counter() - started,
              error=f"{type(exc).__name__}: {exc}")
        app.state.metrics.inc("mg_unhandled_total", {"type": type(exc).__name__})
        raise HTTPException(
            status_code=500,
            detail={"error": "internal error", "request_id": request_id},
        ) from exc
    finally:
        app.state.inflight -= 1
        app.state.metrics.set_gauge("mg_inflight", app.state.inflight)


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------


def _resource_summary() -> dict[str, Any]:
    """Human-readable view of the sampler, for /healthz and the demo.

    Prometheus and Grafana are the right way to read these over time. This
    exists so the same numbers can be shown with one curl when there is no
    time to open a dashboard, and so a collector can pull them without
    parsing the exposition format.
    """
    m = app.state.metrics
    mb = 1024.0 * 1024.0

    def g(name: str, labels: dict[str, str] | None = None) -> float:
        return float(m.get_gauge(name, labels) or 0.0)

    processes: dict[str, Any] = {}
    for name in ("gateway", "presidio", "ollama"):
        label = {"proc": name}
        if m.get_gauge("mg_proc_up", label) is None:
            continue  # never sampled - say nothing rather than say zero
        started = m.get_gauge("mg_proc_start_time_seconds", label)
        processes[name] = {
            "up": bool(g("mg_proc_up", label)),
            "processes": int(g("mg_proc_count", label)),
            "rss_mb": round(g("mg_proc_memory_rss_bytes", label) / mb, 1),
            # Percent of one core, the way top reports it...
            "cpu_percent_core": round(g("mg_proc_cpu_percent", label), 1),
            # ...and of the whole machine, which is the capacity question.
            "cpu_percent_host": round(g("mg_proc_cpu_percent_host", label), 1),
            "cpu_seconds_user": round(
                float(m.get_counter("mg_proc_cpu_seconds_total",
                                    dict(label, mode="user")) or 0.0), 1),
            "threads": int(g("mg_proc_threads", label)),
            "handles": int(g("mg_proc_handles", label)),
            "uptime_s": round(time.time() - started, 1) if started else None,
        }

    lag = m.histogram("mg_event_loop_lag_seconds")
    return {
        "sampler_available": bool(g("mg_sampler_available")),
        "sampler_samples": int(g("mg_sampler_samples")),
        "sampler_cost_ms": round(g("mg_sampler_duration_seconds") * 1000, 2),
        "host": {
            "cpu_percent": round(g("mg_host_cpu_percent"), 1),
            "cpu_physical": int(g("mg_host_cpu_count", {"kind": "physical"})),
            "cpu_logical": int(g("mg_host_cpu_count", {"kind": "logical"})),
            "memory_used_percent": round(g("mg_host_memory_used_percent"), 1),
            # CPU this breakdown cannot explain. Large means a component
            # is missing from the list, not that the machine is idle.
            "cpu_unattributed_percent": round(
                g("mg_host_cpu_unattributed_percent"), 1),
            "memory_available_mb": round(
                g("mg_host_memory_available_bytes") / mb, 0),
            "swap_used_percent": round(g("mg_host_swap_used_percent"), 1),
        },
        # The replacement for "threads active vs inactive": uvicorn runs one
        # event loop in one thread, so the thread count is flat by design
        # and these are the numbers that show saturation.
        "event_loop": {
            "lag_last_ms": round(g("mg_event_loop_lag_seconds_last") * 1000, 2),
            # Steady state only. Startup is kept apart so one spike while
            # the pipeline is being built cannot define an hour's worst case.
            "lag_max_ms": round(g("mg_event_loop_lag_seconds_max") * 1000, 2),
            "lag_p99_ms": round((lag["p99_s"] or 0.0) * 1000, 2)
            if lag and lag.get("p99_s") is not None else None,
            # This machine's system-timer overshoot, measured on an idle
            # loop at startup. Lag is only meaningful above it.
            "lag_floor_ms": round(g("mg_event_loop_lag_floor_seconds") * 1000,
                                  2),
            "lag_startup_max_ms": round(
                g("mg_event_loop_lag_startup_max_seconds") * 1000, 2),
            "warm": bool(g("mg_event_loop_warm")),
            "probes": lag["count"] if lag else 0,
            "tasks": int(g("mg_asyncio_tasks")),
            "python_threads": int(g("mg_py_threads")),
        },
        "processes": processes,
    }


@app.get("/resources", dependencies=[Depends(require_auth)])
async def resources() -> dict[str, Any]:
    """Process, host and event-loop metrics as JSON."""
    return _resource_summary()


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    """Open by design: liveness probes must not need a credential."""
    return {
        "status": "ok",
        "uptime_s": round(time.time() - app.state.started, 1),
        "providers": {
            name: {"zone": p.zone, "model": p.model,
                   "healthy": app.state.health.get(name)}
            for name, p in app.state.providers.items()
        },
        "strategy": app.state.strategy_name,
        "classifier": {
            "name": app.state.classifier_name,
            "healthy": _classifier_healthy(),
            "members": getattr(app.state.classifier, "member_names", None),
        },
        "inflight": app.state.inflight,
        "max_inflight": MAX_INFLIGHT,
        "chain_deadline_s": app.state.policy.chain_deadline_s,
        # Never let an unauthenticated gateway be a silent condition.
        "auth_enabled": bool(os.environ.get("MG_API_KEY")),
        "guardrails": app.state.guard_names,
        # Liveness only. The numbers themselves - host memory, core count,
        # what else is running on the box - are behind auth at /resources,
        # because an open endpoint that describes the machine is free
        # reconnaissance. All this says is whether telemetry is working.
        "sampler": {
            "available": bool(
                app.state.metrics.get_gauge("mg_sampler_available")),
            "samples": int(
                app.state.metrics.get_gauge("mg_sampler_samples") or 0),
        },
    }


@app.get("/metrics", dependencies=[Depends(require_auth)])
async def metrics() -> Response:
    return Response(
        content=app.state.metrics.render_prometheus(),
        media_type="text/plain; version=0.0.4",
    )


@app.get("/audit", dependencies=[Depends(require_auth)])
async def audit(limit: int = 20, only_excluded: bool = False) -> dict[str, Any]:
    """The 'prove no PII reached the cloud' query."""
    records = app.state.sink.tail(limit=max(1, min(limit, 500)))
    if only_excluded:
        records = [
            r for r in records
            if (r.get("policy_decisions") or {}).get("excluded")
        ]
    return {"count": len(records), "events": records}


@app.get("/ledger", dependencies=[Depends(require_auth)])
async def ledger() -> dict[str, Any]:
    """Cost summary: what was spent, and what was AVOIDED by routing local."""
    by_zone: dict[str, dict[str, float]] = {}
    cloud_rate = max(
        (p.cost_per_1k_tokens for p in app.state.providers.values()), default=0.0
    )
    avoided = 0.0
    for rec in app.state.sink.all_records():
        zone = rec.get("zone") or "none"
        row = by_zone.setdefault(zone, {"calls": 0, "tokens": 0, "cost_usd": 0.0})
        row["calls"] += 1
        tokens = (rec.get("tokens_in") or 0) + (rec.get("tokens_out") or 0)
        row["tokens"] += tokens
        row["cost_usd"] += rec.get("cost_usd") or 0.0
        if zone == "onprem":
            avoided += (tokens / 1000.0) * cloud_rate
    for row in by_zone.values():
        row["cost_usd"] = round(row["cost_usd"], 6)
    return {
        "by_zone": by_zone,
        "total_cost_usd": round(sum(r["cost_usd"] for r in by_zone.values()), 6),
        "estimated_cost_avoided_usd": round(avoided, 6),
        "cloud_rate_per_1k": cloud_rate,
    }


@app.get("/policy", dependencies=[Depends(require_auth)])
async def show_policy() -> dict[str, Any]:
    """Show the rule, on screen, live."""
    p = app.state.policy
    return {
        "constraints": p.constraints,
        "selection_order": p.selection_order,
        "chains": p.chains,
        "chain_deadline_s": p.chain_deadline_s,
        "on_no_allowed_provider": p.on_no_allowed_provider,
        "pii_entities": p.pii_entities,
    }


@app.post("/admin/reload", dependencies=[Depends(require_auth)])
async def reload_config() -> dict[str, Any]:
    """Re-read config.yaml and policy.yaml without a restart.

    This is what makes the demo's closing move real: change one line, reload,
    and the backend swaps with no code touched and no process bounce.

    Builds the new state FIRST and swaps only on success - a bad edit must
    never take down a running gateway.
    """
    before = {
        "strategy": app.state.strategy_name,
        "default_provider": app.state.config.get("default_provider"),
        "providers": sorted(app.state.providers),
    }
    try:
        probe = FastAPI()
        _build_state(probe)  # validates and constructs into a throwaway
    except (ConfigError, PolicyError) as exc:
        app.state.metrics.inc("mg_reload_total", {"outcome": "rejected"})
        raise HTTPException(
            status_code=400,
            detail={"error": "reload rejected, previous config still active",
                    "problem": str(exc), "active": before},
        ) from exc

    for attr in ("config", "policy", "providers", "strategy", "strategy_name",
                 "classifier", "guards_in", "guards_out"):
        setattr(app.state, attr, getattr(probe.state, attr))
    app.state.health = {
        name: app.state.health.get(name, True) for name in app.state.providers
    }
    app.state.metrics.inc("mg_reload_total", {"outcome": "ok"})
    return {
        "reloaded": True,
        "before": before,
        "after": {
            "strategy": app.state.strategy_name,
            "default_provider": app.state.config.get("default_provider"),
            "providers": sorted(app.state.providers),
        },
    }
