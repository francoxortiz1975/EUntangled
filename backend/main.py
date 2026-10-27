from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from backend import agents, pipeline
from backend.common import db

app = FastAPI(title="EUntangled API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def private_network(request, call_next):
    # Lets the deployed site (https) call this API on localhost (Chrome Private Network Access).
    if request.method == "OPTIONS" and request.headers.get("access-control-request-private-network"):
        from fastapi.responses import Response
        return Response(status_code=204, headers={
            "Access-Control-Allow-Origin": request.headers.get("origin", "*"),
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "content-type",
            "Access-Control-Allow-Private-Network": "true"})
    resp = await call_next(request)
    resp.headers["Access-Control-Allow-Private-Network"] = "true"
    return resp


class Delay(BaseModel):
    rule_key: str = "prefecture_appt"
    days: int = 14


class Question(BaseModel):
    question: str


class RuleEdit(BaseModel):
    source_url: str
    find: str
    replace: str


class NewCase(BaseModel):
    name: str
    origin: str = ""
    nationality_group: str = "non_eu"
    permit_type: str = "salarie"
    family_status: str = "single"
    start_date: str
    housing: str = "searching"


# Base step graph for a Paris move. EU nationals skip the visa and permit steps.
TEMPLATE = [
    (1, "employment_contract", "Sign employment contract", 0, []),
    (2, "long_stay_visa", "Get long-stay visa", 0, [1]),
    (3, "arrival", "Arrive in Paris", 0, [2]),
    (4, "visa_validation", "Validate visa online", 20, [3]),
    (5, "residence_permit", "Book prefecture appointment", 5, [4]),
    (6, "housing_guarantee", "Sign lease and get proof of address", 120, [3]),
    (7, "bank_account", "Open a French bank account", 45, [6]),
    (8, "social_security", "Register for social security", 30, [6, 7]),
    (9, "navigo_pass", "Get Navigo transport pass", 15, [6]),
    (10, "tax_account", "Set up tax account", 20, [6]),
]
NON_EU_ONLY = {2, 4, 5}


@app.post("/cases")
def create_case(body: NewCase):
    """Onboarding quiz: create a real case, build its steps from the template, then the Planner plans it."""
    from datetime import date, timedelta
    c = db.table("cases").insert({
        "employee_name": body.name, "company": "Northwind Consulting", "origin_country": body.origin,
        "nationality_group": body.nationality_group, "permit_type": body.permit_type,
        "family_status": body.family_status, "city": "Paris", "start_date": body.start_date, "status": "on_track",
    }).execute().data[0]
    start = date.fromisoformat(body.start_date)
    eu = body.nationality_group == "eu"
    rows = [t for t in TEMPLATE if not (eu and t[0] in NON_EU_ONLY)]
    keep = {t[0] for t in rows}
    done = {1, 2, 3} if not eu else {1, 3}
    if body.housing == "found":
        done.add(6)
    ins = []
    for pos, key, title, mins, _ in rows:
        due = start - timedelta(days=max(1, 28 - pos * 3))
        ins.append({"case_id": c["id"], "position": pos, "rule_key": key, "title": title, "est_minutes": mins,
                    "due_date": due.isoformat(), "status": "done" if pos in done else "blocked"})
    steps = db.table("steps").insert(ins).execute().data
    by_pos = {s["position"]: s for s in steps}
    deps = [{"step_id": by_pos[p]["id"], "depends_on_step_id": by_pos[d]["id"]}
            for p, _, _, _, ds in rows for d in ds if d in keep]
    if deps:
        db.table("step_dependencies").insert(deps).execute()
    _unlock(c["id"])
    agents.log_run("orchestrator", f"Created case for {body.name} from onboarding", case_id=c["id"])
    plan = agents.replan(c["id"], {"type": "case.created", "profile": body.model_dump()})
    return {"case_id": c["id"], "plan": plan}


def _unlock(case_id: str):
    """Blocked steps whose dependencies are all done become 'next'."""
    _, steps, deps = agents.load_case(case_id)
    status = {s["id"]: s["status"] for s in steps}
    for s in steps:
        if s["status"] != "blocked":
            continue
        needs = [d["depends_on_step_id"] for d in deps if d["step_id"] == s["id"]]
        if all(status.get(n) == "done" for n in needs):
            db.table("steps").update({"status": "next"}).eq("id", s["id"]).execute()


@app.post("/cases/{case_id}/steps/{position}/complete")
def complete_step(case_id: str, position: int):
    """Mark a step done, unlock what depended on it, and have the Planner re-plan."""
    db.table("steps").update({"status": "done"}).eq("case_id", case_id).eq("position", position).execute()
    db.table("events").insert({"type": "step.completed", "case_id": case_id, "payload": {"position": position}}).execute()
    _unlock(case_id)
    agents.log_run("orchestrator", f"step.completed #{position}, routed to planner", case_id=case_id)
    return agents.replan(case_id, {"type": "step.completed", "position": position})


@app.get("/")
def root():
    return {"service": "EUntangled API", "status": "ok", "docs": "/docs", "health": "/health"}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/cases")
def cases():
    rows = db.table("cases").select("*").order("created_at").execute().data
    review = db.table("review_queue").select("*").eq("status", "open").execute().data
    for c in rows:
        steps = db.table("steps").select("status").eq("case_id", c["id"]).execute().data
        c["steps_done"] = sum(s["status"] == "done" for s in steps)
        c["steps_total"] = len(steps)
        c["steps_at_risk"] = sum(s["status"] == "at_risk" for s in steps)
    return {"cases": rows, "review_queue": review}


@app.get("/cases/{case_id}/plan")
def plan(case_id: str):
    case, steps, deps = agents.load_case(case_id)
    rules = {r["rule_key"]: r for r in agents.case_rules(case)}
    for s in steps:
        r = rules.get(s["rule_key"])
        s["rule"] = agents.slim(r) if r else None
        s["depends_on"] = [d["depends_on_step_id"] for d in deps if d["step_id"] == s["id"]]
    runs = (db.table("agent_runs").select("*").or_(f"case_id.eq.{case_id},case_id.is.null")
            .order("created_at", desc=True).limit(20).execute().data)
    last_plan = next((r for r in runs if r["agent"] == "planner" and r["case_id"] == case_id), None)
    return {"case": case, "steps": steps, "activity": runs,
            "next_best_action": last_plan["metrics"].get("next_best_action") if last_plan else None,
            "latest_plan": last_plan["metrics"].get("plan") if last_plan else None}


@app.post("/cases/{case_id}/delay")
def delay(case_id: str, body: Delay):
    db.table("events").insert({"type": "step.delayed", "case_id": case_id, "payload": body.model_dump()}).execute()
    return {"results": agents.watcher()}


@app.post("/cases/{case_id}/replan")
def replan(case_id: str):
    return agents.replan(case_id, {"type": "manual"})


@app.post("/cases/{case_id}/ask")
def ask(case_id: str, body: Question):
    return agents.answer(case_id, body.question)


@app.post("/events/process")
def process():
    return {"results": agents.watcher()}


@app.post("/simulate/rule-change")
def simulate_rule_change(body: RuleEdit):
    """Demo: edit the latest snapshot of a real source, then run it through the full pipeline.
    A changed hash -> Extractor -> Verifier -> Resolver -> new rule version -> rule.changed -> Watcher -> Planner."""
    src = db.table("sources").select("*").eq("url", body.source_url).single().execute().data
    snap = (db.table("source_snapshots").select("clean_text").eq("source_id", src["id"])
            .order("fetched_at", desc=True).limit(1).execute().data)
    text = snap[0]["clean_text"].replace("\xa0", " ") if snap else ""
    if body.find not in text:
        raise HTTPException(400, "text to replace not found in latest snapshot")
    edited = text.replace(body.find, body.replace)
    ingest = pipeline.run_source(src, override_text=edited)
    pending = db.table("events").select("id,payload").eq("type", "rule.changed").is_("processed_at", "null").execute().data
    for ev in pending:
        db.table("events").update({"payload": {**ev["payload"], "edit": {"source_url": body.source_url,
                                   "before": body.find, "after": body.replace}}}).eq("id", ev["id"]).execute()
    return {"ingest": ingest, "replans": agents.watcher(), "mode": "simulated source edit"}
