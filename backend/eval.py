"""Eval: same model, same questions, no data vs grounded in the corpus.

python -m backend.eval build   # writes data/eval_questions.json from verified rule quotes (review by hand!)
python -m backend.eval run     # writes data/eval_results.json
"""
import json
import sys

import httpx

from backend import agents
from backend.common import ask_json, db

Q_PATH, R_PATH = "data/eval_questions.json", "data/eval_results.json"

BUILD_SYSTEM = """From the official rule records given (one category), write HARD, tangled questions a non-EU salaried
employee moving to Paris would really ask: each combines a personal situation with two or more conditions, deadlines,
amounts or exceptions, so a generic answer gets it wrong. Each must still be fully answerable from the records' quotes.
Output: {"questions": [{"question": str (English), "reference_answer": str, "rule_keys": [str], "source_url": str,
"quotes": [str]}]}"""

BASELINE_SYSTEM = """Answer the relocation question for a non-EU salaried employee moving to Paris. Cite the official source
URL you rely on and the date you believe it was valid.
Output: {"answer": str, "citations": [{"source_url": str, "last_verified": str}]}"""

JUDGE_SYSTEM = """Grade an answer against a reference answer from an official French source. "correct" means it states the
reference's key fact (number, deadline, document, condition) without contradicting it.
Output: {"correct": bool, "reason": short}"""


CATEGORIES = {"residence_permit": "Visa & permits", "housing": "Housing", "bank": "Bank", "social_security": "Health & social security"}


def build(per_cat=5):
    qs = []
    for cat, label in CATEGORIES.items():
        rules = (db.table("rules").select("rule_key,title,summary,requirements,evidence,source_url")
                 .eq("category", cat).neq("status", "superseded").execute().data)
        data, _ = ask_json(BUILD_SYSTEM, json.dumps({"n": per_cat, "category": label, "rules": rules}, ensure_ascii=False))
        qs += [{**q, "category": label} for q in data["questions"][:per_cat]]
    json.dump(qs, open(Q_PATH, "w"), ensure_ascii=False, indent=2)
    print(f"wrote {len(qs)} questions to {Q_PATH}; check each reference_answer against its quote")


def run():
    qs = json.load(open(Q_PATH))
    case = db.table("cases").select("id").eq("employee_name", "Ana Torres").limit(1).execute().data[0]
    corpus = {s["url"] for s in db.table("sources").select("url").execute().data}
    rows = []
    for q in qs:
        try:
            row = score(q, case, corpus)
        except Exception as e:
            print("skip", q["question"][:50], e)
            continue
        rows.append(row)
        print(f"{row['no_data']['correct']!s:5} {row['grounded']['correct']!s:5} {q['question'][:70]}")
    summary = {m: {"accuracy": round(sum(r[m]["correct"] for r in rows) / len(rows), 3),
                   "valid_source": round(sum(r[m]["valid_source"] for r in rows) / len(rows), 3)}
               for m in ("no_data", "grounded")}
    summary["n"] = len(rows)
    summary["by_category"] = {}
    for cat in sorted({r["category"] for r in rows}):
        rs = [r for r in rows if r["category"] == cat]
        summary["by_category"][cat] = {m: {"correct": sum(r[m]["correct"] for r in rs),
                                           "working_link": sum(r[m]["working_link"] for r in rs), "of": len(rs)}
                                       for m in ("no_data", "grounded")}
    for m in ("no_data", "grounded"):
        summary[m]["working_link"] = round(sum(r[m]["working_link"] for r in rows) / len(rows), 3)
    json.dump({"summary": summary, "rows": rows, "mode": "recorded"}, open(R_PATH, "w"), ensure_ascii=False, indent=2)
    print(json.dumps(summary, indent=2))


def score(q, case, corpus):
    base, _ = ask_json(BASELINE_SYSTEM, q["question"])
    grounded = agents.answer(case["id"], q["question"])
    row = {"question": q["question"], "category": q.get("category", "General")}
    if True:
        for name, ans in (("no_data", base), ("grounded", grounded)):
            verdict, _ = ask_json(JUDGE_SYSTEM, json.dumps({"question": q["question"], "reference": q["reference_answer"],
                                                           "answer": ans.get("answer")}, ensure_ascii=False))
            cites = [c.get("source_url", "") for c in ans.get("citations", [])]
            row[name] = {"answer": ans.get("answer"), "correct": verdict["correct"],
                         "valid_source": any(c.rstrip("/") in {u.rstrip("/") for u in corpus} for c in cites),
                         "working_link": any(link_ok(c) for c in cites[:3]), "citations": cites}
    return row


def link_ok(url: str) -> bool:
    try:
        return httpx.get(url, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True, timeout=12).status_code == 200
    except Exception:
        return False


if __name__ == "__main__":
    build() if sys.argv[1:] == ["build"] else run()
