"""Presidio PII analyzer, as a separate process.

Run:
    python -m uvicorn classifiers.presidio_service:app --port 8001

WHY A SEPARATE PROCESS, not an in-process call:

  1. You cannot KILL an in-process function. The fail-closed guarantee is
     only demonstrable if the classifier can actually go away.
  2. It is the unit that gets containerised later — this is the same
     boundary, just not yet packaged.
  3. It isolates a heavy dependency tree (spaCy, numpy, blis) from the
     gateway's own runtime.

The engine is built ONCE at startup; constructing it per request would cost
seconds, not milliseconds.
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

# Entities Presidio can detect that we care about. Note what is ABSENT:
# ACCOUNT_NUMBER. Presidio has no built-in recogniser for it, which is
# precisely why the composite classifier exists — see classifiers/composite.py.
DEFAULT_ENTITIES = [
    "PERSON",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "US_SSN",
    "CREDIT_CARD",
    "LOCATION",
    "IP_ADDRESS",
    "IBAN_CODE",
]

# Confidence floor. A precision/recall knob: lower catches more and
# false-positives more. The eval harness is how this gets chosen rather
# than guessed.
SCORE_THRESHOLD = float(os.environ.get("PRESIDIO_SCORE_THRESHOLD", "0.5"))

# Domain vocabulary that NER mistakes for people or places.
#
# Every entry here was found by the EVAL, not by guessing: "Billing" was
# being read as a LOCATION ("I spoke to someone in Billing last Tuesday").
# Department names, channel brands and product names all look like proper
# nouns to a model that has never seen a pay-TV support ticket.
#
# This is a precision fix and it is deliberately narrow. Over-stuffing an
# allow-list is how you quietly create a hole: anything listed here can
# never be flagged, so it must only contain terms that are NEVER personal
# data in this domain.
DOMAIN_ALLOW_LIST = [
    # internal departments
    "Billing", "Customer Service", "Support", "Engineering", "Retention",
    "Dispatch", "Scheduling",
    # the company and its products
    "DIRECTV", "Genie", "Gemini", "MyFree", "Sunday Ticket",
    # channels and competitors
    "ESPN", "Fox Sports", "NBC Sports", "Netflix", "Hulu", "Disney",
    "Dish Network", "Spectrum", "Comcast", "YouTube TV", "Max", "Paramount",
]

_state: dict[str, Any] = {"engine": None, "loaded_at": None, "error": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    started = time.perf_counter()
    try:
        from presidio_analyzer import AnalyzerEngine

        _state["engine"] = AnalyzerEngine()
        _state["loaded_at"] = round(time.perf_counter() - started, 2)
    except Exception as exc:  # surfaced via /healthz, never swallowed
        _state["error"] = f"{type(exc).__name__}: {exc}"
    yield


app = FastAPI(title="Presidio PII Analyzer", version="1.0", lifespan=lifespan)


class AnalyzeRequest(BaseModel):
    text: str = Field(default="")
    entities: list[str] | None = None
    threshold: float | None = None


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    ready = _state["engine"] is not None
    return {
        "status": "ok" if ready else "unavailable",
        "ready": ready,
        "engine_load_s": _state["loaded_at"],
        "error": _state["error"],
        "threshold": SCORE_THRESHOLD,
        "entities": DEFAULT_ENTITIES,
    }


@app.post("/analyze")
async def analyze(req: AnalyzeRequest) -> dict[str, Any]:
    engine = _state["engine"]
    if engine is None:
        # 503, not an empty result. "I could not look" must never be
        # reported as "I looked and found nothing".
        from fastapi import HTTPException

        raise HTTPException(
            status_code=503,
            detail={"error": "analyzer not ready", "reason": _state["error"]},
        )

    threshold = req.threshold if req.threshold is not None else SCORE_THRESHOLD
    started = time.perf_counter()
    results = engine.analyze(
        text=req.text,
        language="en",
        entities=req.entities or DEFAULT_ENTITIES,
        # Domain vocabulary NER misreads as people/places. Derived from
        # measured false positives, not from imagination.
        allow_list=DOMAIN_ALLOW_LIST,
    )
    kept = [r for r in results if r.score >= threshold]
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    return {
        "entities": sorted({r.entity_type for r in kept}),
        "detections": [
            {
                "entity": r.entity_type,
                "score": round(r.score, 3),
                "text": req.text[r.start:r.end],
            }
            for r in sorted(kept, key=lambda x: -x.score)
        ],
        "threshold": threshold,
        "latency_ms": elapsed_ms,
        "analyzer": "presidio",
    }
