-- EUntangled: Supabase schema (France, demo). Run in the Supabase SQL editor.
-- Synthetic demo data only. RLS policies below are demo-level, tighten before production.

create extension if not exists vector;

-- ---------- Knowledge layer ----------

create table if not exists sources (
  id uuid primary key default gen_random_uuid(),
  url text unique not null,
  title text,
  category text,
  tier smallint not null check (tier between 1 and 3),
  access_method text,
  active boolean not null default true,
  created_at timestamptz not null default now()
);

create table if not exists source_snapshots (
  id uuid primary key default gen_random_uuid(),
  source_id uuid not null references sources(id) on delete cascade,
  fetched_at timestamptz not null default now(),
  http_status int,
  content_hash text not null,
  raw_path text,
  clean_text text,
  unique (source_id, content_hash)
);

create table if not exists rules (
  id uuid primary key default gen_random_uuid(),
  rule_key text not null,
  version int not null default 1,
  category text,
  title text not null,
  summary text,
  requirements jsonb not null default '[]',
  applies_to jsonb not null default '{}',
  depends_on text[] not null default '{}',
  evidence jsonb,
  source_id uuid references sources(id),
  source_url text,
  source_tier smallint check (source_tier between 1 and 3),
  content_hash text,
  confidence numeric(3,2),
  status text not null default 'active'
    check (status in ('active','stale','conflicting','superseded')),
  fetched_at timestamptz,
  last_checked timestamptz,
  last_verified timestamptz,
  trace jsonb,
  created_at timestamptz not null default now(),
  unique (rule_key, version)
);
create index if not exists rules_key_idx on rules (rule_key);
create index if not exists rules_applies_idx on rules using gin (applies_to);

create table if not exists rule_chunks (
  id uuid primary key default gen_random_uuid(),
  rule_id uuid not null references rules(id) on delete cascade,
  chunk_text text not null,
  page int,
  embedding vector(1536),
  fts tsvector generated always as (to_tsvector('french', chunk_text)) stored
);
create index if not exists rule_chunks_vec_idx on rule_chunks using hnsw (embedding vector_cosine_ops);
create index if not exists rule_chunks_fts_idx on rule_chunks using gin (fts);

create table if not exists review_queue (
  id uuid primary key default gen_random_uuid(),
  rule_key text,
  reason text not null,
  details jsonb,
  status text not null default 'open' check (status in ('open','resolved')),
  created_at timestamptz not null default now(),
  resolved_at timestamptz
);

-- ---------- Case layer ----------

create table if not exists cases (
  id uuid primary key default gen_random_uuid(),
  employee_name text not null,
  company text,
  origin_country text,
  nationality_group text,
  permit_type text,
  family_status text,
  city text not null default 'Paris',
  start_date date,
  status text not null default 'on_track'
    check (status in ('on_track','at_risk','blocked')),
  created_at timestamptz not null default now()
);

create table if not exists steps (
  id uuid primary key default gen_random_uuid(),
  case_id uuid not null references cases(id) on delete cascade,
  position int not null,
  rule_key text,
  title text not null,
  description text,
  status text not null default 'blocked'
    check (status in ('done','next','in_progress','blocked','at_risk')),
  est_minutes int,
  due_date date,
  source_url text,
  last_verified timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists steps_case_idx on steps (case_id, position);

create table if not exists step_dependencies (
  step_id uuid not null references steps(id) on delete cascade,
  depends_on_step_id uuid not null references steps(id) on delete cascade,
  primary key (step_id, depends_on_step_id)
);

create table if not exists agent_runs (
  id uuid primary key default gen_random_uuid(),
  case_id uuid references cases(id) on delete cascade,
  agent text not null
    check (agent in ('orchestrator','planner','retrieval','document','watcher','extractor','verifier','normalizer')),
  action text not null,
  tools jsonb not null default '[]',
  sources jsonb not null default '[]',
  mode text not null default 'live' check (mode in ('live','recorded')),
  metrics jsonb not null default '{}',
  created_at timestamptz not null default now()
);
create index if not exists agent_runs_case_idx on agent_runs (case_id, created_at desc);

create table if not exists events (
  id bigint generated always as identity primary key,
  type text not null,
  case_id uuid references cases(id) on delete cascade,
  rule_key text,
  payload jsonb not null default '{}',
  created_at timestamptz not null default now(),
  processed_at timestamptz
);
create index if not exists events_unprocessed_idx on events (created_at) where processed_at is null;

-- ---------- New rule version: supersede old one and emit rule.changed ----------

create or replace function handle_rule_version() returns trigger
language plpgsql as $$
begin
  if exists (select 1 from rules where rule_key = new.rule_key and id <> new.id) then
    update rules set status = 'superseded'
      where rule_key = new.rule_key and id <> new.id and status <> 'superseded';
    insert into events (type, rule_key, payload)
      values ('rule.changed', new.rule_key,
              jsonb_build_object('new_rule_id', new.id, 'version', new.version));
  end if;
  return new;
end $$;

drop trigger if exists trg_rule_version on rules;
create trigger trg_rule_version after insert on rules
  for each row execute function handle_rule_version();

-- ---------- Retrieval: vector search filtered by applies_to ----------
-- filter example: '{"permit_type":"salarie","city":"Paris"}'

create or replace function match_rule_chunks(
  query_embedding vector(1536),
  match_count int default 8,
  filter jsonb default '{}'
) returns table (
  chunk_id uuid, rule_id uuid, rule_key text, title text, chunk_text text,
  source_url text, source_tier smallint, status text,
  last_verified timestamptz, similarity float
)
language sql stable as $$
  select c.id, r.id, r.rule_key, r.title, c.chunk_text,
         r.source_url, r.source_tier, r.status, r.last_verified,
         1 - (c.embedding <=> query_embedding)
  from rule_chunks c
  join rules r on r.id = c.rule_id
  where r.status in ('active','stale','conflicting')
    and r.applies_to @> filter
  order by c.embedding <=> query_embedding
  limit match_count;
$$;

-- ---------- Realtime for the live activity panel ----------

alter publication supabase_realtime add table events, agent_runs, steps, cases;

-- ---------- RLS (demo): public read of demo tables, writes via service role only ----------

alter table sources enable row level security;
alter table source_snapshots enable row level security;
alter table rules enable row level security;
alter table rule_chunks enable row level security;
alter table review_queue enable row level security;
alter table cases enable row level security;
alter table steps enable row level security;
alter table step_dependencies enable row level security;
alter table agent_runs enable row level security;
alter table events enable row level security;

create policy demo_read_rules on rules for select using (true);
create policy demo_read_chunks on rule_chunks for select using (true);
create policy demo_read_review on review_queue for select using (true);
create policy demo_read_cases on cases for select using (true);
create policy demo_read_steps on steps for select using (true);
create policy demo_read_deps on step_dependencies for select using (true);
create policy demo_read_runs on agent_runs for select using (true);
create policy demo_read_events on events for select using (true);

-- ---------- Seed: Ana Torres demo case (synthetic) ----------

with c as (
  insert into cases (employee_name, company, origin_country, nationality_group,
                     permit_type, family_status, city, start_date, status)
  values ('Ana Torres', 'Northwind Consulting', 'Ecuador', 'non_eu',
          'salarie', 'single', 'Paris', '2026-11-12', 'at_risk')
  returning id
),
s as (
  insert into steps (case_id, position, rule_key, title, status, est_minutes, due_date)
  select c.id, v.pos, v.rk, v.title, v.st, v.mins, v.due::date
  from c, (values
    (1,  'employment_contract',  'Sign employment contract',                'done',        0,   '2026-09-15'),
    (2,  'long_stay_visa',       'Get long-stay visa',                      'done',        0,   '2026-09-30'),
    (3,  'arrival',              'Arrive in Paris',                         'done',        0,   '2026-10-10'),
    (4,  'visa_validation',      'Validate visa online',                    'done',        20,  '2026-10-17'),
    (5,  'prefecture_appt',      'Book prefecture appointment',             'next',        5,   '2026-10-23'),
    (6,  'proof_of_address',     'Sign lease and get proof of address',     'in_progress', 120, '2026-10-30'),
    (7,  'bank_account',         'Open a French bank account',              'blocked',     45,  '2026-11-06'),
    (8,  'social_security',      'Register for social security',            'blocked',     30,  '2026-11-10'),
    (9,  'navigo_pass',          'Get Navigo transport pass',               'blocked',     15,  '2026-10-29'),
    (10, 'tax_account',          'Set up tax account',                      'blocked',     20,  '2026-11-15')
  ) as v(pos, rk, title, st, mins, due)
  returning id, position
)
insert into step_dependencies (step_id, depends_on_step_id)
select a.id, b.id
from s a, s b
where (a.position, b.position) in ((7,6),(8,7),(8,6),(9,6),(10,5));
