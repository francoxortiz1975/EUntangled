"""Generate the demo voiceover with ElevenLabs. Needs ELEVENLABS_API_KEY in .env.
Usage: .venv/bin/python voiceover.py   -> writes voiceover/01_intro.mp3 ... and voiceover/full.mp3
"""
import os
import pathlib

import httpx
from dotenv import load_dotenv

load_dotenv()
KEY = os.environ["ELEVENLABS_API_KEY"]
VOICE = os.getenv("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")  # George, works on the free plan

SCRIPT = [
    ("01_intro", "Moving to Paris as a non-EU employee means more than ten admin steps that depend on each other, and the rules keep changing. EUntangled turns that into a personal, cited plan that re-plans itself."),
    ("02_plan", "This is Ana, moving from Quito to Paris, starting on the twelfth of November. Her next action has a date and tells her what it unblocks. Every step shows its official source, when it was last verified, and how confident the agent is."),
    ("03_delay", "Her prefecture appointment just slipped by two weeks. The Orchestrator routes the event to the Planner, which shifts every dependent step, flags what is now at risk, and picks a new next action. Nobody had to email HR."),
    ("04_rule", "Now an official page changes. The pipeline detects the new content hash, the Extractor and Verifier rebuild the rule from the exact official quote, and a new rule version fires a rule changed event. The Watcher finds every affected case and re-plans them in parallel."),
    ("05_eugene", "Ana can also ask EUgenia, our relocation agent, anything about her move. Every answer is grounded in verified official sources, with a link and a verification date. If a rule is not covered, EUgenia says to confirm with a specialist."),
    ("06_manager", "The mobility team sees every relocation at once: who is at risk, why, and which rules need a human review."),
    ("07_evidence", "Same model, same twenty hard questions. Alone, it was right forty-five percent of the time, and only half its answers had a working official source. With EUntangled, seventy-five percent correct, and ninety-five percent verifiable."),
    ("08_close", "Localyze proved companies pay for relocation. EUntangled brings adaptive, cited plans at a price mid-size firms can afford. EUntangled. Untangle your move."),
]

out = pathlib.Path("voiceover")
out.mkdir(exist_ok=True)
full = b""
for name, text in SCRIPT:
    r = httpx.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}",
        headers={"xi-api-key": KEY, "accept": "audio/mpeg"},
        json={"text": text, "model_id": "eleven_multilingual_v2",
              "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}},
        timeout=120,
    )
    r.raise_for_status()
    (out / f"{name}.mp3").write_bytes(r.content)
    full += r.content
    print("wrote", name)
(out / "full.mp3").write_bytes(full)
print("wrote voiceover/full.mp3")
