"""Knowledge pipeline: fetch -> hash -> parse -> Extractor -> Verifier -> Normalizer -> Resolver.

Usage:
  python -m backend.pipeline load-sources      # upsert data/sources.csv into `sources`
  python -m backend.pipeline run [--limit N]   # fetch every active source and ingest changes
"""
import csv
import hashlib
import io
import json
import sys
import time
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

from backend.common import MODEL, ask_json, db, embed, log_run

PROMPT_VERSION = "v1"
UA = "EUntangledBot/0.1 (hackathon research; contact via github)"
MAX_CHUNKS_PER_SOURCE = 3
CHUNK_CHARS = 3500

# Keys shared with case steps, so rules link to the plan.
RULE_KEYS = [
    "employment_contract", "long_stay_visa", "visa_validation", "prefecture_appt",
    "residence_permit", "proof_of_address", "housing_guarantee", "bank_account",
    "social_security", "navigo_pass", "tax_account",
]

EXTRACTOR_SYSTEM = f"""You are the Extractor in a pipeline that turns official French administrative pages into rule records
for people relocating to Paris. Read ONE passage and return the rules it states.
Output: {{"rules": [ {{
  "rule_key": one of {RULE_KEYS} or a new snake_case key,
  "category": "residence_permit|visa|housing|bank|social_security|tax|transport",
  "title": short English title,
  "summary": 1-2 English sentences,
  "requirements": [ {{"document": str|null, "condition": str|null, "deadline": str|null, "fee": str|null,
                      "quote": exact verbatim sentence from the passage supporting this requirement}} ],
  "applies_to": {{"nationality_group": "non_eu|eu|any", "permit_type": "salarie|student|talent|any", "city": "Paris|any"}},
  "depends_on": [rule_keys that must be done first],
  "evidence": {{"quote": the single most important verbatim quote}}
}} ]}}
Rules: only state what the passage says. Quotes must be copied exactly, in the original language. If the passage holds no actionable rule, return {{"rules": []}}."""

VERIFIER_SYSTEM = """You are the Verifier. You see ONE draft rule record and the source passage, nothing else.
For each requirement (by index) decide if the passage supports it: the quote must appear in the passage and the fields must
follow from it. Also check the summary.
Output: {"requirements": [{"index": int, "supported": bool, "reason": short}], "summary_supported": bool, "confidence": 0.0-1.0}"""


def now():
    return datetime.now(timezone.utc).isoformat()


def load_sources(path="data/sources.csv"):
    with open(path, newline="") as f:
        rows = [dict(r, tier=int(r["tier"])) for r in csv.DictReader(f)]
    db.table("sources").upsert(rows, on_conflict="url").execute()
    print(f"loaded {len(rows)} sources")


def fetch(url: str):
    r = httpx.get(url, headers={"User-Agent": UA}, follow_redirects=True, timeout=30)
    return r.status_code, r.content, r.headers.get("content-type", "")


def to_text(content: bytes, ctype: str) -> str:
    if "pdf" in ctype:
        reader = PdfReader(io.BytesIO(content))
        return "\n".join(f"[page {i+1}]\n{p.extract_text() or ''}" for i, p in enumerate(reader.pages))
    soup = BeautifulSoup(content, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "svg"]):
        tag.decompose()
    main = soup.find("main") or soup.body or soup
    lines = [l.strip() for l in main.get_text("\n").splitlines()]
    return "\n".join(l for l in lines if l)


def chunk(text: str):
    paras, buf, out = text.split("\n"), "", []
    for p in paras:
        if len(buf) + len(p) > CHUNK_CHARS and buf:
            out.append(buf)
            buf = ""
        buf += p + "\n"
    if buf.strip():
        out.append(buf)
    return out[:MAX_CHUNKS_PER_SOURCE]


def extract(passage: str):
    data, usage = ask_json(EXTRACTOR_SYSTEM, passage)
    return data.get("rules", []), usage


def verify(record: dict, passage: str):
    data, usage = ask_json(VERIFIER_SYSTEM, json.dumps({"record": record, "passage": passage}, ensure_ascii=False))
    keep = {r["index"] for r in data.get("requirements", []) if r.get("supported")}
    total = len(record.get("requirements", []))
    record["requirements"] = [q for i, q in enumerate(record.get("requirements", [])) if i in keep]
    if not data.get("summary_supported", True):
        record["summary"] = None
    checks = total + 1
    passed = len(keep) + (1 if data.get("summary_supported", True) else 0)
    return record, float(data.get("confidence", 0.5)), {"checked": checks, "passed": passed}, usage


def normalize(record: dict):
    a = record.get("applies_to") or {}
    record["applies_to"] = {k: v for k, v in a.items() if v and v != "any"}
    record["rule_key"] = (record.get("rule_key") or "misc").strip().lower().replace(" ", "_")
    record["depends_on"] = [d for d in record.get("depends_on", []) if d != record["rule_key"]]
    return record


def resolve(record: dict, confidence: float, source: dict, content_hash: str, trace: dict):
    """Compare with the stored rule; insert a new version (fires rule.changed) only when content changed."""
    if not record["requirements"]:
        return "dropped"
    existing = (db.table("rules").select("*").eq("rule_key", record["rule_key"])
                .neq("status", "superseded").order("version", desc=True).limit(1).execute().data)
    row = {
        "rule_key": record["rule_key"], "category": record.get("category"), "title": record.get("title") or record["rule_key"],
        "summary": record.get("summary"), "requirements": record["requirements"], "applies_to": record["applies_to"],
        "depends_on": record["depends_on"], "evidence": record.get("evidence"), "source_id": source["id"],
        "source_url": source["url"], "source_tier": source["tier"], "content_hash": content_hash,
        "confidence": round(confidence, 2), "fetched_at": now(), "last_checked": now(), "last_verified": now(),
        "trace": trace, "status": "active",
    }
    if confidence < 0.6:
        db.table("review_queue").insert({"rule_key": row["rule_key"], "reason": "low_confidence",
                                         "details": {"confidence": confidence, "source": source["url"]}}).execute()
    if not existing:
        db.table("rules").insert(row).execute()
        return "new"
    old = existing[0]
    if old["source_id"] == source["id"] and old["content_hash"] == content_hash:
        # Same snapshot, another chunk: merge requirements instead of versioning.
        merged = old["requirements"] + [q for q in row["requirements"] if q not in old["requirements"]]
        db.table("rules").update({"requirements": merged}).eq("id", old["id"]).execute()
        return "merged"
    if json.dumps(old["requirements"], sort_keys=True) == json.dumps(row["requirements"], sort_keys=True):
        db.table("rules").update({"last_checked": now(), "last_verified": now()}).eq("id", old["id"]).execute()
        return "unchanged"
    if old["source_id"] != source["id"] and (old["source_tier"] or 3) < source["tier"]:
        # A lower-tier source disagrees with a higher-tier one: keep the official rule, flag the conflict.
        db.table("review_queue").insert({"rule_key": row["rule_key"], "reason": "conflict",
            "details": {"kept": old["source_url"], "kept_tier": old["source_tier"],
                        "other": source["url"], "other_tier": source["tier"],
                        "other_requirements": row["requirements"]}}).execute()
        db.table("rules").update({"status": "conflicting"}).eq("id", old["id"]).execute()
        return "conflict"
    row["version"] = old["version"] + 1
    db.table("rules").insert(row).execute()  # trigger supersedes old version and emits rule.changed
    return "changed"


def ingest_text(source: dict, text: str, content_hash: str):
    stats = {"rules": 0, "checked": 0, "passed": 0, "outcomes": {}}
    for passage in chunk(text):
        records, u1 = extract(passage)
        for rec in records:
            rec, conf, chk, u2 = verify(rec, passage)
            rec = normalize(rec)
            trace = {"model": MODEL, "prompt_version": PROMPT_VERSION,
                     "tokens": u1["input"] + u1["output"] + u2["input"] + u2["output"]}
            outcome = resolve(rec, conf, source, content_hash, trace)
            stats["rules"] += 1
            stats["checked"] += chk["checked"]
            stats["passed"] += chk["passed"]
            stats["outcomes"][outcome] = stats["outcomes"].get(outcome, 0) + 1
            if outcome in ("new", "changed"):
                rule = (db.table("rules").select("id").eq("rule_key", rec["rule_key"])
                        .order("version", desc=True).limit(1).execute().data[0])
                texts = [rec.get("summary") or rec["title"]] + [q.get("quote", "") for q in rec["requirements"]]
                vecs = embed(texts)
                db.table("rule_chunks").insert([
                    {"rule_id": rule["id"], "chunk_text": t, "embedding": vecs[i] if vecs else None}
                    for i, t in enumerate(texts) if t]).execute()
    stats["verifier_pass_rate"] = round(stats["passed"] / stats["checked"], 3) if stats["checked"] else None
    log_run("verifier", f"Verified {stats['rules']} rules from {source['url']}",
            sources=[source["url"]], metrics=stats)
    return stats


def run_source(source: dict, override_text: str | None = None):
    if override_text is None:
        status, content, ctype = fetch(source["url"])
        if status >= 400:
            print(f"  {status} {source['url']}")
            return {"status": status}
        text = to_text(content, ctype)
    else:
        status, text = 200, override_text
    h = hashlib.sha256(text.encode()).hexdigest()
    last = (db.table("source_snapshots").select("content_hash").eq("source_id", source["id"])
            .order("fetched_at", desc=True).limit(1).execute().data)
    if last and last[0]["content_hash"] == h:
        db.table("rules").update({"last_checked": now()}).eq("source_id", source["id"]).neq("status", "superseded").execute()
        print(f"  unchanged {source['url']}")
        return {"status": status, "changed": False}
    db.table("source_snapshots").upsert({"source_id": source["id"], "http_status": status, "content_hash": h,
                                         "clean_text": text}, on_conflict="source_id,content_hash").execute()
    stats = ingest_text(source, text, h)
    print(f"  ingested {source['url']}: {stats}")
    return {"status": status, "changed": True, **stats}


def run(limit: int | None = None):
    sources = db.table("sources").select("*").eq("active", True).execute().data
    for s in sources[:limit]:
        try:
            run_source(s)
        except Exception as e:  # keep going: one broken page must not stop the crawl
            print(f"  error {s['url']}: {e}")
        time.sleep(1)  # rate limit


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "load-sources":
        load_sources()
    else:
        lim = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
        run(lim)
