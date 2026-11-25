"""Phantom Clipboard (v1.4.1) — shared clipboard for the household fleet.

Design (privacy first):
- History, pins, search, retention and clear are host-side and always on.
- AUTOMATIC desktop synchronization is OFF by default and only runs when
  (a) ``clipboard_auto_enabled`` is true, (b) a native helper is present
  (xclip/xsel on X11, wl-clipboard on Wayland, PowerShell on Windows,
  pbpaste on macOS) and (c) the device participates.  The browser
  dashboard does NOT need to be open for companion-side sync — that is
  the point of the helpers — but browser-only platforms (phones) always
  sync manually: mobile OSes do not let a web page watch the clipboard.
- Payloads: text (default cap 256 KiB, configurable) and small images
  (PNG/JPEG data URLs, cap 2 MiB).  Oversized payloads are rejected
  honestly with a size error, never truncated.
- Duplicate suppression: SHA-256 of the payload; an identical payload
  already in history (or seen within DEDUPE_WINDOW_S) is not re-stored
  or re-delivered.
- Loop prevention: entries carry ``origin`` (device id) and ``hop``; a
  companion never re-broadcasts an entry that originated on it, and
  receiving never triggers sending.  There is no auto-execution of
  clipboard content anywhere; content is inert data.
- On receive (manual or auto), text is written to the local OS clipboard
  via the native helper when available so the user can paste directly.
  The auto-poll loop treats that write as a duplicate hash and does not
  fan it back out.
- Participation: per-device ``clipboard_participating`` config plus a
  global pause (``clipboard_paused``) and private mode that drops
  incoming entries entirely.  We make NO claim of detecting passwords
  or other sensitive content — users control what they copy.
- No cloud services, no new credentials: delivery reuses the peer HTTP
  channel and the mobile SSE stream, like every other feature.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

from .util import now_ms, iso_now, atomic_write_json, read_json

MAX_TEXT_DEFAULT = 256 * 1024
MAX_IMAGE_DEFAULT = 2 * 1024 * 1024
HISTORY_CAP = 300
DEDUPE_WINDOW_S = 5
HISTORY_FILE = "clipboard_history.json"

# native helpers per platform: (binary_name, paste_argv, copy_argv)
# Windows copy uses stdin → Set-Clipboard (see os_copy).
HELPER_CANDIDATES = {
    "wayland": ("wl-paste", ["wl-paste", "--no-newline"],
                ["wl-copy"]),
    "x11": ("xclip", ["xclip", "-selection", "clipboard", "-o"],
            ["xclip", "-selection", "clipboard"]),
    "x11-alt": ("xsel", ["xsel", "--clipboard", "--output"],
                ["xsel", "--clipboard", "--input"]),
    "windows": ("powershell.exe" if os.name == "nt" else "powershell",
                None,  # built in os_paste for Windows
                None),
    "macos": ("pbpaste", ["pbpaste"], ["pbcopy"]),
}

# Extra lookup paths so a user-local wl-clipboard / xclip install is found
# even when the companion was started with a minimal PATH (SSH, schtasks).
_HELPER_PATH_EXTRA = [
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/.local/opt/pn-helpers/wl-clipboard/usr/bin"),
    "/usr/local/bin",
    "/usr/bin",
]


class ClipboardError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _which(name: str) -> Optional[str]:
    """shutil.which plus well-known user-local helper paths."""
    found = shutil.which(name)
    if found:
        return found
    for root in _HELPER_PATH_EXTRA:
        candidate = os.path.join(root, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    # Windows: powershell may live outside PATH for some service contexts
    if name.lower() in ("powershell.exe", "powershell") and os.name == "nt":
        windir = os.environ.get("SystemRoot") or os.environ.get("WINDIR") \
            or r"C:\Windows"
        for sub in (r"System32\WindowsPowerShell\v1.0\powershell.exe",
                    r"SysWOW64\WindowsPowerShell\v1.0\powershell.exe"):
            candidate = os.path.join(windir, sub)
            if os.path.isfile(candidate):
                return candidate
    return None


def _helper_env() -> dict:
    """Environment for clipboard helpers.

    Prefer the running process env (desktop launcher inherits the session).
    When started from SSH without WAYLAND_DISPLAY/DISPLAY, pull them from
    ``systemctl --user show-environment`` so wl-clipboard / xclip can reach
    the graphical session.
    """
    env = dict(os.environ)
    # Ensure user-local helper bins are visible to child processes too.
    extras = [p for p in _HELPER_PATH_EXTRA if os.path.isdir(p)]
    path = env.get("PATH") or ""
    for p in extras:
        if p not in path.split(os.pathsep):
            path = p + os.pathsep + path
    env["PATH"] = path
    need = ("WAYLAND_DISPLAY", "DISPLAY", "XDG_RUNTIME_DIR",
            "DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_TYPE")
    try:
        r = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            capture_output=True, timeout=3, text=True)
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k in need and v and not env.get(k):
                    env[k] = v
    except (OSError, subprocess.TimeoutExpired):
        pass
    # Last-resort runtime dir for user session sockets (POSIX only)
    if os.name != "nt":
        if not env.get("XDG_RUNTIME_DIR"):
            try:
                env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
            except AttributeError:
                pass
        runtime = env.get("XDG_RUNTIME_DIR") or ""
        if not env.get("WAYLAND_DISPLAY") and runtime and os.path.exists(
                os.path.join(runtime, "wayland-0")):
            env["WAYLAND_DISPLAY"] = "wayland-0"
    return env


class ClipboardEngine:
    def __init__(self, data_dir: str, config, registry, mobile, identity,
                 sse, events, log=None):
        self.data_dir = data_dir
        self.config = config
        self.registry = registry
        self.mobile = mobile
        self.identity = identity
        self.sse = sse
        self.events = events
        self.log = log
        self._lock = threading.RLock()
        self._path = os.path.join(data_dir, HISTORY_FILE)
        self._items: List[dict] = read_json(self._path, None) or []
        self._last_hashes: List[str] = []     # recent hashes (loop guard)
        self._stop = threading.Event()
        self._auto_thread: Optional[threading.Thread] = None
        self._auto_last_hash = ""
        # Hashes we just wrote to the OS clipboard from a peer receive —
        # auto-poll must not fan these back out.
        self._suppress_fanout: Dict[str, float] = {}
        self.ensure_auto_sync()

    # ------------------------------------------------------------------
    # configuration surface
    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.config.get("clipboard_enabled", True))

    @property
    def max_text(self) -> int:
        return int(self.config.get("clipboard_max_bytes", MAX_TEXT_DEFAULT))

    @property
    def max_image(self) -> int:
        return int(self.config.get("clipboard_max_image_bytes",
                                   MAX_IMAGE_DEFAULT))

    def status(self) -> dict:
        me = self.identity() or {}
        return {
            "ok": True,
            "enabled": self.enabled,
            "auto_enabled": bool(self.config.get("clipboard_auto_enabled",
                                                 False)),
            "paused": bool(self.config.get("clipboard_paused", False)),
            "private_mode": bool(self.config.get("clipboard_private_mode",
                                                 False)),
            "participating": bool(self.config.get(
                "clipboard_participating", True)),
            "retention_days": int(self.config.get(
                "clipboard_retention_days", 30)),
            "max_text": self.max_text, "max_image": self.max_image,
            "helpers": self.helper_status(),
            "active_helper": (self._active_helper() or (None,))[0],
            "device_id": me.get("device_id"),
        }

    def helper_status(self) -> dict:
        """Which native helpers exist (honest detection, no assumptions)."""
        out = {}
        for key, (name, _paste, _copy) in HELPER_CANDIDATES.items():
            out[key] = bool(_which(name))
        return out

    def ensure_auto_sync(self) -> None:
        """Start the auto-sync poller when enabled; safe to call repeatedly
        (e.g. after the user toggles the setting without restarting)."""
        if not self.config.get("clipboard_auto_enabled", False):
            return
        if self._auto_thread and self._auto_thread.is_alive():
            return
        self._stop.clear()
        self._auto_thread = threading.Thread(target=self._auto_sync_loop,
                                             name="pn-clipboard-auto",
                                             daemon=True)
        self._auto_thread.start()

    # ------------------------------------------------------------------
    # history
    # ------------------------------------------------------------------
    def _persist(self) -> None:
        atomic_write_json(self._path, self._items)

    def _prune(self) -> None:
        retention = int(self.config.get("clipboard_retention_days", 30))
        cutoff = now_ms() - retention * 86400_000
        pinned = [x for x in self._items if x.get("pinned")]
        unpinned = [x for x in self._items
                    if not x.get("pinned") and x.get("ts", 0) >= cutoff]
        items = pinned + unpinned
        if len(items) > HISTORY_CAP:
            items = [x for x in items if x.get("pinned")] + \
                [x for x in items if not x.get("pinned")][-HISTORY_CAP:]
        self._items = items[-HISTORY_CAP * 2:]

    def items(self, query: str = "", limit: int = 100) -> dict:
        with self._lock:
            self._prune()
            out = list(self._items)
        if query:
            q = query.lower()
            out = [x for x in out
                   if (x.get("text") or "").lower().find(q) >= 0
                   or (x.get("name") or "").lower().find(q) >= 0]
        out.sort(key=lambda x: (not x.get("pinned"),
                                -(x.get("ts") or 0)))
        return {"ok": True, "items": out[:limit]}

    def clear(self, pins_too: bool = False) -> dict:
        with self._lock:
            self._items = [] if pins_too else \
                [x for x in self._items if x.get("pinned")]
            self._persist()
        return {"ok": True, "kept": len(self._items)}

    def pin(self, item_id: str, pinned: bool) -> dict:
        with self._lock:
            for x in self._items:
                if x["id"] == item_id:
                    x["pinned"] = bool(pinned)
                    self._persist()
                    return {"ok": True, "item": x}
        raise ClipboardError(404, "no_item", "Unknown clipboard item")

    # ------------------------------------------------------------------
    # capture / store
    # ------------------------------------------------------------------
    def put(self, body: dict, origin: Optional[dict] = None) -> dict:
        """Store a clipboard entry (from a browser, a phone or a peer).

        Returns the stored entry.  Duplicates (same payload hash already
        present, or seen in the dedupe window) return the existing entry
        with ``duplicate: true`` and are not re-delivered.
        """
        kind = str(body.get("kind") or "text")
        me = self.identity() or {}
        origin = origin or {"device_id": me.get("device_id"),
                            "name": me.get("name")}
        if kind == "text":
            text = str(body.get("text") or "")
            data = text.encode("utf-8")
            if len(data) > self.max_text:
                raise ClipboardError(
                    413, "too_large",
                    f"Text payload {len(data)} B exceeds the configured "
                    f"limit of {self.max_text} B")
            digest = _sha256_bytes(data)
            name = ""
        elif kind == "image":
            # PNG/JPEG as a data URL — small screenshots only
            url = str(body.get("data_url") or "")
            if not url.startswith("data:image/"):
                raise ClipboardError(400, "bad_image",
                                     "Image entries must be image data URLs")
            data = url.encode("utf-8")
            if len(data) > self.max_image:
                raise ClipboardError(
                    413, "too_large",
                    f"Image payload {len(data)} B exceeds the configured "
                    f"limit of {self.max_image} B")
            digest = _sha256_bytes(data)
            text = ""
            name = str(body.get("name") or "image")
        else:
            raise ClipboardError(400, "bad_kind",
                                 f"Unknown clipboard kind {kind}")
        with self._lock:
            # duplicate suppression: identical hash already stored →
            # bump its timestamp instead of storing a copy
            for x in self._items:
                if x.get("hash") == digest:
                    x["ts"] = now_ms()
                    self._persist()
                    return {"ok": True, "item": x, "duplicate": True}
            entry = {
                "id": "cl_" + os.urandom(5).hex(),
                "kind": kind, "text": text, "data_url":
                body.get("data_url", "") if kind == "image" else "",
                "name": name,
                "size": len(data), "hash": digest,
                "origin": origin, "hop": int(body.get("hop") or 0),
                "ts": now_ms(), "iso": iso_now(), "pinned": False,
            }
            self._items.append(entry)
            self._prune()
            self._persist()
        return {"ok": True, "item": entry, "duplicate": False}

    # ------------------------------------------------------------------
    # delivery
    # ------------------------------------------------------------------
    def send(self, body: dict) -> dict:
        """Store locally then deliver to the requested targets.  Loop
        prevention: entries originated HERE are never relayed back here;
        receivers never auto-forward (hop grows, capped at 1)."""
        if not self.enabled or self.config.get("clipboard_paused"):
            raise ClipboardError(403, "clipboard_paused",
                                 "Clipboard is paused on this companion")
        targets = body.get("targets") or []
        if not targets:
            raise ClipboardError(400, "no_targets",
                                 "Clipboard send needs targets")
        if any(not isinstance(t, dict) for t in targets):
            raise ClipboardError(400, "bad_targets",
                                 "Each target must be an object with "
                                 "kind + device_id or client_id")
        r = self.put({"kind": body.get("kind") or "text",
                      "text": body.get("text") or "",
                      "data_url": body.get("data_url") or "",
                      "name": body.get("name") or ""})
        entry = r["item"]
        if r["duplicate"]:
            return {"ok": True, "entry": entry, "results": [],
                    "duplicate": True, "note": "duplicate suppressed"}
        me = self.identity() or {}
        results = []
        for t in targets:
            if t.get("kind") == "mobile":
                cid = t.get("client_id") or ""
                host = t.get("host_device_id") or me.get("device_id")
                ok = self._push_phone(cid, entry, host)
                results.append({"id": cid, "kind": "mobile",
                                "state": "delivered" if ok
                                else "unavailable"})
            else:
                did = t.get("device_id") or ""
                if did == me.get("device_id"):
                    results.append({"id": did, "kind": "computer",
                                    "state": "local"})
                    continue
                ok = self._push_computer(did, entry)
                results.append({"id": did, "kind": "computer",
                                "state": "delivered" if ok
                                else "unavailable"})
        if self.events:
            self.events.emit("clipboard",
                             f"Clipboard shared with "
                             f"{sum(1 for x in results if x['state'] in
                                    ('delivered', 'local'))} device(s)",
                             data={"id": entry["id"]})
        return {"ok": True, "entry": entry, "results": results}

    def _entry_payload(self, entry: dict) -> dict:
        return dict(entry, hop=1)   # receivers never forward: hop 1

    def _push_computer(self, device_id: str, entry: dict) -> bool:
        peer = self.registry.get(device_id)
        if not (peer and peer.addresses):
            return False
        # v1.3.2 lesson: verify destination identity before first delivery;
        # an address alone is not an identity and stale presence is not
        # proof of unreachability.
        if not self.registry.verify_peer(peer.device_id, peer.addresses[0],
                                         peer.api_port, timeout=2.5):
            return False
        from .peers import PeerClient
        client = PeerClient(lambda: self.config.get("auth_token") or "")
        try:
            client.request(
                "POST",
                f"http://{peer.addresses[0]}:{peer.api_port}"
                f"/api/clipboard/incoming",
                body={"entry": self._entry_payload(entry)}, timeout=8)
            return True
        except Exception:
            return False

    def _push_phone(self, client_id: str, entry: dict,
                    host_device_id: str) -> bool:
        me = self.identity() or {}
        payload = self._entry_payload(entry)
        if not host_device_id or host_device_id == me.get("device_id"):
            if self.sse:
                self.sse.publish("mobile", {"type": "clipboard",
                                            "for": client_id,
                                            "data": payload})
            return True
        peer = self.registry.get(host_device_id)
        if peer and peer.addresses:
            from .peers import PeerClient
            client = PeerClient(lambda: self.config.get("auth_token") or "")
            try:
                client.request(
                    "POST",
                    f"http://{peer.addresses[0]}:{peer.api_port}"
                    f"/api/clipboard/relay",
                    body={"for": client_id, "entry": payload}, timeout=6)
                return True
            except Exception:
                return False
        return False

    def incoming(self, entry: dict) -> dict:
        """Peer-to-peer: another companion sent clipboard content HERE.
        Respects private mode / pause; never auto-forwards.  Writes text
        into the local OS clipboard so the user can paste immediately."""
        if not self.enabled or self.config.get("clipboard_paused"):
            return {"ok": True, "state": "paused"}
        if self.config.get("clipboard_private_mode"):
            return {"ok": True, "state": "private"}
        if int(entry.get("hop") or 0) > 1:
            return {"ok": True, "state": "hop-limited"}
        if not self.config.get("clipboard_participating", True):
            return {"ok": True, "state": "not-participating"}
        r = self.put({"kind": entry.get("kind") or "text",
                      "text": entry.get("text") or "",
                      "data_url": entry.get("data_url") or "",
                      "name": entry.get("name") or "",
                      "hop": entry.get("hop") or 0},
                     origin=entry.get("origin") or {})
        if not r["duplicate"]:
            self._apply_os_clipboard(r["item"])
            self._publish_local(r["item"])
        return {"ok": True, "state": "delivered",
                "duplicate": bool(r["duplicate"])}

    def relay(self, body: dict) -> dict:
        cid = str(body.get("for") or "")
        entry = body.get("entry") or {}
        if self.sse:
            self.sse.publish("mobile", {"type": "clipboard", "for": cid,
                                        "data": entry})
        return {"ok": True}

    def _publish_local(self, entry: dict) -> None:
        if self.sse:
            self.sse.publish("clipboard", {"type": "clipboard",
                                           "for": None, "data": entry})
        if self.sse and self.mobile:
            # every locally-hosted phone gets it too (they fetch history)
            try:
                for c in self.mobile.all():
                    self.sse.publish("mobile",
                                     {"type": "clipboard",
                                      "for": c.client_id,
                                      "data": entry})
            except Exception:
                pass

    def _apply_os_clipboard(self, entry: dict) -> None:
        """Write received text to the OS clipboard and suppress auto-fanout
        of the same payload for a short window (loop guard)."""
        if (entry.get("kind") or "text") != "text":
            return
        text = entry.get("text") or ""
        if not text:
            return
        digest = entry.get("hash") or _sha256_bytes(text.encode("utf-8"))
        self._suppress_fanout[digest] = time.time() + 15.0
        self._auto_last_hash = digest
        try:
            self.os_copy(text)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # native helpers (auto-sync — OFF by default)
    # ------------------------------------------------------------------
    def _session_prefers_wayland(self) -> bool:
        env = _helper_env()
        return bool(env.get("WAYLAND_DISPLAY")) or \
            (env.get("XDG_SESSION_TYPE") or "").lower() == "wayland"

    def _active_helper(self) -> Optional[Tuple[str, Optional[list],
                                                 Optional[list]]]:
        order = ("wayland", "x11", "x11-alt", "macos", "windows")
        if not self._session_prefers_wayland():
            order = ("x11", "x11-alt", "wayland", "macos", "windows")
        for key in order:
            name, paste, copy = HELPER_CANDIDATES[key]
            if _which(name):
                return (key, paste, copy)
        return None

    def os_paste(self) -> Optional[str]:
        """Read the OS clipboard via the native helper (helper required;
        returns None when absent or empty)."""
        helper = self._active_helper()
        if not helper:
            return None
        key, paste, _copy = helper
        env = _helper_env()
        try:
            if key == "windows":
                ps = _which(HELPER_CANDIDATES["windows"][0])
                if not ps:
                    return None
                r = subprocess.run(
                    [ps, "-NoProfile", "-NonInteractive", "-Command",
                     "Get-Clipboard -Raw"],
                    capture_output=True, timeout=8, env=env)
            else:
                # Resolve binary to absolute path for minimal-PATH contexts
                argv = list(paste)
                resolved = _which(argv[0])
                if resolved:
                    argv[0] = resolved
                r = subprocess.run(argv, capture_output=True, timeout=5,
                                   env=env)
            if r.returncode != 0:
                return None
            text = r.stdout.decode("utf-8", "replace")
            # PowerShell Get-Clipboard may append a trailing newline
            if key == "windows" and text.endswith("\r\n"):
                text = text[:-2]
            elif key == "windows" and text.endswith("\n"):
                text = text[:-1]
            return text if text else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    def os_copy(self, text: str) -> bool:
        """Write the OS clipboard via the native helper."""
        helper = self._active_helper()
        if not helper:
            return False
        key, _paste, copy = helper
        env = _helper_env()
        try:
            if key == "windows":
                ps = _which(HELPER_CANDIDATES["windows"][0])
                if not ps:
                    return False
                # Pipe UTF-8 text on stdin into Set-Clipboard.  Passing the
                # payload as an extra argv (v1.4.0) never reached $input and
                # left the Windows clipboard unchanged.
                r = subprocess.run(
                    [ps, "-NoProfile", "-NonInteractive", "-Command",
                     "$input | Set-Clipboard"],
                    input=text.encode("utf-8"),
                    capture_output=True, timeout=8, env=env)
                return r.returncode == 0
            argv = list(copy)
            resolved = _which(argv[0])
            if resolved:
                argv[0] = resolved
            # wl-copy keeps a background offer process that can hold pipes
            # open forever — never capture stdout/stderr or it times out
            # even after a successful write.
            r = subprocess.run(argv, input=text.encode("utf-8"),
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,
                               timeout=5, env=env)
            return r.returncode == 0
        except subprocess.TimeoutExpired:
            # Treat timeout after a Wayland offer as success when paste
            # can already read the payload (wl-copy quirk).
            if key == "wayland":
                got = self.os_paste()
                return got == text
            return False
        except OSError:
            return False

    def _auto_sync_loop(self) -> None:
        """Poll the OS clipboard every 2 s; store+publish NEW payloads.
        Loop prevention: identical consecutive hashes are skipped; hashes
        recently applied from peer receive are not fanout out again.
        Participation and pause flags are honored every tick."""
        while not self._stop.wait(2.0):
            if not self.enabled or \
                    self.config.get("clipboard_paused") or \
                    not self.config.get("clipboard_auto_enabled", False) or \
                    not self.config.get("clipboard_participating", True):
                continue
            text = self.os_paste()
            if not text:
                continue
            digest = _sha256_bytes(text.encode("utf-8"))
            if digest == self._auto_last_hash:
                continue
            self._auto_last_hash = digest
            # Expire suppression map
            now = time.time()
            self._suppress_fanout = {
                h: exp for h, exp in self._suppress_fanout.items()
                if exp > now}
            suppress = digest in self._suppress_fanout
            try:
                r = self.put({"kind": "text", "text": text})
                if r["duplicate"]:
                    continue
                self._publish_local(r["item"])
                if not suppress:
                    self._fanout_peers(r["item"])
            except ClipboardError:
                continue

    def _fanout_peers(self, entry: dict) -> None:
        me = self.identity() or {}
        for peer in list(self.registry.all()):
            if peer.device_id == me.get("device_id") or \
                    not peer.addresses or not peer.participating:
                continue
            if getattr(peer, "status", None) == "offline":
                continue
            self._push_computer(peer.device_id, entry)

    def shutdown(self) -> None:
        self._stop.set()
