"""Eval: same model, same questions, no data vs grounded in the corpus.

python -m backend.eval build   # writes data/eval_questions.json from verified rule quotes (review by hand!)
python -m backend.eval run     # writes data/eval_results.json
"""
import json
import sys

from backend import agents
from backend.common import ask_json, db

Q_PATH, R_PATH = "data/eval_questions.json", "data/eval_results.json"

BUILD_SYSTEM = """From the official rule records given, write evaluation questions a non-EU salaried employee moving to Paris
would ask. Each must be answerable from ONE record's quote. Output:
{"questions": [{"question": str (English), "reference_answer": str, "rule_key": str, "source_url": str, "quote": str}]}"""

BASELINE_SYSTEM = """Answer the relocation question for a non-EU salaried employee moving to Paris. Cite the official source
URL you rely on and the date you believe it was valid.
Output: {"answer": str, "citations": [{"source_url": str, "last_verified": str}]}"""

JUDGE_SYSTEM = """Grade an answer against a reference answer from an official French source. "correct" means it states the
reference's key fact (number, deadline, document, condition) without contradicting it.
Output: {"correct": bool, "reason": short}"""


def build(n=15):
    rules = db.table("rules").select("rule_key,title,requirements,evidence,source_url").neq("status", "superseded").execute().data
    data, _ = ask_json(BUILD_SYSTEM, json.dumps({"n": n, "rules": rules}, ensure_ascii=False))
    qs = data["questions"][:n]
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
    json.dump({"summary": summary, "rows": rows, "mode": "recorded"}, open(R_PATH, "w"), ensure_ascii=False, indent=2)
    print(json.dumps(summary, indent=2))


def score(q, case, corpus):
    base, _ = ask_json(BASELINE_SYSTEM, q["question"])
    grounded = agents.answer(case["id"], q["question"])
    row = {"question": q["question"]}
    if True:
        for name, ans in (("no_data", base), ("grounded", grounded)):
            verdict, _ = ask_json(JUDGE_SYSTEM, json.dumps({"question": q["question"], "reference": q["reference_answer"],
                                                           "answer": ans.get("answer")}, ensure_ascii=False))
            cites = [c.get("source_url", "") for c in ans.get("citations", [])]
            row[name] = {"answer": ans.get("answer"), "correct": verdict["correct"],
                         "valid_source": any(c.rstrip("/") in {u.rstrip("/") for u in corpus} for c in cites),
                         "citations": cites}
    return row


if __name__ == "__main__":
    build() if sys.argv[1:] == ["build"] else run()
