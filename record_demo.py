"""Record the demo automatically: Chrome clicks through the app, then each section is fitted to its voiceover clip.
Needs: API on :8000, site on :5500 (python3 -m http.server 5500 -d docs), voiceover/*.mp3, ffmpeg.
Usage: .venv/bin/python record_demo.py  ->  demo/EUntangled_demo.mp4
"""
import glob
import pathlib
import shutil
import subprocess
import time

from playwright.sync_api import sync_playwright

URL = "http://localhost:5500"
OUT = pathlib.Path("demo")
shutil.rmtree(OUT, ignore_errors=True)
OUT.mkdir()
clips = sorted(glob.glob("voiceover/0*.mp3"))
dur = [float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", c])) for c in clips]

marks = []  # (section start time in seconds since recording began)

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, record_video_dir=str(OUT), record_video_size={"width": 1440, "height": 900})
    page = ctx.new_page()
    t0 = time.time()
    page.goto(URL)
    page.wait_for_function("document.getElementById('conn').innerText.includes('live')", timeout=30000)
    page.wait_for_timeout(1500)

    def section(i, action):
        marks.append(time.time() - t0)
        start = time.time()
        action()
        rest = dur[i] - (time.time() - start)
        if rest > 0:
            page.wait_for_timeout(int(rest * 1000))

    def smooth(y):
        page.evaluate(f"window.scrollTo({{top:{y},behavior:'smooth'}})")

    def wait_idle():
        page.wait_for_function("document.getElementById('busy').classList.contains('hide')", timeout=240000)

    # 1 intro: hero
    section(0, lambda: page.wait_for_timeout(500))
    # 2 plan + journey + why this step
    def plan():
        smooth(520); page.wait_for_timeout(2500)
        page.locator(".node").nth(5).click(); page.wait_for_timeout(2500)
        page.locator(".node").nth(4).click(); page.wait_for_timeout(1500)
    section(1, plan)
    # 3 delay
    def delay():
        smooth(0); page.wait_for_timeout(800)
        page.click("#b-delay"); wait_idle(); page.wait_for_timeout(800)
    section(2, delay)
    # 4 rule change (long; will be sped up to fit the clip)
    def rule():
        page.click("#b-rule"); wait_idle(); page.wait_for_timeout(1500)
    section(3, rule)
    # 5 EUgene
    def eugene():
        page.locator("#sugg button").first.click()
        page.wait_for_function("!document.querySelector('#msgs .spin')", timeout=120000)
        page.wait_for_timeout(1500)
    section(4, eugene)
    # 6 manager
    section(5, lambda: (page.click(".tab[data-v=mgr]"), page.wait_for_timeout(1500)))
    # 7 evidence
    section(6, lambda: (page.click(".tab[data-v=ev]"), page.wait_for_timeout(800)))
    # 8 close
    section(7, lambda: (page.click(".tab[data-v=emp]"), smooth(0), page.wait_for_timeout(800)))
    marks.append(time.time() - t0)
    ctx.close()
    browser.close()

raw = glob.glob(str(OUT / "*.webm"))[0]
parts = []
for i, c in enumerate(clips):
    a, b = marks[i], marks[i + 1]
    speed = (b - a) / dur[i]  # >1 means the action took longer than its narration: speed it up
    part = OUT / f"part{i}.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{a}", "-t", f"{b - a}", "-i", raw, "-i", c,
                    "-filter:v", f"setpts=PTS/{max(speed, 0.01)},fps=30", "-map", "0:v", "-map", "1:a",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(part)], check=True)
    parts.append(part)
(OUT / "list.txt").write_text("".join(f"file '{pp.name}'\n" for pp in parts))
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(OUT / "list.txt"), "-c", "copy",
                str(OUT / "EUntangled_demo.mp4")], check=True)
print("marks", [round(m, 1) for m in marks])
print("wrote", OUT / "EUntangled_demo.mp4")
