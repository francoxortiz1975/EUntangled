import json
import os
import re

from anthropic import Anthropic
from dotenv import load_dotenv
from openai import OpenAI
from supabase import create_client

load_dotenv()

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")

db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
claude = Anthropic()
_openai = OpenAI() if os.getenv("OPENAI_API_KEY") else None


def ask_json(system: str, user: str, max_tokens: int = 4000):
    """One Claude call that must return JSON. Returns (parsed, usage)."""
    msg = claude.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system + "\nReply with JSON only, no prose, no code fences.",
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    usage = {"input": msg.usage.input_tokens, "output": msg.usage.output_tokens}
    return json.loads(text), usage


def embed(texts: list[str]) -> list[list[float]] | None:
    if not _openai or not texts:
        return None
    r = _openai.embeddings.create(model="text-embedding-3-small", input=texts)
    return [d.embedding for d in r.data]


def log_run(agent: str, action: str, case_id=None, sources=None, metrics=None, mode="live"):
    db.table("agent_runs").insert({
        "agent": agent, "action": action, "case_id": case_id,
        "sources": sources or [], "metrics": metrics or {}, "mode": mode,
    }).execute()
