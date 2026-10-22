# EUntangled

**An independent AI case companion for people moving to France.** It turns bureaucracy into a personal, cited plan that re-plans itself when your case or an official rule changes.

Built in one day at AI Innovate Paris (Accel × Iterate), 22 Oct 2026. Scope today: Paris, non-EU salaried workers.

---

## The problem

Moving to Paris as a non-EU employee means 10+ admin steps that depend on each other: visa validation, prefecture appointment, lease, bank account, social security, tax account, transport. One delay cascades. Without a proof of address you can't open a bank account, and without a bank account you can't get paid or registered.

**Pain points**
- **Employees** lose weeks to dependencies nobody explained, French-only pages, and rules that differ by nationality, permit and age.
- **Mobility and HR teams** answer the same questions by email, case by case, with no view of who is about to slip.
- **Official rules change.** A guide written last year, or an LLM trained last year, can be wrong today, and nobody tells the people already in the middle of a move.
- **Current options:** relocation agencies (expensive, human-only), enterprise platforms like Localyze (built for large companies, fixed rule pipelines), or generic city guides (the same list for everyone, with no tracking).

## Who it's for

- **Buyer:** global mobility and HR teams at consultancies and mid-size multinationals that move people between offices.
- **User:** the employee being moved. Our demo case is Ana Torres, a software engineer moving from Quito to Paris and starting 12 Nov 2026 (synthetic).
- **Next segments:** university international offices, relocation providers, scale-ups without a mobility team.

## What it does

| | |
|---|---|
| **Personal plan per case** | Steps, dependencies and dates built for one person's nationality group, permit type, city and start date. The plan names the next best action ("Book your prefecture appointment by Fri 24 Oct, 5 min, unblocks 3 steps"). |
| **Automatic tracking** | Every case event (a delay, a completed step, a rule change) flows through an events table. Dependent steps shift, at-risk steps are flagged, and the manager sees the cause. |
| **Live research on official sources** | The pipeline fetches official pages (service-public.fr, ANEF, Ameli, Banque de France rules, Visale, DossierFacile) and turns them into verified rule records, each with its exact supporting quote. |
| **Agents that monitor for change** | When a page's content hash changes, the new rule version fires `rule.changed`. The Watcher finds every affected case and the Planner re-plans it, with the source shown. |
| **Every answer is cited** | Each step and answer carries its source URL, source tier, last-verified date and confidence. With no source, the agent says "confirm with a specialist". |
| **Document checks** *(prototype)* | Upload a lease or a payslip. The Document agent checks it against the requirement and flags problems, such as a missing move-in date. Mocked in the UI today. |
| **Manager view** | All cases with risk, blockers, a "needs human review" queue for conflicting or low-confidence rules, and one-click nudges. |

## Why not just ask Claude or ChatGPT?

**Trust.** A chat model answers from memory, so you can't tell when a rule changed or where the answer came from. We tested that.

### Evidence (recorded run, same model, same questions)

**Run 1: 15 questions, references taken from official source text**

| | No data (model alone) | EUntangled (grounded) |
|---|---|---|
| Answers with at least one working source link | **7 / 15** | **15 / 15** |
| Every cited link works | 4 / 15 | 15 / 15 |
| Correct against the official text | 9 / 15 (60%) | 10 / 15 (67%) |

**Run 2: 20 hard, multi-condition questions, 5 per category**

| Category | Correct: no data | Correct: EUntangled | Working source link: no data | Working source link: EUntangled |
|---|---|---|---|---|
| Housing | 1 / 5 | **5 / 5** | 1 / 5 | **5 / 5** |
| Bank | 3 / 5 | **5 / 5** | 2 / 5 | 4 / 5 |
| Visa & permits | 3 / 5 | 3 / 5 | 3 / 5 | **5 / 5** |
| Health & social security | 2 / 5 | 2 / 5 | 4 / 5 | **5 / 5** |
| **Total** | **9 / 20 (45%)** | **15 / 20 (75%)** | **10 / 20 (50%)** | **19 / 20 (95%)** |

How it works:
- Questions and references were generated from the verified quotes in our corpus.
- An LLM judge graded the answers against those references.
- Link checks are real HTTP requests made during the run.
- Raw results are in `data/eval_results*.json`.

Limits:
- Small sample, one run each. Treat accuracy as indicative.
- References were not checked by a lawyer.
- Tax, phone and transport have no rules in the corpus yet, so they are not scored.

**Without data, about half the answers point to a page that doesn't load. With EUntangled, every answer points to a working official page, together with the date it was last verified.** Beyond accuracy, a chat model can't do the rest of the job:
- no memory of your case
- no tracking of dependencies
- no way to tell you when a rule changes after you asked
- no view across 50 relocating employees for the HR team

## How it works

```
Official sources ──fetch──▶ snapshot + content hash ──unchanged? stop (no model cost)
                                   │ changed
                                   ▼
             Extractor (Claude)  → strict JSON rule + exact quote per requirement
             Verifier  (Claude)  → sees only record + passage, drops unsupported fields, sets confidence
             Normalizer          → applies_to tags, depends_on links
             Resolver            → rank by source tier, then recency; conflicts → review queue
                                   │ new rule version
                                   ▼
                    DB trigger emits rule.changed  ──▶  Watcher finds affected cases
                                                        Planner re-plans (dates, risk, next action)
                                                        Employee + manager see the cause, with sources
```

**Agents:** Orchestrator, Planner, Retrieval, Watcher, Document (prototype), plus the pipeline agents Extractor, Verifier and Normalizer. Every run is logged to `agent_runs` with its sources, its metrics (for example the Verifier pass rate) and a live or recorded tag.

**Data layer (Supabase):**
- Postgres holds versioned `rules`, `sources`, `source_snapshots`, `cases`, `steps`, `step_dependencies`, `agent_runs` and `review_queue`.
- `rule_chunks` adds pgvector embeddings and French full-text search, filtered to the case.
- The `events` table plus Realtime carries changes live to the UI.

Schema: [`supabase/schema.sql`](supabase/schema.sql).

**Current numbers:** 9 official sources, 20 active verified rules, Verifier pass rate about 95% of fields.

## Repo

```
backend/
  pipeline.py   fetch → hash → parse → Extractor → Verifier → Normalizer → Resolver
  agents.py     Retrieval, Planner, Watcher, Orchestrator
  main.py       FastAPI: /cases, /cases/{id}/plan, /delay, /ask, /simulate/rule-change
  eval.py       no-data vs grounded benchmark
data/           sources.csv, eval questions and results
supabase/       schema, trigger, vector search function
web/            frontend (Lovable, React + TanStack), submodule
```

## Run it

```bash
uv venv && uv pip install -r requirements.txt
cp .env.example .env               # Anthropic, OpenAI (embeddings), Supabase service role
python -m backend.pipeline run     # ingest official sources
uvicorn backend.main:app --port 8000
python -m backend.eval build && python -m backend.eval run
```

Frontend: `cd web && bun install && VITE_API_URL=http://localhost:8000 bun run dev`. Without `VITE_API_URL` it runs on mocked data.

## What's live and what's not

- **Live:** source ingestion, Extractor and Verifier, rule versioning with `rule.changed`, Watcher and Planner re-plans, grounded answers with citations, and the eval.
- **Simulated:** the rule change in the demo. It is a real edit to a real fetched page (the Visale age limit), then run through the full pipeline. Official pages rarely change on demo day.
- **Mocked prototype:** document upload checks, Telegram and email reminders, and the other cases in the manager view.
- **Roadmap:**
  - scheduled crawls (pg_cron)
  - government connectors (ANEF status, FranceConnect)
  - more sources (tax, transport, Légifrance via PISTE)
  - more countries across the EU

All personal data in the demo is synthetic. Production would run in an EU region and keep personal data to a minimum.
