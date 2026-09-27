"""Take 6 clean screenshots (no overlays) of the LIVE app and save them next to the videos in docs/video/.

    .\\.venv\\Scripts\\python.exe scripts/take_screenshots.py --base http://localhost:18080
Reset the demo data first for clean numbers. Output: docs/video/screenshot-01-*.png ... screenshot-06-*.png
"""
import argparse
import pathlib

import httpx
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEND_READY = "() => { const s = document.getElementById('send'); return s && !s.disabled; }"


def main(base: str, out: pathlib.Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    c = httpx.Client(base_url=base, timeout=240)
    h = {"authorization": "Bearer " + c.post("/auth/dev-token", json={"username": "alice"}).json()["token"]}
    for m in ("What's my balance?", "What is the current mortgage rate?"):  # warm the model
        c.post("/chat", json={"message": m}, headers=h)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=1.5)
        page = ctx.new_page()

        def ask(text):
            page.fill("#msg", text)
            page.press("#msg", "Enter")
            page.wait_for_timeout(600)
            page.wait_for_function(SEND_READY, timeout=240000)
            page.wait_for_timeout(500)

        def shot(name):
            page.wait_for_timeout(500)
            path = out / name
            page.screenshot(path=str(path))
            print("saved", path.name)

        def customer(user="alice"):
            page.goto(base + "/")
            page.select_option("#user", user)
            page.click("#login")
            page.wait_for_selector("#who:has-text('signed in')")

        customer()
        ask("What's my balance?")
        shot("screenshot-01-customer-balance-route-and-tool.png")

        ask("What is the balance of account 100200300501?")
        shot("screenshot-02-refused-other-customers-account.png")

        page.click("#login")  # fresh conversation
        page.wait_for_timeout(600)
        ask("trasnfer 1400$ to bob from savings")
        shot("screenshot-03-transfer-confirmation-buttons.png")
        page.click(".confirm button")
        page.wait_for_function(SEND_READY, timeout=240000)
        page.wait_for_timeout(600)

        page.goto(base + "/staff")
        page.click("#login")
        page.wait_for_selector("#list .card", timeout=30000)
        shot("screenshot-04-staff-console-pending-approval.png")

        page.click("#traces button")
        page.wait_for_function("() => document.getElementById('out').textContent.length > 80", timeout=30000)
        page.click("#audit")
        page.wait_for_function("() => document.getElementById('status').textContent.includes('OK')", timeout=30000)
        page.locator("#out").scroll_into_view_if_needed()
        page.evaluate("() => window.scrollBy(0, 200)")
        shot("screenshot-05-trace-replay-and-audit-verified.png")
        page.evaluate("() => window.scrollTo(0, 0)")
        page.click("#list .card button")  # approve, so the demo data ends tidy
        page.wait_for_timeout(1500)

        customer()
        ask("Show my recent transactions")
        shot("screenshot-06-hidden-attack-detected-and-flagged.png")
        browser.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:18080")
    ap.add_argument("--out", default=str(ROOT / "docs" / "video"))
    a = ap.parse_args()
    main(a.base, pathlib.Path(a.out))
