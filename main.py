"""
FastAPI backend for the Healthtech Product Strategy Orchestrator (v2).

What's new vs v1:
- Three-pass execution per agent: REASON (free-form prose), CRITIQUE (self-review),
  STRUCTURE (JSON). Reasoning and critique aren't shown by default but are streamed
  as events so the UI can expose them on demand.
- Web-search + web-fetch tool use for market, consumer, regulatory agents during
  Pass A (the reasoning pass).
- Adversarial validation — harsh senior-partner critique replacing the polite checker.
- Confidence scores and reasoning traces embedded in every structured output.
- Design agent now produces an SVG screen wireframe rendered live in the UI.

Pipeline shape:
  master_intake.reason -> .critique -> .structure
  for each specialist:
      specialist.reason (with tools if applicable) -> .critique -> .structure
      master_validation (adversarial, Haiku)
  synthesis.reason -> .structure
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from anthropic import APIError, APIStatusError, AsyncAnthropic
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agents import (
    AGENT_PIPELINE,
    AGENTS_WITH_TOOLS,
    EXAMPLE_BRIEFS,
    JSON_DISCIPLINE,
    MASTER_INTAKE_CRITIQUE,
    MASTER_INTAKE_REASON,
    MASTER_INTAKE_STRUCTURE,
    SPECIALIST_IDS,
    SPECIALIST_PROMPTS,
    SYNTHESIS_REASON,
    SYNTHESIS_STRUCTURE,
    VALIDATION_PROMPT,
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MODEL_PRIMARY = os.getenv("MODEL_PRIMARY", "claude-sonnet-4-20250514")
MODEL_VALIDATION = os.getenv("MODEL_VALIDATION", "claude-haiku-4-5-20251001")

# Token budgets — Pass A (reason) gets the most because that's where actual
# thinking happens. Pass B (critique) is shorter. Pass C (structure) needs
# enough room for the JSON.
MAX_TOKENS_REASON = int(os.getenv("MAX_TOKENS_REASON", "3500"))
MAX_TOKENS_CRITIQUE = int(os.getenv("MAX_TOKENS_CRITIQUE", "1800"))
MAX_TOKENS_STRUCTURE = int(os.getenv("MAX_TOKENS_STRUCTURE", "4096"))
MAX_TOKENS_VALIDATION = int(os.getenv("MAX_TOKENS_VALIDATION", "1200"))

API_KEY = os.getenv("ANTHROPIC_API_KEY")
if not API_KEY:
    print("[warn] ANTHROPIC_API_KEY env var not set. /api/run will fail until set.")

client = AsyncAnthropic(api_key=API_KEY) if API_KEY else None


# ---------------------------------------------------------------------------
# JSON extraction (same robust parser as v1)
# ---------------------------------------------------------------------------

def try_parse_json(text: str) -> dict | list | None:
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    cleaned = cleaned.replace("```", "").strip()

    first = cleaned.find("{")
    if first < 0:
        return None
    last = cleaned.rfind("}")

    if last > first:
        try:
            return json.loads(cleaned[first : last + 1])
        except json.JSONDecodeError:
            pass

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
# Anthropic client wrappers
# ---------------------------------------------------------------------------

@dataclass
class ProseResult:
    text: str
    error: str | None = None


@dataclass
class StructuredResult:
    json_data: dict | None
    raw: str
    attempts: int = 1
    error: str | None = None


# Web tools the API natively supports.
# Anthropic supports a server-side web_search tool — we declare it here.
WEB_TOOLS: list[dict] = [
    {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 5,
    }
]


async def call_prose(
    system_prompt: str,
    user_message: str,
    *,
    max_tokens: int,
    model: str = MODEL_PRIMARY,
    use_web_tools: bool = False,
    max_attempts: int = 2,
) -> ProseResult:
    """Free-form prose call. Used for reasoning and critique passes.
    Optional web tool use; if a tool call is made by the model, the API
    auto-loops until the model produces a final assistant message."""
    if not client:
        return ProseResult("", error="ANTHROPIC_API_KEY not configured on server")

    last_err: str | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            kwargs = {
                "model": model,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_message}],
            }
            if use_web_tools:
                kwargs["tools"] = WEB_TOOLS
            msg = await client.messages.create(**kwargs)
            # The final response may be a mix of text and tool-use blocks.
            # We just want the assistant's prose. The API handles the
            # tool round-trips internally with web_search_20250305.
            text_parts = [
                b.text for b in msg.content
                if hasattr(b, "type") and b.type == "text" and hasattr(b, "text")
            ]
            text = "\n".join(text_parts).strip()
            if not text:
                last_err = "Empty prose response"
                await asyncio.sleep(1.5 * attempt)
                continue
            return ProseResult(text)
        except APIStatusError as e:
            last_err = f"API {e.status_code}: {str(e)[:300]}"
            if e.status_code == 429:
                await asyncio.sleep(min(5 * attempt, 15))
                continue
            if 400 <= e.status_code < 500:
                # If tool use was the issue, retry without tools
                if use_web_tools and attempt == 1:
                    use_web_tools = False
                    continue
                return ProseResult("", error=last_err)
            await asyncio.sleep(2 * attempt)
        except APIError as e:
            last_err = f"Network error: {e}"
            await asyncio.sleep(1.5 * attempt)
        except Exception as e:
            last_err = f"Unexpected: {type(e).__name__}: {e}"
            await asyncio.sleep(1.5 * attempt)

    return ProseResult("", error=last_err or "exhausted retries")


async def call_structured(
    system_prompt: str,
    user_message: str,
    *,
    max_tokens: int = MAX_TOKENS_STRUCTURE,
    model: str = MODEL_PRIMARY,
    max_attempts: int = 3,
) -> StructuredResult:
    """JSON-output call with assistant prefill to force JSON shape."""
    if not client:
        return StructuredResult(None, "", 0, error="ANTHROPIC_API_KEY not configured on server")

    last_err: str | None = None
    last_raw = ""
    current_user_message = user_message

    for attempt in range(1, max_attempts + 1):
        try:
            msg = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt + JSON_DISCIPLINE,
                messages=[
                    {"role": "user", "content": current_user_message},
                    {"role": "assistant", "content": "{"},
                ],
            )
            text_blocks = [
                b.text for b in msg.content
                if hasattr(b, "type") and b.type == "text" and hasattr(b, "text")
            ]
            raw = "{" + "".join(text_blocks)
            last_raw = raw
            parsed = try_parse_json(raw)
            if parsed is not None:
                return StructuredResult(parsed, raw, attempt)
            last_err = "JSON parse failed"
            current_user_message = (
                user_message
                + "\n\nIMPORTANT: Your previous response was not valid JSON. "
                "Respond with ONLY a JSON object."
            )
        except APIStatusError as e:
            last_err = f"API {e.status_code}: {str(e)[:300]}"
            if e.status_code == 429:
                await asyncio.sleep(min(5 * attempt, 15))
                continue
            if 400 <= e.status_code < 500:
                return StructuredResult(None, last_raw, attempt, error=last_err)
            await asyncio.sleep(2 * attempt)
        except APIError as e:
            last_err = f"Network error: {e}"
            await asyncio.sleep(1.5 * attempt)
        except Exception as e:
            last_err = f"Unexpected: {type(e).__name__}: {e}"
            await asyncio.sleep(1.5 * attempt)

    return StructuredResult(None, last_raw, max_attempts, error=last_err or "exhausted retries")


# ---------------------------------------------------------------------------
# Three-pass agent runner
# ---------------------------------------------------------------------------

@dataclass
class AgentRun:
    agent_id: str
    reasoning: str = ""
    critique: str = ""
    structured: dict | None = None
    error: str | None = None
    sources: list[str] = field(default_factory=list)


async def run_three_pass_agent(
    agent_id: str,
    reason_prompt: str,
    critique_prompt: str,
    structure_prompt: str,
    base_user_message: str,
    *,
    use_tools: bool = False,
    emit_event=None,
) -> AgentRun:
    """
    Runs a three-pass cycle for one agent.
    emit_event(event_name, payload) is an async callable for the SSE stream.
    """
    run = AgentRun(agent_id=agent_id)

    # ---- Pass A: reasoning ----
    if emit_event:
        await emit_event("agent_pass", {"id": agent_id, "pass": "reason", "uses_tools": use_tools})
    pass_a = await call_prose(
        system_prompt=reason_prompt,
        user_message=base_user_message,
        max_tokens=MAX_TOKENS_REASON,
        use_web_tools=use_tools,
    )
    if pass_a.error:
        run.error = f"Pass A (reason) failed: {pass_a.error}"
        return run
    run.reasoning = pass_a.text
    if emit_event:
        await emit_event("agent_pass_done", {"id": agent_id, "pass": "reason", "chars": len(pass_a.text)})

    # ---- Pass B: critique ----
    if emit_event:
        await emit_event("agent_pass", {"id": agent_id, "pass": "critique"})
    critique_input = (
        f"{base_user_message}\n\n"
        f"YOUR PASS A REASONING:\n{pass_a.text}"
    )
    pass_b = await call_prose(
        system_prompt=critique_prompt,
        user_message=critique_input,
        max_tokens=MAX_TOKENS_CRITIQUE,
    )
    if pass_b.error:
        # Don't kill the run — proceed to structure pass without critique
        run.critique = f"[critique skipped: {pass_b.error}]"
    else:
        run.critique = pass_b.text
        if emit_event:
            await emit_event("agent_pass_done", {"id": agent_id, "pass": "critique", "chars": len(pass_b.text)})

    # ---- Pass C: structure ----
    if emit_event:
        await emit_event("agent_pass", {"id": agent_id, "pass": "structure"})
    structure_input = (
        f"{base_user_message}\n\n"
        f"YOUR PASS A REASONING:\n{pass_a.text}\n\n"
        f"YOUR PASS B SELF-CRITIQUE:\n{run.critique}"
    )
    pass_c = await call_structured(
        system_prompt=structure_prompt,
        user_message=structure_input,
        max_tokens=MAX_TOKENS_STRUCTURE,
    )
    if not pass_c.json_data:
        run.error = f"Pass C (structure) failed: {pass_c.error or 'JSON parse failed'}"
        return run
    run.structured = pass_c.json_data
    if isinstance(pass_c.json_data.get("sources_used"), list):
        run.sources = pass_c.json_data["sources_used"]
    if emit_event:
        await emit_event("agent_pass_done", {"id": agent_id, "pass": "structure"})

    return run


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Healthtech Product Strategy Orchestrator v2")

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
    enable_tools: bool = True


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "version": "v2",
        "api_key_configured": bool(API_KEY),
        "primary_model": MODEL_PRIMARY,
        "validation_model": MODEL_VALIDATION,
        "agents_with_tools": sorted(list(AGENTS_WITH_TOOLS)),
    }


@app.get("/api/example-briefs")
async def example_briefs():
    return {"briefs": EXAMPLE_BRIEFS}


@app.get("/api/agent-pipeline")
async def agent_pipeline():
    return {"pipeline": AGENT_PIPELINE}


@app.get("/api/preflight")
async def preflight():
    if not client:
        return {"ok": False, "stage": 0, "kind": "config",
                "message": "ANTHROPIC_API_KEY not set on server"}

    start = time.time()
    stage1 = await call_structured(
        system_prompt='You are a connectivity test. Reply with the JSON {"ok": true} and nothing else.',
        user_message="Ping.",
        max_tokens=50,
        max_attempts=2,
    )
    if not stage1.json_data or stage1.json_data.get("ok") is not True:
        return {
            "ok": False, "stage": 1,
            "kind": "parse" if stage1.json_data is None else "shape",
            "message": stage1.error or f"Unexpected stage 1 reply: {stage1.raw[:120]}",
            "ms": int((time.time() - start) * 1000),
        }

    stage2 = await call_structured(
        system_prompt=(
            "You are a preflight load test. Return ONLY a JSON object with shape "
            '{"sections": [{"title": "...", "items": ["...", "...", "..."]}, ...]}. '
            "Include 4 sections, each with 5 items about Indian healthtech."
        ),
        user_message="Generate the JSON now.",
        max_tokens=1500,
        max_attempts=2,
    )
    if not stage2.json_data or not isinstance(stage2.json_data.get("sections"), list):
        return {
            "ok": False, "stage": 2,
            "kind": "parse" if stage2.json_data is None else "shape",
            "message": stage2.error or f"Stage 2 shape unexpected: {stage2.raw[:200]}",
            "ms": int((time.time() - start) * 1000),
        }

    return {"ok": True, "ms": int((time.time() - start) * 1000)}


def sse_event(event_type: str, data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event_type}\ndata: {payload}\n\n"


async def run_pipeline_stream(brief: str, skip_validation: bool, enable_tools: bool) -> AsyncIterator[str]:
    if not client:
        yield sse_event("fatal", {"message": "ANTHROPIC_API_KEY not configured on server"})
        return

    # Buffer for emitted events that the agent runner needs to push.
    # We use a queue so the runner can emit asynchronously while the
    # main loop yields them in order.
    emit_queue: asyncio.Queue = asyncio.Queue()

    async def emit(name: str, payload: Any):
        await emit_queue.put((name, payload))

    yield sse_event("start", {"pipeline": AGENT_PIPELINE, "brief_chars": len(brief), "version": "v2"})

    async def drain_queue():
        out = []
        while not emit_queue.empty():
            name, payload = emit_queue.get_nowait()
            out.append(sse_event(name, payload))
        return out

    # ---- 1. Master intake ----
    yield sse_event("agent_start", {"id": "master_intake"})
    intake_run = await run_three_pass_agent(
        agent_id="master_intake",
        reason_prompt=MASTER_INTAKE_REASON,
        critique_prompt=MASTER_INTAKE_CRITIQUE,
        structure_prompt=MASTER_INTAKE_STRUCTURE,
        base_user_message=f"BRIEF:\n{brief}",
        use_tools=False,
        emit_event=emit,
    )
    for ev in await drain_queue():
        yield ev
    if intake_run.error or not intake_run.structured:
        yield sse_event("agent_error", {"id": "master_intake", "error": intake_run.error})
        yield sse_event("fatal", {"message": "Master intake failed; pipeline cannot continue."})
        return
    yield sse_event("agent_done", {
        "id": "master_intake",
        "data": intake_run.structured,
        "reasoning": intake_run.reasoning,
        "critique": intake_run.critique,
    })

    context = (
        f"BRIEF:\n{brief}\n\n"
        f"MASTER INTAKE:\n{json.dumps(intake_run.structured, indent=2, ensure_ascii=False)}"
    )

    # ---- 2-8. Specialists ----
    for spec_id in SPECIALIST_IDS:
        use_tools = enable_tools and (spec_id in AGENTS_WITH_TOOLS)
        yield sse_event("agent_start", {"id": spec_id, "uses_tools": use_tools})
        spec_run = await run_three_pass_agent(
            agent_id=spec_id,
            reason_prompt=SPECIALIST_PROMPTS[f"{spec_id}_reason"],
            critique_prompt=SPECIALIST_PROMPTS[f"{spec_id}_critique"],
            structure_prompt=SPECIALIST_PROMPTS[f"{spec_id}_structure"],
            base_user_message=context,
            use_tools=use_tools,
            emit_event=emit,
        )
        for ev in await drain_queue():
            yield ev

        if spec_run.error or not spec_run.structured:
            yield sse_event("agent_error", {"id": spec_id, "error": spec_run.error})
            continue

        # Adversarial validation pass
        validation = None
        if not skip_validation:
            yield sse_event("agent_validating", {"id": spec_id})
            validate_input = (
                f"SPECIALIST: {spec_id}\n\n"
                f"OUTPUT:\n{json.dumps(spec_run.structured, indent=2, ensure_ascii=False)}\n\n"
                f"ORIGINAL BRIEF:\n{brief}"
            )
            val = await call_structured(
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
            "data": spec_run.structured,
            "reasoning": spec_run.reasoning,
            "critique": spec_run.critique,
            "validation": validation,
        })
        context += f"\n\n{spec_id.upper()}:\n{json.dumps(spec_run.structured, indent=2, ensure_ascii=False)}"

    # ---- 9. Synthesis (two-pass) ----
    yield sse_event("agent_start", {"id": "synthesis"})
    yield sse_event("agent_pass", {"id": "synthesis", "pass": "reason"})
    synth_reason = await call_prose(
        system_prompt=SYNTHESIS_REASON,
        user_message=context,
        max_tokens=MAX_TOKENS_REASON,
    )
    if synth_reason.error:
        yield sse_event("agent_error", {"id": "synthesis", "error": f"Synth reason: {synth_reason.error}"})
        yield sse_event("done", {"complete": False})
        return

    yield sse_event("agent_pass_done", {"id": "synthesis", "pass": "reason", "chars": len(synth_reason.text)})
    yield sse_event("agent_pass", {"id": "synthesis", "pass": "structure"})
    synth_struct = await call_structured(
        system_prompt=SYNTHESIS_STRUCTURE,
        user_message=context + f"\n\nYOUR SYNTHESIS PROSE:\n{synth_reason.text}",
        max_tokens=MAX_TOKENS_STRUCTURE,
    )
    if not synth_struct.json_data:
        yield sse_event("agent_error", {"id": "synthesis", "error": synth_struct.error or "structure failed"})
    else:
        yield sse_event("agent_done", {
            "id": "synthesis",
            "data": synth_struct.json_data,
            "reasoning": synth_reason.text,
            "critique": "",
        })

    yield sse_event("done", {"complete": True})


@app.post("/api/run")
async def run(req: RunRequest):
    return StreamingResponse(
        run_pipeline_stream(req.brief, req.skip_validation, req.enable_tools),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    index = os.path.join(static_dir, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    raise HTTPException(404, "Frontend not built — static/index.html missing")


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
