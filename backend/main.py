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
