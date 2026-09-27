"""~90 second highlights video of the LIVE stack, with captions in English or Urdu (or both files).

    .\\.venv\\Scripts\\python.exe scripts/make_short_video.py --lang both --reset
    .\\.venv\\Scripts\\python.exe scripts/make_short_video.py --lang ur          # only Urdu
Outputs (the long video docs/video/bankai-demo.mp4 is never touched):
    docs/video/bankai-demo-90s-en.mp4      docs/video/bankai-demo-90s-ur.mp4
Waiting for the real AI model is sped up 4x in post-processing (the caption says so), everything else is real time.
--reset runs `docker compose down -v && up -d` before each recording so balances start clean.
"""
import argparse
import pathlib
import re
import shutil
import subprocess
import sys
import time

import httpx
import imageio_ffmpeg
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from make_demo_video import OVERLAY_JS, Demo, warm_up  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEND_READY = "() => { const s = document.getElementById('send'); return s && !s.disabled; }"

T = {
    "en": {
        "dir": "ltr",
        "title": "Bank AI Assistant",
        "title_lines": ["4 specialist AI agents, one coordinator", "Security is enforced by the tools, not the AI",
                        "Big transfers need a human", "Everything is logged and replayable"],
        "thinking": "AI is thinking... (sped up)",
        "s1": "Alice asks in plain language. A coordinator picks a specialist agent.",
        "p_route": "The coordinator chose the ACCOUNTS agent, which used a secure tool",
        "s2": "Now Alice asks about someone else's account",
        "p_refused": "REFUSED by the tool layer. The AI cannot override it.",
        "s3": "A $1,400 transfer, typed fast with a typo",
        "p_confirm": "The confirmation is written by CODE, not the AI",
        "s3b": "Alice confirms with a real click",
        "p_wait": "Over the limit: PAUSED for a human. No money has moved.",
        "s4": "Staff console: Sam, a bank employee",
        "p_card": "The paused transfer and the reason",
        "p_audit": "Tamper-evident audit log: verified",
        "s5": "Sam approves. Only now does the money move.",
        "p_note": "The customer is notified automatically",
        "s6": "Defence against hackers: a hidden instruction inside a transaction",
        "p_inj": "DETECTED and removed before the AI could read it",
        "end": "What you saw",
        "end_lines": ["Understands typos (local AI model)", "Tools enforce security, not the AI",
                      "Humans approve risky transfers", "Full audit trail and replay"],
    },
    "ur": {
        "dir": "rtl",
        "title": "بینک اے آئی اسسٹنٹ",
        "title_lines": ["چار ماہر اے آئی ایجنٹس اور ایک کوآرڈینیٹر", "سکیورٹی ٹولز نافذ کرتے ہیں، اے آئی نہیں",
                        "بڑی رقم کی منظوری انسان دیتا ہے", "ہر کام کا ریکارڈ اور ری پلے موجود ہے"],
        "thinking": "اے آئی سوچ رہا ہے... (تیز رفتار)",
        "s1": "ایلس عام زبان میں سوال پوچھتی ہے۔ کوآرڈینیٹر ماہر ایجنٹ چنتا ہے۔",
        "p_route": "کوآرڈینیٹر نے اکاؤنٹس ایجنٹ چنا، جس نے محفوظ ٹول چلایا",
        "s2": "اب ایلس کسی اور کے اکاؤنٹ کے بارے میں پوچھتی ہے",
        "p_refused": "ٹول لیئر نے انکار کر دیا۔ اے آئی اسے بدل نہیں سکتا۔",
        "s3": "1,400 ڈالر کی ٹرانسفر، جلدی میں غلط ہجے کے ساتھ",
        "p_confirm": "تصدیق کا متن کوڈ لکھتا ہے، اے آئی نہیں",
        "s3b": "ایلس اصلی کلک سے تصدیق کرتی ہے",
        "p_wait": "حد سے زیادہ رقم: انسان کے انتظار میں رکی ہے۔ رقم ابھی نہیں گئی۔",
        "s4": "سٹاف کنسول: سام، بینک کا ملازم",
        "p_card": "رکی ہوئی ٹرانسفر اور اس کی وجہ",
        "p_audit": "چھیڑ چھاڑ سے محفوظ آڈٹ لاگ: تصدیق شدہ",
        "s5": "سام منظور کرتا ہے۔ اب ہی رقم جاتی ہے۔",
        "p_note": "کسٹمر کو خود بخود اطلاع مل جاتی ہے",
        "s6": "ہیکرز سے حفاظت: لین دین میں چھپی ہوئی ہدایت",
        "p_inj": "اے آئی کے پڑھنے سے پہلے ہی پکڑی گئی اور ہٹا دی گئی",
        "end": "آپ نے کیا دیکھا",
        "end_lines": ["غلط ہجے بھی سمجھتا ہے (مقامی اے آئی ماڈل)", "سکیورٹی ٹولز نافذ کرتے ہیں",
                      "خطرناک ٹرانسفر انسان منظور کرتا ہے", "مکمل آڈٹ ٹریل اور ری پلے"],
    },
}


class Short(Demo):
    def __init__(self, page, t0, tx):
        super().__init__(page, 6)
        self.t0, self.tx, self.fast = t0, tx, []

    def now(self):
        return time.time() - self.t0

    def click(self, locator):  # quicker than the full-length video
        loc = locator.first
        loc.scroll_into_view_if_needed()
        b = loc.bounding_box()
        self.page.evaluate("([x,y]) => window.__demo.cursor(x,y)", [b["x"] + b["width"] / 2, b["y"] + b["height"] / 2])
        self.page.wait_for_timeout(650)
        loc.click()

    def ask(self, text):
        p = self.page
        self.click(p.locator("#msg"))
        p.keyboard.type(text, delay=32)
        p.wait_for_timeout(200)
        self.caption(self.tx["thinking"], None, 0)
        p.keyboard.press("Enter")
        start = self.now()
        p.wait_for_timeout(500)
        p.wait_for_function(SEND_READY, timeout=240000)
        self.fast.append((start + 0.7, self.now() - 0.2))  # the waiting part is sped up afterwards
        p.wait_for_timeout(250)
        return p.locator("#log .msg.bot").last

    def wait_done(self):
        start = self.now()
        self.page.wait_for_function(SEND_READY, timeout=240000)
        self.fast.append((start + 0.5, self.now() - 0.2))


def scenes(d: Short, page, base):
    tx = d.tx
    page.goto(base + "/")
    page.wait_for_timeout(500)
    d.card(tx["title"], tx["title_lines"], 4.5)

    d.caption(tx["s1"], "1/6", 0)
    d.login("alice")
    bot = d.ask("What's my balance?")
    d.caption(tx["s1"], "1/6", 0)
    d.point(bot.locator(".chip", has_text="route:"), tx["p_route"], "below", 3.4)
    d.clear()

    d.caption(tx["s2"], "2/6", 0)
    bot = d.ask("What is the balance of account 100200300501?")
    d.caption(tx["s2"], "2/6", 0)
    d.point(bot, tx["p_refused"], "right", 3.4)
    d.clear()

    d.caption(tx["s3"], "3/6", 0)
    page.click("#login")  # fresh conversation
    page.wait_for_timeout(500)
    bot = d.ask("trasnfer 1400$ to bob from savings")
    d.caption(tx["s3"], "3/6", 0)
    d.point(bot.locator(".confirm button").first, tx["p_confirm"], "above", 3.6)
    d.clear()
    d.caption(tx["s3b"], "3/6", 0)
    d.click(bot.locator(".confirm button").first)
    d.wait_done()
    bot = page.locator("#log .msg.bot").last
    d.caption(tx["s3b"], "3/6", 0)
    d.point(bot.locator(".chip", has_text="awaiting staff approval"), tx["p_wait"], "above", 3.8)
    d.clear()

    d.caption(tx["s4"], "4/6", 0)
    page.goto(base + "/staff")
    page.wait_for_timeout(400)
    d.click(page.locator("#login"))
    page.wait_for_selector("#list .card", timeout=30000)
    page.wait_for_timeout(400)
    d.point(page.locator("#list .card").first, tx["p_card"], "below", 3.2)
    d.clear()
    d.click(page.locator("#audit"))
    page.wait_for_function("() => document.getElementById('status').textContent.includes('OK')", timeout=30000)
    d.point(page.locator("#status"), tx["p_audit"], "above", 3.0)
    d.clear()
    page.evaluate("() => window.scrollTo(0, 0)")
    d.caption(tx["s5"], "5/6", 0)
    d.click(page.locator("#list .card button").first)
    page.wait_for_timeout(2200)

    page.goto(base + "/")
    page.wait_for_timeout(400)
    d.login("alice")
    page.wait_for_selector(".msg.note", timeout=30000)
    page.wait_for_timeout(300)
    d.point(page.locator(".msg.note").first, tx["p_note"], "below", 3.0)
    d.clear()

    d.caption(tx["s6"], "6/6", 0)
    bot = d.ask("Show my recent transactions")
    d.caption(tx["s6"], "6/6", 0)
    d.point(bot.locator(".chip", has_text="injection_suspected"), tx["p_inj"], "below", 4.2)
    d.clear()
    d.caption("", None, 0)
    d.card(tx["end"], tx["end_lines"], 5)


def duration_of(path):
    out = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-i", str(path)], capture_output=True, text=True).stderr
    h, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out).groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def speed_up(src, dst, fast, speed=4.0):
    total = duration_of(src)
    segs, cur = [], 0.0
    for a, b in sorted(fast):
        a, b = max(a, cur), min(b, total)
        if b - a < 1.0:
            continue
        segs += [(cur, a, 1.0), (a, b, speed)]
        cur = b
    segs.append((cur, total, 1.0))
    segs = [s for s in segs if s[1] - s[0] > 0.05]
    parts = [f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=(PTS-STARTPTS)/{sp}[v{i}]" for i, (a, b, sp) in enumerate(segs)]
    graph = ";".join(parts) + ";" + "".join(f"[v{i}]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=0[out]"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(src), "-filter_complex", graph, "-map", "[out]",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "22", "-preset", "medium", "-movflags", "+faststart", str(dst)], check=True)


def reset_stack(base):
    env_port = base.rsplit(":", 1)[-1].split("/")[0]
    env = {**__import__("os").environ, "API_PORT": env_port}
    subprocess.run(["docker", "compose", "down", "-v"], cwd=ROOT, env=env, check=True, capture_output=True)
    subprocess.run(["docker", "compose", "up", "-d"], cwd=ROOT, env=env, check=True, capture_output=True)
    for _ in range(60):
        try:
            if httpx.get(base + "/readyz", timeout=3).json().get("status") == "ok":
                time.sleep(3)
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3)
    raise SystemExit("stack did not become ready after reset")


def record(base, outdir, lang):
    tx = T[lang]
    tmp = outdir / f"_tmp_{lang}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    warm_up(base)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1024, "height": 576}, record_video_dir=str(tmp),
                                  record_video_size={"width": 1280, "height": 720}, bypass_csp=True)
        ctx.add_init_script(f"window.__demoDir = '{tx['dir']}';")
        ctx.add_init_script(OVERLAY_JS)
        page = ctx.new_page()
        t0 = time.time()
        d = Short(page, t0, tx)
        try:
            scenes(d, page, base)
        except Exception as e:  # noqa: BLE001
            print("SCENE FAILED:", type(e).__name__, str(e)[:200])
        vid = page.video
        ctx.close()
        webm = pathlib.Path(vid.path())
        browser.close()
    dst = outdir / f"bankai-demo-90s-{lang}.mp4"
    speed_up(webm, dst, d.fast)
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"[{lang}] written: {dst}  duration {duration_of(dst):.0f}s  ({dst.stat().st_size / 1e6:.1f} MB)")
    return dst


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:18080")
    ap.add_argument("--lang", choices=["en", "ur", "both"], default="both")
    ap.add_argument("--reset", action="store_true", help="reset demo data (docker compose down -v / up -d) before each recording")
    ap.add_argument("--out", default=str(ROOT / "docs" / "video"))
    a = ap.parse_args()
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for lang in (["en", "ur"] if a.lang == "both" else [a.lang]):
        if a.reset:
            reset_stack(a.base)
        record(a.base, out, lang)
