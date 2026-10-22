# EUntangled

An independent AI agent that turns French relocation bureaucracy into a personal, cited plan that re-plans when your case or an official rule changes. Built at AI Innovate Paris (Accel x Iterate), 22 Oct 2026.

## How it works

**Knowledge pipeline** (`backend/pipeline.py`): fetch official source → content hash (stop if unchanged) → parse and chunk → **Extractor** (Claude, rule JSON with exact quotes) → **Verifier** (Claude, sees only record + passage, drops unsupported fields) → **Normalizer** (applies_to, depends_on) → **Resolver** (rank by source tier then recency, conflicts to review queue, new version fires `rule.changed`).

**Case agents** (`backend/agents.py`): **Watcher** consumes events, **Planner** shifts dependent steps and picks the next best action with sources, **Retrieval** answers questions grounded in case-filtered rules.

**Data** (Supabase): Postgres (versioned rules, cases, steps, agent runs), pgvector + French full-text on `rule_chunks`, `events` table + Realtime. Schema: `supabase/schema.sql`.

## Run

```bash
uv venv && uv pip install -r requirements.txt
cp .env.example .env   # fill keys
python -m backend.pipeline run
uvicorn backend.main:app --reload
```

## Honest scope

France, Paris, non-EU salaried workers. Synthetic demo case (Ana Torres). Government connectors (ANEF status, FranceConnect) are roadmap. Rule changes in the demo are simulated edits of real fetched pages, run through the full pipeline.
