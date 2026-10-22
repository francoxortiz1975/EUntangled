"""Case agents: Retrieval, Planner, Watcher, Orchestrator."""
import json
from datetime import date, datetime, timedelta, timezone

from backend.common import ask_json, db, embed, log_run

STALE_DAYS = 30

PLANNER_SYSTEM = """You are the Planner for a relocation case in Paris. You get the case profile, its steps (with status,
due dates, dependencies), the official rules that apply (with source and freshness) and what just changed.
Decide what the employee should do this week. Use only the given rules; if a step has no rule, say "confirm with a specialist".
Voice: a calm, competent relocation coach. Warm but precise, plain English, second person. Every action is a verb with a
date ("Book your prefecture appointment by Fri 24 Oct"). Say what is fine, what slipped and the fix. No hype, no filler.
If the cause includes an edit or a changed rule, name the concrete change (for example an age limit or fee that moved).
Output: {"next_best_action": {"step_position": int, "title": str, "why": str, "unblocks": int},
         "this_week": [short dated actions], "before_start": [short dated actions], "first_90_days": [short actions],
         "day7_proof": [what "done" looks like by next week],
         "risk": "on_track|at_risk|blocked", "risk_reason": str,
         "explanation": 2-3 sentences on what changed and why the plan moved, citing source URLs,
         "reminder": one short message to send the employee}"""

ANSWER_SYSTEM = """You answer a relocating employee's question using ONLY the rules provided. Cite every claim with its
source_url and last_verified date. If the rules do not cover it, or a rule is stale or conflicting, say so and advise
confirming with a specialist.
Output: {"answer": str, "citations": [{"source_url": str, "last_verified": str, "rule_key": str}], "confidence": 0.0-1.0}"""


def applies(rule: dict, case: dict) -> bool:
    for k, v in (rule.get("applies_to") or {}).items():
        if case.get(k) and v != case.get(k):
            return False
    return True


def freshness(rule: dict):
    lv = rule.get("last_verified")
    if not lv:
        return "unverified"
    age = datetime.now(timezone.utc) - datetime.fromisoformat(lv.replace("Z", "+00:00"))
    return "stale" if age.days > STALE_DAYS else "fresh"


def case_rules(case: dict):
    rules = db.table("rules").select("*").neq("status", "superseded").execute().data
    out = []
    for r in rules:
        if applies(r, case):
            r["freshness"] = freshness(r)
            out.append(r)
    return out


def retrieve(case: dict, question: str, k: int = 6):
    """Retrieval agent: vector search when embeddings exist, else keyword overlap, always filtered to the case."""
    rules = {r["id"]: r for r in case_rules(case)}
    vec = embed([question])
    hits = []
    if vec:
        res = db.rpc("match_rule_chunks", {"query_embedding": vec[0], "match_count": 20, "filter": {}}).execute().data
        for h in res:
            if h["rule_id"] in rules and h["rule_id"] not in [x["id"] for x in hits]:
                hits.append(rules[h["rule_id"]])
    if not hits:
        words = set(question.lower().split())
        scored = sorted(rules.values(), key=lambda r: -len(words & set(json.dumps(r, ensure_ascii=False).lower().split())))
        hits = scored
    return hits[:k]


def slim(r: dict):
    return {k: r.get(k) for k in ("rule_key", "title", "summary", "requirements", "depends_on", "source_url",
                                   "source_tier", "last_verified", "freshness", "status", "confidence")}


def answer(case_id: str, question: str):
    case = db.table("cases").select("*").eq("id", case_id).single().execute().data
    hits = retrieve(case, question)
    data, _ = ask_json(ANSWER_SYSTEM, json.dumps({"case": case, "question": question,
                                                  "rules": [slim(r) for r in hits]}, ensure_ascii=False, default=str))
    log_run("retrieval", f"Answered: {question[:80]}", case_id=case_id,
            sources=[c["source_url"] for c in data.get("citations", [])])
    return data


def load_case(case_id: str):
    case = db.table("cases").select("*").eq("id", case_id).single().execute().data
    steps = db.table("steps").select("*").eq("case_id", case_id).order("position").execute().data
    ids = [s["id"] for s in steps]
    deps = db.table("step_dependencies").select("*").in_("step_id", ids).execute().data if ids else []
    return case, steps, deps


def shift_dependents(steps, deps, start_id: str, days: int):
    """Move a step and everything downstream of it by `days`."""
    by_id = {s["id"]: s for s in steps}
    children = {}
    for d in deps:
        children.setdefault(d["depends_on_step_id"], []).append(d["step_id"])
    moved, todo = [], [start_id]
    while todo:
        sid = todo.pop()
        if sid in moved:
            continue
        moved.append(sid)
        s = by_id[sid]
        if s["due_date"]:
            s["due_date"] = (date.fromisoformat(s["due_date"]) + timedelta(days=days)).isoformat()
        todo += children.get(sid, [])
    return [by_id[i] for i in moved]


def replan(case_id: str, cause: dict, delay: dict | None = None):
    """Planner agent. `delay` = {"rule_key": ..., "days": ...} shifts dates; then Claude picks the next action."""
    case, steps, deps = load_case(case_id)
    rules = case_rules(case)
    rule_by_key = {r["rule_key"]: r for r in rules}
    changed = []
    if delay:
        target = next(s for s in steps if s["rule_key"] == delay["rule_key"])
        changed = shift_dependents(steps, deps, target["id"], int(delay["days"]))
    start = date.fromisoformat(case["start_date"]) if case.get("start_date") else None
    for s in steps:
        r = rule_by_key.get(s["rule_key"])
        upd = {"updated_at": datetime.now(timezone.utc).isoformat()}
        if r:
            upd["source_url"] = r["source_url"]
            upd["last_verified"] = r["last_verified"]
        if s in changed or s["id"] in [c["id"] for c in changed]:
            upd["due_date"] = s["due_date"]
        late = start and s["due_date"] and s["status"] != "done" and date.fromisoformat(s["due_date"]) > start
        if late and s["status"] in ("blocked", "next", "in_progress"):
            upd["status"] = s["status"] = "at_risk"
        if cause.get("rule_key") and s["rule_key"] == cause["rule_key"] and s["status"] != "done":
            upd["description"] = f"Updated after rule change: {r['summary'] if r else 'see source'}"
        db.table("steps").update(upd).eq("id", s["id"]).execute()

    plan, _ = ask_json(PLANNER_SYSTEM, json.dumps({
        "case": case, "today": date.today().isoformat(), "cause": cause,
        "steps": [{k: s.get(k) for k in ("position", "rule_key", "title", "status", "due_date", "est_minutes")} for s in steps],
        "dependencies": [{"step": next(x["position"] for x in steps if x["id"] == d["step_id"]),
                          "needs": next(x["position"] for x in steps if x["id"] == d["depends_on_step_id"])} for d in deps],
        "rules": [slim(r) for r in rules],
    }, ensure_ascii=False, default=str))
    db.table("cases").update({"status": plan.get("risk", case["status"])}).eq("id", case_id).execute()
    srcs = sorted({r["source_url"] for r in rules if r["rule_key"] in {s["rule_key"] for s in steps}})
    log_run("planner", plan.get("explanation", "Re-planned case"), case_id=case_id, sources=srcs,
            metrics={"cause": cause, "next_best_action": plan.get("next_best_action"),
                     "risk": plan.get("risk"), "risk_reason": plan.get("risk_reason"),
                     "moved_steps": [c["position"] for c in changed], "plan": plan})
    db.table("events").insert({"type": "plan.updated", "case_id": case_id,
                               "payload": {"cause": cause, "plan": plan}}).execute()
    return plan


def watcher():
    """Watcher agent: consume unprocessed events and fan out to affected cases."""
    evs = db.table("events").select("*").is_("processed_at", "null").order("created_at").execute().data
    results = []
    for ev in evs:
        if ev["type"] == "rule.changed":
            steps = db.table("steps").select("case_id").eq("rule_key", ev["rule_key"]).neq("status", "done").execute().data
            case_ids = sorted({s["case_id"] for s in steps})
            log_run("watcher", f"rule.changed on {ev['rule_key']}: {len(case_ids)} affected case(s)",
                    metrics={"event_id": ev["id"], "cases": case_ids})
            for cid in case_ids:
                results.append(replan(cid, {"type": "rule.changed", "rule_key": ev["rule_key"],
                                            "event_id": ev["id"], **(ev.get("payload") or {})}))
        elif ev["type"] == "step.delayed":
            p = ev["payload"]
            log_run("orchestrator", f"step.delayed {p.get('rule_key')} by {p.get('days')} days, routed to planner",
                    case_id=ev["case_id"])
            results.append(replan(ev["case_id"], {"type": "step.delayed", **p}, delay=p))
        db.table("events").update({"processed_at": datetime.now(timezone.utc).isoformat()}).eq("id", ev["id"]).execute()
    return results
