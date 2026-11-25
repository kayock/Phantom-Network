#!/usr/bin/env python3
"""v1.4.0 browser qualification: every shipped desktop view and every
mobile tab loads and operates; genuine screenshots captured; console
errors collected (the v1.3.0 release shipped a JS syntax defect that
prevented its mobile UI from loading — this sweep exists so that never
happens again)."""

import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from tests.conftest import Companion  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402

OUT = os.path.join(ROOT, "docs", "screenshots")
os.makedirs(OUT, exist_ok=True)

DESKTOP_VIEWS = [
    ("dashboard", "Command Center"),
    ("radar", "Network Radar"),
    ("voice", "Phantom Voice"),
    ("broadcast", "Phantom Broadcast"),
    ("media", "Phantom Media"),
    ("clipboard", "Phantom Clipboard"),
    ("mesh", "Phantom Mesh"),
    ("chat", "GhostChat"),
    ("intercom", "Phantom Intercom"),
    ("audio", "Audio Command"),
    ("signal", "Signal Laboratory"),
    ("fleet", "Fleet Monitor"),
    ("transfers", "Transfer Center"),
    ("events", "Event Center"),
    ("diagnostics", "Diagnostics"),
    ("settings", "Settings"),
    ("commander", "File Commander"),
]

SIGNAL_TABS = ["spectrum", "morse", "practice", "acoustic", "modem", "chat"]
MOBILE_TABS = ["home", "chat", "voice", "media", "clip", "files", "devices",
               "more"]


def shot(page, name):
    page.screenshot(path=os.path.join(OUT, f"{name}.png"), full_page=False)


def main() -> int:
    c = Companion("BrowserQA").start()
    failures = []
    console_errors = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=[
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--autoplay-policy=no-user-gesture-required"])
            ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
            page.on("console", lambda m: console_errors.append(
                f"{m.type}: {m.text[:160]}") if m.type == "error" else None)
            page.on("pageerror", lambda e: console_errors.append(
                f"pageerror: {str(e)[:200]}"))

            page.goto(f"{c.base}/", wait_until="domcontentloaded", timeout=30000)
            time.sleep(1.0)
            # first-run onboarding modal: complete it so the nav is usable
            try:
                page.click('button:has-text("Finish setup")', timeout=4000)
                page.wait_for_timeout(600)
            except Exception:
                try:
                    page.click('button:has-text("Continue without sharing")',
                               timeout=3000)
                    page.wait_for_timeout(600)
                except Exception:
                    pass
            # dismiss any "update available" toast overlay
            try:
                page.evaluate("""document.querySelectorAll('.toast')
                    .forEach(t => t.remove())""")
            except Exception:
                pass
            shot(page, "v140-command-center")

            for view, title in DESKTOP_VIEWS:
                try:
                    page.click(f'a.nav-item[data-view="{view}"]',
                               timeout=5000)
                    page.wait_for_timeout(1400)
                    body_text = page.inner_text("body")
                    if view == "signal":
                        # drive every signal-lab tab
                        for tab in SIGNAL_TABS:
                            page.click(f'.tabbar .tab:has-text("{tab}")'
                                       if tab != "practice"
                                       else '.tabbar .tab:has-text("practice")',
                                       timeout=4000)
                            page.wait_for_timeout(600)
                        page.click('.tabbar .tab:has-text("practice")',
                                   timeout=4000)
                        page.wait_for_timeout(500)
                        page.click('button:has-text("New session")',
                                   timeout=4000)
                        page.wait_for_timeout(600)
                        txt = page.inner_text("body")
                        assert "accuracy" in txt.lower() or "session" in \
                            txt.lower(), "practice session did not start"
                        page.click('.tabbar .tab:has-text("chat")',
                                   timeout=4000)
                        page.wait_for_timeout(500)
                        page.click('button:has-text("Offline self-test")',
                                   timeout=4000)
                        page.wait_for_timeout(1200)
                        txt = page.inner_text("body")
                        assert "self-test" in txt.lower(), \
                            "acoustic chat self-test did not run"
                        page.click('.tabbar .tab:has-text("spectrum")',
                                   timeout=4000)
                        page.wait_for_timeout(500)
                    if view == "clipboard":
                        assert "Phantom Clipboard" in body_text
                        assert "history" in body_text.lower()
                    if view == "voice":
                        assert "Phantom Voice" in body_text
                        assert "secure context" in body_text.lower() or \
                            "WebRTC" in body_text
                    if view == "broadcast":
                        assert "Phantom Broadcast" in body_text
                        assert "Dinner announcement" in body_text
                    if view == "media":
                        assert "Phantom Media" in body_text
                    if view == "mesh":
                        assert "Phantom Mesh" in body_text
                    shot(page, f"v140-{view}")
                    print(f"  ✓ {view}")
                except Exception as e:
                    failures.append(f"{view}: {str(e)[:200]}")
                    print(f"  ✗ {view}: {str(e)[:160]}")

            # ---------------- mobile ----------------
            m = browser.new_context(
                viewport={"width": 390, "height": 844},
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like "
                           "Mac OS X) AppleWebKit/605.1.15 (KHTML, like "
                           "Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
            mp = m.new_page()
            mp.on("pageerror", lambda e: console_errors.append(
                f"mobile pageerror: {str(e)[:200]}"))
            mp.goto(f"{c.base}/m", wait_until="domcontentloaded", timeout=30000)
            mp.wait_for_timeout(1200)
            txt = mp.inner_text("body")
            if "name" in txt.lower() and "register" in txt.lower():
                mp.fill("#ob-name", "QA iPhone")
                mp.click("#ob-register", timeout=5000)
                mp.wait_for_timeout(1500)
            for tab in MOBILE_TABS:
                try:
                    mp.click(f'.mtab[data-tab="{tab}"]', timeout=5000)
                    mp.wait_for_timeout(900)
                    body_text = mp.inner_text("body")
                    if tab == "voice":
                        assert "Call a device" in body_text or \
                            "Announce" in body_text
                    if tab == "clip":
                        assert "History" in body_text or "Clipboard" in \
                            body_text
                    if tab == "media":
                        assert "Phantom Media" in body_text
                    if tab == "more":
                        assert "Name, type" in body_text or \
                            "This device" in body_text, \
                            f"more tab content missing: {body_text[:120]}"
                    shot(mp, f"v140-mobile-{tab}")
                    print(f"  ✓ mobile/{tab}")
                except Exception as e:
                    failures.append(f"mobile/{tab}: {str(e)[:200]}")
                    print(f"  ✗ mobile/{tab}: {str(e)[:160]}")

            # mobile onboarding restore form presence
            mp.click('.mtab[data-tab="more"]', timeout=4000)
            mp.wait_for_timeout(400)
            try:
                mp.click('button:has-text("Reset identity")', timeout=3000)
                mp.wait_for_timeout(800)
                assert "Restore an existing identity" in \
                    mp.inner_text("body"), "restore form missing"
                shot(mp, "v140-mobile-restore")
                print("  ✓ mobile/restore")
            except Exception as e:
                failures.append(f"mobile/restore: {str(e)[:160]}")

            browser.close()
    finally:
        c.stop()

    real_errors = [e for e in console_errors
                   if "favicon" not in e and "404" not in e
                   and "Autoplay" not in e and "autoplay" not in e]
    print("\nconsole/page errors:", len(real_errors))
    for e in real_errors[:12]:
        print("   ", e)
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nALL BROWSER CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
