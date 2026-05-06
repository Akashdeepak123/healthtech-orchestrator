# v2 Upgrade — what changed and how to deploy

This is a substantial upgrade that touches `agents.py`, `main.py`, `static/index.html`,
and `requirements.txt`. Replace those four files in your repo and `git push` —
Railway will redeploy automatically in 60-90 seconds.

## What's new

**Three-pass execution per agent.** Each agent now runs in three sequential calls:
- **Pass A (Reason)** — free-form prose, the agent thinks out loud
- **Pass B (Critique)** — the same agent reads its own reasoning and stress-tests it
- **Pass C (Structure)** — the agent produces the final JSON, informed by A and B

This is the single biggest quality lever. The old single-shot prompts forced the
model to fill schema slots without ever actually thinking; now it reasons first.

**Web search for grounding.** The Market, Consumer, and Regulatory agents have
the `web_search` tool enabled during Pass A. Market actually retrieves current
Indian healthtech market reports. Consumer pulls NSSO/NFHS demographic data to
ground personas. Regulatory pulls current DPDP/ABDM/CDSCO specifications.

**Confidence scoring everywhere.** Every structured output includes `confidence`
on each major section and an `overall_confidence` score (0-100). Honest
self-assessment is part of the prompt. Low-confidence claims show as red pills.

**Reasoning traces.** Every agent's structured output includes a `reasoning_trace`
field (4-6 sentence audit trail). Plus the full Pass A reasoning and Pass B critique
are streamed to the frontend and exposed via a "Show full reasoning" drawer.

**Adversarial validation.** The validator is no longer a polite checker. It's a
senior partner at a strategy consultancy looking for evidence gaps, hallucinations,
and consistency breaks — quoting specific claims and demanding fixes.

**Live SVG dashboard / critical screen.** The Design agent now picks between a
PM-facing dashboard and a consumer-facing critical screen (based on the brief),
then produces an actual SVG wireframe that renders inline in the UI. Real labels,
real data from upstream agents, not generic placeholder boxes.

**Better progress visibility.** The status box now shows which pass is running:
"Market: reasoning out loud (with web search)…" → "self-critiquing…" →
"producing structured output…". You always know what's happening.

## Cost and time changes

- **Old run:** ~17 API calls, ~60-90 seconds, ~$0.20-0.30 per pipeline
- **New run:** ~30 API calls (3 per agent + validations), ~3-4 minutes, ~$0.50-0.80 per pipeline

The increase is expected. You're trading speed and cost for substantively deeper
output. If you want the cheaper path back, tick the **Skip validation** checkbox
in the UI — that drops the validation passes and saves ~30%.

## How to deploy

Assuming you've already deployed v1 to Railway from the GitHub repo:

1. In your local `orchestrator-backend` folder, replace these four files with the
   v2 versions: `agents.py`, `main.py`, `static/index.html`, `requirements.txt`.

2. Commit and push:
   ```bash
   git add agents.py main.py static/index.html requirements.txt
   git commit -m "v2: multi-pass agents, web search, confidence scoring, dashboard SVG"
   git push origin main
   ```

3. Railway detects the push and starts a new deployment. Watch it under **Deployments**
   in your Railway project — first deploy might take 2-3 minutes because the new
   `anthropic>=0.42.0` requirement triggers a fresh package install.

4. Once it's green, refresh your Railway URL. The UI looks similar but watch for:
   - "web search enabled" badges on Market / Consumer / Regulatory in the agent header
   - A "Reasoning Trace" block at the top of each completed agent
   - A "Show full reasoning & self-critique" drawer below it
   - Confidence pills (e.g., "Overall confidence: 78/100") in agent headers
   - The SVG screen wireframe rendered inline in the Design agent's output

5. Run preflight first to confirm the API is healthy. Then load the **Chronic Care**
   example brief and click Run. Watch the progress box — you'll see each pass tick
   through.

## Verifying it's actually v2

Visit `/api/health` on your Railway URL. The response includes `"version": "v2"`
and a list of agents with web search enabled.

## If something breaks

The most likely failure modes:

- **Web search tool errors.** Older `anthropic` SDKs don't support the
  `web_search_20250305` tool. The `requirements.txt` bump to `>=0.42.0` should
  handle this, but if the build log shows a tool-related error, the SDK didn't
  upgrade — try forcing a clean rebuild from Railway settings.

- **Long pipeline times.** A full v2 run can take 3-4 minutes with all 30+ API
  calls. Railway's default timeouts allow this, but if you see the SSE stream cut
  off, check Railway's request timeout setting under your service settings.

- **SVG not rendering.** If the design agent's wireframe shows as text/blank,
  the model returned malformed SVG. The frontend renders raw SVG via innerHTML
  with the trust assumption that the model produces valid markup; very rarely it
  doesn't. Re-running fixes it most of the time.

- **Higher costs.** If you don't want the full depth, untick "Skip validation"
  to save ~30% on every run. To save more, set `MAX_TOKENS_REASON=2500` in
  Railway env vars to compress the reasoning passes.

## What's still v1 (intentionally not changed)

- The methodology in the Word manual. The chapter structure still maps 1:1 to
  the agents; the prompts are deepened versions of the same frameworks, not new
  ones. So the manual is still the source of truth.
- The overall pipeline order. Master intake → 7 specialists → synthesis.
- The frontend aesthetic (editorial dossier).
