# Healthtech Product Strategy Orchestrator

Multi-agent product strategy system. One Master Orchestrator + seven specialists, each validated before output is accepted, all running against the direct Anthropic API.

This is the production-grade backend version of the system documented in the *Healthtech Product Strategy Manual*. Same agent architecture and prompts as the artifact prototype, but running on FastAPI with no proxy in the critical path.

## Architecture

- **FastAPI** backend — async, streams agent results via SSE
- **Vanilla HTML/JS** frontend — served from the same app, no build step
- **Direct Anthropic API** — your API key, your quota, no intermediary
- **Server-Sent Events** for real-time pipeline updates to the UI

The pipeline runs nine agents in sequence: Master intake → Market Intelligence → Consumer Insights → Strategy → Product Definition → Regulatory & Operations → Metrics & Moat → Design & Prototype → Master synthesis. Each specialist gets an optional validation pass from a cheaper model (Haiku) checking source tier, internal consistency, and completeness.

## Project Structure

```
orchestrator-backend/
├── main.py              # FastAPI app — endpoints, SSE streaming, Anthropic client
├── agents.py            # All system prompts, agent pipeline, example briefs
├── static/
│   └── index.html       # Single-page frontend (no build step)
├── requirements.txt     # Python dependencies
├── Procfile             # Railway/Heroku startup command
├── railway.json         # Railway deployment config
├── .env.example         # Environment variable template
└── README.md
```

## Local Development

You need Python 3.10 or later and an Anthropic API key from [console.anthropic.com](https://console.anthropic.com).

```bash
# 1. Clone or unzip into a directory, cd in
cd orchestrator-backend

# 2. Set up a virtual environment
python -m venv .venv
source .venv/bin/activate            # macOS/Linux
# .venv\Scripts\activate              # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set your API key
cp .env.example .env
# Edit .env and paste your real ANTHROPIC_API_KEY

# 5. Load the env and run
export $(cat .env | xargs)            # macOS/Linux; or use python-dotenv
python main.py
```

Open `http://localhost:8000` in your browser. You should see the orchestrator UI. Click **Preflight · Check Connectivity** first — both stages should pass in under 5 seconds. Then load an example brief and run the pipeline.

A full pipeline run takes 60-90 seconds and uses roughly 30,000 input tokens and 15,000 output tokens with the default models. With validation skipped, ~50% less.

## Deploy to Railway (recommended — 5 minutes)

Railway is the easiest path. It auto-detects Python, installs dependencies, and runs the app from the `Procfile`.

1. Push this directory to a new GitHub repo.
2. Go to [railway.app](https://railway.app), click **New Project → Deploy from GitHub Repo**, pick this repo.
3. Railway will detect Python and start building. While it builds, click **Variables** in the project sidebar and add:
   - `ANTHROPIC_API_KEY` = your key
4. Once deployed, click **Settings → Networking → Generate Domain** to get a public URL.
5. Open the URL — the orchestrator UI loads. Run preflight to confirm.

That's it. Push to `main` to redeploy.

### Railway tips
- The `railway.json` file in this repo configures health checks at `/api/health`, so Railway will know if the app is unhealthy and restart it.
- If you need a custom domain, add it under **Settings → Networking → Custom Domain**.
- Logs are at **Deployments → [your deployment] → View Logs**. Useful when debugging.

## Deploy to Vercel (alternative)

Vercel is also viable but Python apps on Vercel run on serverless functions, which has a 10-second free-tier timeout that breaks long pipeline runs. To use Vercel for this, upgrade to Pro (60s timeout) or refactor the pipeline to stream from the edge.

If you still want to try:

1. `npm i -g vercel` and run `vercel` in the project root.
2. When prompted, accept defaults.
3. After first deploy, set the env var: `vercel env add ANTHROPIC_API_KEY`.
4. Redeploy: `vercel --prod`.

For this kind of long-running streaming workload, Railway is honestly the better fit.

## Configuration

All configuration via environment variables. See `.env.example` for the full list.

| Variable | Default | Purpose |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | (none, required) | Your direct API key |
| `MODEL_PRIMARY` | `claude-sonnet-4-20250514` | Used for all agent specialists and synthesis |
| `MODEL_VALIDATION` | `claude-haiku-4-5-20251001` | Used for validation passes (cheaper) |
| `MAX_TOKENS_SPECIALIST` | `4096` | Output budget per specialist agent |
| `MAX_TOKENS_VALIDATION` | `1024` | Output budget per validation pass |
| `ALLOWED_ORIGINS` | `*` | CORS — set to your domain in production |
| `PORT` | `8000` | Port for local dev (Railway sets this automatically) |

## API Endpoints

All under `/api/*`:

- `GET /api/health` — server-side liveness; reports if API key is configured
- `GET /api/preflight` — two-stage connectivity check (cheap ping + representative call)
- `GET /api/example-briefs` — pre-canned example briefs
- `GET /api/agent-pipeline` — pipeline metadata for the frontend sidebar
- `POST /api/run` — run the full pipeline. Body: `{"brief": "...", "skip_validation": false}`. Returns SSE stream.

The SSE stream emits these events: `start`, `agent_start`, `agent_validating`, `agent_done`, `agent_error`, `done`, `fatal`. Each event payload is a JSON object. The frontend in `static/index.html` shows the consumer pattern.

## Cost Estimation

With default models (Sonnet for agents, Haiku for validation), a full pipeline run costs roughly:
- Input tokens: ~30,000 across all 9 agents + 7 validations
- Output tokens: ~15,000 across all 9 agents + 7 validations
- At current pricing: roughly $0.20-0.30 per full run

If you skip validation, ~50% cheaper. If you put everything on Haiku, ~80% cheaper but lower quality on the specialists. Sonnet for specialists + Haiku for validation is the sweet spot.

## Extending

### Adding a new specialist agent

1. In `agents.py`, add a new prompt to `SPECIALIST_PROMPTS` dict.
2. Add the agent to `AGENT_PIPELINE` list (in the order it should run).
3. Add the agent ID to `SPECIALIST_IDS`.
4. In `static/index.html`, add a new renderer function (e.g., `renderRiskAssessment(d)`) and add a case to the `renderAgentData` switch.

That's it — the backend pipeline is data-driven from `agents.py`.

### Changing prompt voice

Every prompt in `agents.py` is a literal string. Edit in place, redeploy. The Master intake and synthesis prompts establish the "Pragmatic Visionary" voice from the manual; if you want to shift the tone, those are the load-bearing edits.

### Persisting runs

The current backend doesn't store pipeline outputs — each run is in-memory. If you want history, add a simple SQLite or Postgres layer in `main.py` and store the final results dict at the end of `run_pipeline_stream`. Railway includes free Postgres if you add it from the project sidebar.

## Why this exists

This system codifies the methodology in the *Healthtech Product Strategy Manual* (the companion Word document). The manual is the human-readable strategy playbook; this orchestrator is the same playbook turned into something you can run on a brief and get a defensible first-pass strategy in 90 seconds.

It's deliberately opinionated. The Master orchestrator refuses to skip the intake interrogation. Specialists must anchor major claims to evidence tiers. The synthesis is written in a single voice. None of these are hyperparameters — they're the methodology.
