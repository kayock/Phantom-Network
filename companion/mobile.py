"""Phantom Mobile — independent browser-client registration and hosting.

A phone or tablet that opens the mobile interface registers as its own
logical participant with a persistent identity (``client_id`` +
``client_secret`` kept in the browser's localStorage).  It is hosted by the
companion whose web interface it used — that companion relays communications
and stores the client's inbox, so **no phone needs to run a persistent
HTTP server of its own**.

Roles and guarantees
--------------------
* The host companion keeps ``mobile_clients.json`` (identity, name, type,
  icon, last_seen, inbox metadata).  The inbox itself lives in
  ``mobile_inbox_<client_id>.jsonl`` next to it.
* Presence is heartbeat-based: the mobile UI posts ``/api/mobile/heartbeat``
  every ~10 s; a client is *online* while its last heartbeat is inside
  ``mobile_client_ttl_ms`` and *offline* (with an honest ``last_seen``)
  otherwise.  Nothing is fabricated while the phone is locked or the browser
  is suspended — that limitation is documented, not hidden.
* Messages for a client are routed through its host: senders deliver to
  ``POST /api/chat/incoming`` on the host companion (same endpoint computers
  already use), the host stores the message in the client's inbox and pushes
  it over SSE (``event: mobile``).  **Delivered** is only reported after the
  client itself acknowledges receipt — never merely because the host
  companion received the message.
* Mobile clients never gain the host's filesystem authority.  Uploads and
  downloads still require the operator's explicitly authorized shares, and
  mobile clients are always listed with ``kind: "mobile"`` and a
  ``capabilities`` map that omits everything a browser client cannot do.
* The ``client_secret`` is a local convenience (prevents a random browser
  from renaming/removing somebody else's client) — it is not a pairing gate
  and introduces no new mandatory authentication.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from typing import Dict, List, Optional

from .errors import FsError
from .util import atomic_write_json, new_id, now_ms, read_json

MAX_INBOX_MESSAGES = 2000        # per client, ring file
MAX_INBOX_BYTES = MAX_INBOX_MESSAGES * 600

DEVICE_TYPES = {
    "phone": {"label": "Phone", "icon": "📱"},
    "tablet": {"label": "Tablet", "icon": "📲"},
    "laptop": {"label": "Laptop", "icon": "💻"},
    "watch": {"label": "Watch", "icon": "⌚"},
    "other": {"label": "Other", "icon": "📟"},
}

# What a mobile browser client can honestly do (advertised in fleet lists).
MOBILE_CAPABILITIES = {
    "chat": True,
    "receive_files": True,       # into authorized directories of computers
    "send_files": True,          # user-selected uploads / camera capture
    "voice_messages": True,
    "intercom": "browser",       # only inside a secure context, explicit PTT
    "audio_command": "browser",  # browser-side sounds only (no host player)
    "monitoring": False,         # no telemetry source on the phone itself
    "shares": 0,
    "file_commander": False,     # no unrestricted access to a phone filesystem
}


class MobileClient:
    __slots__ = ("client_id", "secret", "name", "device_type", "icon",
                 "created", "first_seen", "last_seen", "online",
                 "ua", "notify_prefs")

    def __init__(self, client_id: str):
        self.client_id = client_id
        self.secret = ""
        self.name = ""
        self.device_type = "phone"
        self.icon = "📱"
        self.created = now_ms()
        self.first_seen = now_ms()
        self.last_seen = 0
        self.online = False
        self.ua = ""
        self.notify_prefs = {}       # browser-side prefs mirrored for UX

    def to_dict(self) -> dict:
        # NOTE: includes the secret — used ONLY for on-disk persistence so a
        # browser keeps its identity across companion restarts.  to_public()
        # strips it; API responses must never leak it.
        return {
            "client_id": self.client_id, "secret": self.secret,
            "name": self.name,
            "device_type": self.device_type, "icon": self.icon,
            "created": self.created, "first_seen": self.first_seen,
            "last_seen": self.last_seen, "online": self.online,
            "notify_prefs": self.notify_prefs,
        }

    def to_public(self, host_device_id: str = "", host_name: str = "") -> dict:
        d = {k: v for k, v in self.to_dict().items() if k != "secret"}
        d.update({
            "kind": "mobile",
            "device_id": self.client_id,       # logical participant id
            "host_device_id": host_device_id,
            "host_name": host_name,
            "display_name": self.name or f"{self.device_type} client",
            "capabilities": MOBILE_CAPABILITIES,
            "status": "online" if self.online else "offline",
            "participating": True,
        })
        return d


class MobileClientRegistry:
    """Registry + inbox store for browser clients hosted by THIS companion."""

    def __init__(self, data_dir: str, config, events=None, sse=None, log=None):
        self.data_dir = data_dir
        self.config = config
        self.events = events
        self.sse = sse
        self.log = log
        self.path = os.path.join(data_dir, "mobile_clients.json")
        self._lock = threading.RLock()
        self._clients: Dict[str, MobileClient] = {}
        self._load()
        self._stop = threading.Event()
        threading.Thread(target=self._presence_loop, name="pn-mobile",
                         daemon=True).start()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        data = read_json(self.path, None) or {"clients": {}}
        for cid, d in (data.get("clients") or {}).items():
            c = MobileClient(cid)
            for slot in ("secret", "name", "device_type", "icon", "created",
                         "first_seen", "last_seen", "ua"):
                if slot in d:
                    setattr(c, slot, d[slot])
            try:
                prefs = d.get("notify_prefs")
                if isinstance(prefs, dict):
                    c.notify_prefs = prefs
            except Exception:
                pass
            c.online = False                  # re-proven by heartbeat
            self._clients[cid] = c

    def _save(self) -> None:
        with self._lock:
            data = {"clients": {cid: c.to_dict()
                                for cid, c in self._clients.items()}}
        atomic_write_json(self.path, data)

    # ------------------------------------------------------------------
    # registration / identity
    # ------------------------------------------------------------------
    def register(self, body: dict) -> dict:
        """Create or re-attach a browser client.  Returns its identity."""
        if not self.config.get("mobile_host_enabled", True):
            raise FsError(403, "mobile_disabled",
                          "Mobile hosting is disabled on this companion")
        cid = str(body.get("client_id") or "").strip()
        secret = str(body.get("client_secret") or "").strip()
        if not cid or not cid.startswith("mob_") or \
                not all(ch in "0123456789abcdef" for ch in cid[4:]):
            cid = "mob_" + secrets.token_hex(8)
        new_client = False
        with self._lock:
            client = self._clients.get(cid)
            if client is None:
                # brand-new participant: mint id + secret; the secret is
                # returned to THIS browser exactly once
                new_client = True
                client = MobileClient(cid)
                client.secret = secret or secrets.token_hex(16)
                self._clients[cid] = client
            elif not secret:
                # v1.3.1 identity protection: an existing client id may
                # NEVER be re-attached without its secret.  This is the
                # takeover vector closed here — before the fix, submitting
                # a bare client_id disclosed that client's secret.
                raise FsError(401, "secret_required",
                              "This client id is already registered. "
                              "Provide its client_secret to reconnect, or "
                              "register a new device (leave the id empty) "
                              "or use Recover identity.")
            elif secret != client.secret:
                raise FsError(403, "identity_mismatch",
                              "This browser holds a different secret for the "
                              "same client id. Use Recover identity in the "
                              "mobile UI to re-register.")
            # client exists and the secret matches: legitimate re-attach
            # (browser refresh / heartbeat recovery).  The identity below
            # deliberately does NOT include the secret — the reconnecting
            # browser already holds it.
            if body.get("name"):
                client.name = str(body["name"])[:60]
            dtype = str(body.get("device_type") or client.device_type or "phone")
            if dtype not in DEVICE_TYPES:
                dtype = "phone"
            client.device_type = dtype
            if body.get("icon"):
                client.icon = str(body["icon"])[:8]
            client.ua = str(body.get("ua") or "")[:120]
            client.last_seen = now_ms()
            client.online = True
            identity = self.client_identity(client)
        self._save()
        if self.events:
            self.events.emit("device_connected",
                             f"Mobile client registered: {client.name or cid}"
                             f" ({client.device_type})",
                             data={"client_id": cid, "kind": "mobile"})
        # the secret is returned EXACTLY ONCE — only for a brand-new
        # registration.  Re-attach with valid credentials returns the
        # identity WITHOUT the secret (the browser already holds it), and
        # public fleet lists never include it.
        if new_client:
            identity["secret"] = client.secret
        return identity

    def client_identity(self, client: MobileClient,
                        host_device_id: str = "", host_name: str = "") -> dict:
        return client.to_public(host_device_id, host_name)

    def recover(self, old_client_id: str, body: dict) -> dict:
        """Browser-storage recovery: mint a fresh id for a lost browser.

        The operator explicitly renames/removes the orphaned entry from the
        desktop UI; this endpoint only creates the NEW identity.
        """
        body = dict(body or {})
        body.pop("client_id", None)
        body.pop("client_secret", None)
        return self.register(body)

    # ------------------------------------------------------------------
    # presence
    # ------------------------------------------------------------------
    def heartbeat(self, client_id: str, secret: str) -> dict:
        c = self._authenticate(client_id, secret)
        c.last_seen = now_ms()
        if not c.online:
            c.online = True
            if self.events:
                self.events.emit("device_connected",
                                 f"Mobile client online: {c.name or c.client_id}",
                                 data={"client_id": c.client_id,
                                       "kind": "mobile"})
        self._save()
        return {"ok": True, "ts": now_ms(),
                "ttl_ms": int(self.config.get("mobile_client_ttl_ms") or 45000)}

    def update_prefs(self, client_id: str, secret: str, prefs: dict) -> dict:
        c = self._authenticate(client_id, secret)
        if isinstance(prefs, dict):
            clean = {k: v for k, v in prefs.items()
                     if k in ("sounds", "mentions", "transfers", "silent")}
            c.notify_prefs = clean
        self._save()
        return {"ok": True}

    def rename(self, client_id: str, name: str, icon: str = "",
               device_type: str = "") -> dict:
        with self._lock:
            c = self._clients.get(client_id)
            if not c:
                raise FsError(404, "no_client", "Unknown mobile client")
            if name:
                c.name = str(name)[:60]
            if icon:
                c.icon = str(icon)[:8]
            if device_type and device_type in DEVICE_TYPES:
                c.device_type = device_type
            ident = self.client_identity(c)
        self._save()
        if self.sse:
            self.sse.publish("mobile", {"type": "client_updated",
                                        "client": ident})
        return ident

    def remove(self, client_id: str) -> dict:
        with self._lock:
            c = self._clients.pop(client_id, None)
        if not c:
            raise FsError(404, "no_client", "Unknown mobile client")
        self._save()
        inbox = self._inbox_path(client_id)
        try:
            if os.path.exists(inbox):
                os.unlink(inbox)
        except OSError:
            pass
        if self.events:
            self.events.emit("system",
                             f"Mobile client removed: {c.name or client_id}",
                             severity="warn",
                             data={"client_id": client_id})
        return {"ok": True}

    def _authenticate(self, client_id: str, secret: str) -> MobileClient:
        with self._lock:
            c = self._clients.get(client_id or "")
        if c is None:
            raise FsError(404, "no_client", "Unknown mobile client")
        # v1.3.1: an empty/absent secret can never authenticate — the
        # client must exist WITH a secret and the presented secret must
        # match it exactly (no fallback path for legacy/empty secrets).
        if not c.secret or not secret or secret != c.secret:
            raise FsError(403, "identity_mismatch",
                          "Client secret required and must match")
        return c

    def sweep_offline(self) -> List[MobileClient]:
        """Mark clients whose heartbeat TTL expired; returns newly-offline."""
        ttl = int(self.config.get("mobile_client_ttl_ms") or 45000)
        cutoff = now_ms() - ttl
        lost = []
        with self._lock:
            for c in self._clients.values():
                if c.online and c.last_seen < cutoff:
                    c.online = False
                    lost.append(c)
        if lost:
            self._save()
            if self.events:
                for c in lost:
                    self.events.emit("device_lost",
                                     f"Mobile client offline: "
                                     f"{c.name or c.client_id}",
                                     severity="warn",
                                     data={"client_id": c.client_id,
                                           "kind": "mobile"})
            if self.sse:
                for c in lost:
                    self.sse.publish("mobile", {
                        "type": "presence",
                        "client_id": c.client_id,
                        "online": False,
                        "last_seen": c.last_seen,
                    })
        return lost

    def _presence_loop(self) -> None:
        while not self._stop.wait(10.0):
            try:
                self.sweep_offline()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    def all(self) -> List[MobileClient]:
        with self._lock:
            return list(self._clients.values())

    def get(self, client_id: str) -> Optional[MobileClient]:
        with self._lock:
            return self._clients.get(client_id)

    def public_list(self, host_device_id: str = "", host_name: str = "") -> list:
        with self._lock:
            return [c.to_public(host_device_id, host_name)
                    for c in self._clients.values()]

    def count_online(self) -> int:
        with self._lock:
            return sum(1 for c in self._clients.values() if c.online)

    # ------------------------------------------------------------------
    # per-client inbox (independent conversation history)
    # ------------------------------------------------------------------
    def _inbox_path(self, client_id: str) -> str:
        safe = "".join(ch for ch in client_id if ch.isalnum())
        return os.path.join(self.data_dir, f"mobile_inbox_{safe}.jsonl")

    def inbox_append(self, client_id: str, message: dict) -> None:
        c = self.get(client_id)
        if c is None:
            return
        path = self._inbox_path(client_id)
        entry = dict(message)
        entry["status"] = "delivered_to_host"   # honest: client not acked yet
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            if os.path.exists(path) and os.path.getsize(path) > MAX_INBOX_BYTES:
                self._trim_inbox(path)
        except OSError:
            pass

    def _trim_inbox(self, path: str) -> None:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()[-MAX_INBOX_MESSAGES:]
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.writelines(lines)
            os.replace(tmp, path)
        except OSError:
            pass

    def inbox_since(self, client_id: str, since_ts: int = 0,
                    limit: int = 200) -> List[dict]:
        """Messages for the client newer than ``since_ts`` (sync on connect)."""
        path = self._inbox_path(client_id)
        out = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        m = json.loads(line)
                    except ValueError:
                        continue
                    if m.get("ts", 0) > since_ts:
                        out.append(m)
        except OSError:
            pass
        out.sort(key=lambda m: m.get("ts", 0))
        return out[-limit:]

    def inbox_mark(self, client_id: str, ids: List[str],
                   state: str = "delivered") -> List[str]:
        """Flip message statuses inside the client inbox after client ack."""
        path = self._inbox_path(client_id)
        wanted = set(ids)
        changed = []
        rows = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        m = json.loads(line)
                    except ValueError:
                        continue
                    if m.get("id") in wanted and \
                            m.get("status") != state and state != "read":
                        m["status"] = state
                        changed.append(m)
                    rows.append(json.dumps(m, ensure_ascii=False))
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write("\n".join(rows) + ("\n" if rows else ""))
            os.replace(tmp, path)
        except OSError:
            pass
        return [m["id"] for m in changed]

    def inbox_update_message(self, client_id: str, msg_id: str,
                             patch: dict) -> None:
        """Rewrite one inbox row (reaction / edit / tombstone propagation)."""
        path = self._inbox_path(client_id)
        rows = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        m = json.loads(line)
                    except ValueError:
                        continue
                    if m.get("id") == msg_id:
                        m.update(patch)
                    rows.append(json.dumps(m, ensure_ascii=False))
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write("\n".join(rows) + ("\n" if rows else ""))
            os.replace(tmp, path)
        except OSError:
            pass

    def unread_counts(self, client_id: str, read_marks: dict) -> dict:
        """Conv → unread count for one client (read_marks: conv → ts)."""
        counts: Dict[str, int] = {}
        for m in self.inbox_since(client_id, 0, limit=MAX_INBOX_MESSAGES):
            if m.get("outgoing"):
                continue
            conv = m.get("conv") or ""
            if m.get("ts", 0) > (read_marks.get(conv, 0) or 0):
                counts[conv] = counts.get(conv, 0) + 1
        return counts
