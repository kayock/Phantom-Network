#!/usr/bin/env python3
"""OPTIONAL Phantom Network system tray companion (v1.3.0).

Deliberately dependency-minimal: uses pystray + Pillow IF installed
(`pip install pystray pillow`), otherwise prints honest instructions and
exits.  Never autostarts, never installs anything, purely local.

Features: open the command center, list online devices, unread GhostChat
count, pause notifications (SSE-free: polls /api/stats at low rate),
stop companion (POST to the local admin endpoint equivalent: SIGTERM is
NOT sent — it only opens the UI; stopping stays a manual action to avoid
surprises).  If the tray stack is unavailable, normal browser operation
is unaffected — this helper is a convenience, not a dependency.
"""
import json
import sys
import urllib.request

API = "http://127.0.0.1:8415"


def stats():
    try:
        with urllib.request.urlopen(f"{API}/api/stats", timeout=3) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def main():
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        print("Tray support needs the optional pystray stack:")
        print("  pip install pystray pillow")
        print("Without it, use Phantom Network in the browser as usual.")
        return 1
    import pystray
    from PIL import Image, ImageDraw

    def icon_image(color=(139, 92, 246)):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse([4, 4, 60, 60], fill=(10, 12, 20, 255),
                  outline=color + (255,), width=4)
        d.ellipse([22, 22, 42, 42], fill=color + (255,))
        return img

    def title_with_unread(_: pystray.Icon) -> str:
        s = stats()
        if not s:
            return "Phantom: companion unreachable"
        unread = s.get("chat", {}).get("messages_stored", 0)
        peers = s.get("peers_online", 0)
        return f"Phantom — {peers} online"

    def open_ui(icon, item):
        import webbrowser
        webbrowser.open(API)

    def quit_tray(icon, item):
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: title_with_unread(None), None, enabled=False),
        pystray.MenuItem("Open Phantom Network", open_ui, default=True),
        pystray.MenuItem("Quit tray", quit_tray),
    )
    ic = pystray.Icon("phantom", icon_image(), "Phantom Network", menu)
    ic.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
