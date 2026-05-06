"""
FastAPI backend for the Healthtech Product Strategy Orchestrator.

Architecture:
- Single FastAPI app, serves both the API and the static frontend.
- /api/preflight: cheap connectivity check
- /api/run: SSE stream that runs the full agent pipeline, emitting events as
  each agent completes. Frontend listens and updates the UI in real time.
- /api/example-briefs: loads the three pre-canned briefs for the dropdown.
- All Anthropic API calls go directly to the official SDK with the
  ANTHROPIC_API_KEY env var. No proxy in the path.

Why SSE and not WebSockets: SSE is one-way (server → client) which is exactly
what the pipeline produces, has built-in reconnect, works through proxies,
and needs no extra dependencies. The frontend uses native EventSource.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

from anthropic import APIError, APIStatusError, AsyncAnthropic
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agents import (
    AGENT_PIPELINE,
    EXAMPLE_BRIEFS,
    MASTER_INTAKE_PROMPT,
    SPECIALIST_IDS,
    SPECIALIST_PROMPTS,
    SYNTHESIS_PROMPT,
    VALIDATION_PROMPT,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MODEL_PRIMARY = os.getenv("MODEL_PRIMARY", "claude-sonnet-4-20250514")
MODEL_VALIDATION = os.getenv("MODEL_VALIDATION", "claude-haiku-4-5-20251001")
MAX_TOKENS_SPECIALIST = int(os.getenv("MAX_TOKENS_SPECIALIST", "4096"))
MAX_TOKENS_VALIDATION = int(os.getenv("MAX_TOKENS_VALIDATION", "1024"))

API_KEY = os.getenv("ANTHROPIC_API_KEY")
if not API_KEY:
    # Don't crash at import time — let the health check report it cleanly.
    print("[warn] ANTHROPIC_API_KEY env var not set. /api/run will fail until set.")

# Single shared async client. The Anthropic SDK is thread-safe and handles
# connection pooling internally.
client = AsyncAnthropic(api_key=API_KEY) if API_KEY else None


# ---------------------------------------------------------------------------
# JSON extraction — same robust parser as the JSX, ported to Python
# ---------------------------------------------------------------------------

def try_parse_json(text: str) -> dict | list | None:
    """Tolerant JSON extractor. Strips fences, finds first balanced {…},
    repairs truncation by closing dangling braces/brackets."""
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    cleaned = cleaned.replace("```", "").strip()

    first = cleaned.find("{")
    if first < 0:
        return None
    last = cleaned.rfind("}")

    # Strategy 1: full extracted span
    if last > first:
        try:
            return json.loads(cleaned[first : last + 1])
        except json.JSONDecodeError:
            pass

    # Strategy 2: forward bracket-balance with string awareness
    s = cleaned[first:]
    depth = 0
    in_str = False
    escape = False
    end_idx = -1
    for i, c in enumerate(s):
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end_idx = i
                break
    if end_idx >= 0:
        try:
            return json.loads(s[: end_idx + 1])
        except json.JSONDecodeError:
            pass

    # Strategy 3: truncation repair — count unclosed structures, append closers
    open_curly = open_square = 0
    in_str = False
    escape = False
    for c in s:
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            open_curly += 1
        elif c == "}":
            open_curly -= 1
        elif c == "[":
            open_square += 1
        elif c == "]":
            open_square -= 1

    repaired = s
    if in_str:
        last_quote = repaired.rfind('"')
        if last_quote > 0:
            repaired = repaired[: last_quote + 1]
    repaired = re.sub(r"[,:\s]+$", "", repaired)
    repaired += "]" * max(0, open_square)
    repaired += "}" * max(0, open_curly)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Anthropic client wrapper with retry and JSON enforcement
# ---------------------------------------------------------------------------

@dataclass
class AgentResult:
    json_data: dict | None
    raw: str
    attempts: int
    error: str | None = None


JSON_DISCIPLINE = (
    "\n\nCRITICAL OUTPUT RULES:\n"
    "- Output ONLY a valid JSON object.\n"
    "- Your response MUST start with the character { and end with the character }.\n"
    "- No prose before the {, no prose after the }, no markdown code fences, no commentary.\n"
    '- If you cannot fully complete a field, use "TBD" as the value rather than truncating the JSON.'
)


async def call_claude(
    system_prompt: str,
    user_message: str,
    *,
    max_tokens: int = MAX_TOKENS_SPECIALIST,
    model: str = MODEL_PRIMARY,
    max_attempts: int = 3,
) -> AgentResult:
    """Direct Anthropic API call with assistant prefill (the {  trick) to force
    JSON output, plus automatic retry on transient errors."""
    if not client:
        return AgentResult(None, "", 0, error="ANTHROPIC_API_KEY not configured on server")

    last_err: str | None = None
    last_raw = ""
    for attempt in range(1, max_attempts + 1):
        try:
            # The direct API supports assistant message prefill — unlike the artifact proxy.
            # Prefilling "{" forces the model into JSON-shaped output from token 1.
            msg = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt + JSON_DISCIPLINE,
                messages=[
                    {"role": "user", "content": user_message},
                    {"role": "assistant", "content": "{"},
                ],
            )
            # Reattach the prefilled "{" so downstream parsers see complete JSON
            text_blocks = [b.text for b in msg.content if b.type == "text"]
            raw = "{" + "".join(text_blocks)
            last_raw = raw
            parsed = try_parse_json(raw)
            if parsed is not None:
                return AgentResult(parsed, raw, attempt)
            # Parse failure — retry with stronger reminder
            last_err = "JSON parse failed"
            user_message = (
                user_message
                + "\n\nIMPORTANT: Your previous response was not valid JSON. "
                "Respond with ONLY a JSON object, nothing else."
            )
        except APIStatusError as e:
            # 4xx/5xx from the API — auth, rate limit, etc.
            last_err = f"API {e.status_code}: {str(e)[:200]}"
            if e.status_code == 429:
                # Rate limited — back off longer
                await asyncio.sleep(min(5 * attempt, 15))
                continue
            if 400 <= e.status_code < 500:
                # 4xx other than 429 won't fix itself, fail fast
                return AgentResult(None, last_raw, attempt, error=last_err)
            # 5xx — retry with backoff
            await asyncio.sleep(2 * attempt)
        except APIError as e:
            # Network-level
            last_err = f"Network error: {e}"
            await asyncio.sleep(1.5 * attempt)
        except Exception as e:
            last_err = f"Unexpected error: {type(e).__name__}: {e}"
            await asyncio.sleep(1.5 * attempt)

    return AgentResult(None, last_raw, max_attempts, error=last_err or "exhausted retries")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Healthtech Product Strategy Orchestrator")

# CORS — permissive in dev, lock down in prod via env
allowed_origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

class RunRequest(BaseModel):
    brief: str = Field(..., min_length=20, max_length=10000)
    skip_validation: bool = False


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "api_key_configured": bool(API_KEY),
        "primary_model": MODEL_PRIMARY,
        "validation_model": MODEL_VALIDATION,
    }


@app.get("/api/example-briefs")
async def example_briefs():
    return {"briefs": EXAMPLE_BRIEFS}


@app.get("/api/agent-pipeline")
async def agent_pipeline():
    return {"pipeline": AGENT_PIPELINE}


@app.get("/api/preflight")
async def preflight():
    """Two-stage check: cheap ping + representative larger call."""
    if not client:
        return {"ok": False, "stage": 0, "kind": "config", "message": "ANTHROPIC_API_KEY not set on server"}

    start = time.time()

    # Stage 1: cheap ping
    stage1 = await call_claude(
        system_prompt='You are a connectivity test. Reply with the JSON {"ok": true} and nothing else.',
        user_message="Ping.",
        max_tokens=50,
        max_attempts=2,
    )
    if not stage1.json_data or stage1.json_data.get("ok") is not True:
        return {
            "ok": False,
            "stage": 1,
            "kind": "parse" if stage1.json_data is None else "shape",
            "message": stage1.error or f"Unexpected stage 1 reply: {stage1.raw[:120]}",
            "ms": int((time.time() - start) * 1000),
        }

    # Stage 2: representative call — 1500 tokens of structured output
    stage2 = await call_claude(
        system_prompt=(
            "You are a preflight load test. Return ONLY a JSON object with this shape: "
            '{"sections": [{"title": "...", "items": ["...", "...", "..."]}, ...]}. '
            "Include 4 sections, each with 5 items. Each item should be a short sentence "
            "about Indian healthtech."
        ),
        user_message="Generate the JSON now.",
        max_tokens=1500,
        max_attempts=2,
    )
    if not stage2.json_data or not isinstance(stage2.json_data.get("sections"), list):
        return {
            "ok": False,
            "stage": 2,
            "kind": "parse" if stage2.json_data is None else "shape",
            "message": stage2.error or f"Stage 2 shape unexpected: {stage2.raw[:200]}",
            "ms": int((time.time() - start) * 1000),
        }

    return {"ok": True, "ms": int((time.time() - start) * 1000)}


def sse_event(event_type: str, data: Any) -> str:
    """Format a Server-Sent Event. JSON-encode the payload."""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event_type}\ndata: {payload}\n\n"


async def run_pipeline_stream(brief: str, skip_validation: bool) -> AsyncIterator[str]:
    """The full agent pipeline as an SSE stream. Each yielded string is one event."""
    if not client:
        yield sse_event("fatal", {"message": "ANTHROPIC_API_KEY not configured on server"})
        return

    yield sse_event("start", {"pipeline": AGENT_PIPELINE, "brief_chars": len(brief)})

    # ---- 1. Master intake ----
    yield sse_event("agent_start", {"id": "master_intake"})
    intake = await call_claude(MASTER_INTAKE_PROMPT, f"BRIEF:\n{brief}")
    if not intake.json_data:
        yield sse_event("agent_error", {
            "id": "master_intake",
            "error": intake.error or "JSON parse failed after retries",
            "raw": intake.raw[:2000] if intake.raw else None,
        })
        yield sse_event("fatal", {"message": "Master intake failed; pipeline cannot continue."})
        return
    yield sse_event("agent_done", {"id": "master_intake", "data": intake.json_data, "attempts": intake.attempts})

    # Build accumulating context for the specialists
    context = f"BRIEF:\n{brief}\n\nMASTER INTAKE:\n{json.dumps(intake.json_data, indent=2, ensure_ascii=False)}"

    # ---- 2-8. Specialists, each with optional validation ----
    for spec_id in SPECIALIST_IDS:
        yield sse_event("agent_start", {"id": spec_id})
        out = await call_claude(SPECIALIST_PROMPTS[spec_id], context)
        if not out.json_data:
            yield sse_event("agent_error", {
                "id": spec_id,
                "error": out.error or "JSON parse failed after retries",
                "raw": out.raw[:2000] if out.raw else None,
            })
            # Don't kill the pipeline — skip ahead with a placeholder
            continue

        # Validation pass (cheaper Haiku model, non-blocking)
        validation = None
        if not skip_validation:
            yield sse_event("agent_validating", {"id": spec_id})
            validate_input = (
                f"SPECIALIST: {spec_id}\n"
                f"OUTPUT:\n{json.dumps(out.json_data, indent=2, ensure_ascii=False)}\n\n"
                f"ORIGINAL BRIEF:\n{brief}"
            )
            val = await call_claude(
                VALIDATION_PROMPT,
                validate_input,
                max_tokens=MAX_TOKENS_VALIDATION,
                model=MODEL_VALIDATION,
                max_attempts=2,
            )
            if val.json_data:
                validation = val.json_data

        yield sse_event("agent_done", {
            "id": spec_id,
            "data": out.json_data,
            "validation": validation,
            "attempts": out.attempts,
        })
        context += f"\n\n{spec_id.upper()}:\n{json.dumps(out.json_data, indent=2, ensure_ascii=False)}"

    # ---- 9. Synthesis ----
    yield sse_event("agent_start", {"id": "synthesis"})
    synth = await call_claude(SYNTHESIS_PROMPT, context)
    if not synth.json_data:
        yield sse_event("agent_error", {
            "id": "synthesis",
            "error": synth.error or "JSON parse failed after retries",
            "raw": synth.raw[:2000] if synth.raw else None,
        })
    else:
        yield sse_event("agent_done", {"id": "synthesis", "data": synth.json_data, "attempts": synth.attempts})

    yield sse_event("done", {"complete": True})


@app.post("/api/run")
async def run(req: RunRequest):
    """SSE stream of the full pipeline. Frontend uses fetch + ReadableStream
    rather than EventSource because EventSource doesn't support POST bodies."""
    return StreamingResponse(
        run_pipeline_stream(req.brief, req.skip_validation),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # tell nginx not to buffer
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

# Mount static files. The index.html is served by an explicit route below
# so it's reachable at "/" without a trailing slash.
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    index = os.path.join(static_dir, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    raise HTTPException(404, "Frontend not built — static/index.html missing")


# ---------------------------------------------------------------------------
# Local dev entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
