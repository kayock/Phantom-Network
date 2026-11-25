"""Phantom Intercom backend: opt-in push-to-talk PCM relay.

Protocol (documented in FILE_TRANSFER_PROTOCOL.md §intercom):

1. Browser captures mic audio (16 kHz mono, Int16 LE PCM, ~40 ms chunks)
   through the Web Audio API **only while the push-to-talk button is held**.
2. Browser POSTs base64 chunks to its companion: ``POST /api/intercom/talk``
   ``{"session", "pcm_b64", "seq"}``.
3. That companion relays each chunk to every session participant's
   companion: ``POST /api/intercom/relay``.
4. Each receiving companion pushes the chunk to its browser clients over
   SSE ``event: intercom`` (base64 payload).

Design consequences (all documented honestly in KNOWN_LIMITATIONS.md):
latency is ~150–400 ms (HTTP + jitter buffer); no echo cancellation beyond
the browser's own AEC; **microphone capture requires a secure context**
(localhost, or HTTPS with the optional companion TLS mode, or Chrome's
`unsafely-treat-insecure-origin-as-secure` flag on trusted LANs).

There is **no recording feature** in v1: audio is relayed live and dropped.
No covert capture is possible — capture happens exclusively in the
operator's own browser while a clearly visible PTT control is held.
"""

from __future__ import annotations

import base64
import threading
import time
from typing import Dict, List, Optional

from .util import new_id, now_ms

SESSION_TTL_MS = 15_000


class IntercomService:
    def __init__(self, config, registry, events, sse, identity_provider, log=None):
        self.config = config
        self.registry = registry
        self.events = events
        self.sse = sse
        self.identity = identity_provider
        self.log = log
        self._lock = threading.RLock()
        self._sessions: Dict[str, dict] = {}

    def enabled(self) -> bool:
        return bool(self.config.get("intercom_enabled", True))

    # ------------------------------------------------------------------
    def create_session(self, participants: List[str], label: str = "") -> dict:
        """Create a talk session with a list of target device ids."""
        if not self.enabled():
            return {"ok": False, "error": "Intercom disabled on this device"}
        me = self.identity()
        members = [{"device_id": me["device_id"], "name": me["name"]}]
        for pid in participants:
            peer = self.registry.get(pid)
            if peer:
                members.append({"device_id": pid, "name": peer.display_name()})
        session = {
            "id": new_id("icm"), "label": label or "intercom",
            "members": members, "created": now_ms(),
            "last_activity": now_ms(),
            "talking": None,
        }
        with self._lock:
            self._sessions[session["id"]] = session
        if self.events:
            names = ", ".join(m["name"] for m in members)
            self.events.emit("intercom_start",
                             f"Intercom session started: {names}",
                             data={"session": session["id"]})
        return {"ok": True, "session": session}

    def end_session(self, session_id: str) -> dict:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session and self.events:
            self.events.emit("intercom_stop", "Intercom session ended",
                             data={"session": session_id})
        return {"ended": bool(session)}

    def sessions(self) -> List[dict]:
        self._gc()
        with self._lock:
            out = []
            for s in self._sessions.values():
                d = dict(s)
                d["members"] = list(s["members"])
                out.append(d)
            return out

    # ------------------------------------------------------------------
    def talk(self, session_id: str, pcm_b64: str, seq: int) -> dict:
        """Browser → companion: relay a PCM chunk to all other members."""
        if not self.enabled():
            return {"ok": False, "error": "Intercom disabled"}
        from .peers import PeerClient
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return {"ok": False, "error": "Unknown session"}
            session["last_activity"] = now_ms()
            me = self.identity()
            session["talking"] = me["device_id"]
            members = list(session["members"])
        payload = {"session": session_id, "pcm_b64": pcm_b64, "seq": seq,
                   "from": me["device_id"], "from_name": me["name"]}
        client = PeerClient(self._client_token)
        for member in members:
            if member["device_id"] == me["device_id"]:
                continue                      # never echo back to sender
            peer = self.registry.get(member["device_id"])
            if not (peer and peer.status == "online" and peer.addresses):
                continue
            try:
                client.request("POST",
                               f"http://{peer.addresses[0]}:{peer.api_port}"
                               f"/api/intercom/relay",
                               body=payload, timeout=3)
            except Exception:
                pass
        return {"ok": True, "relayed": True}

    def relay(self, payload: dict) -> dict:
        """Another companion → us: push to local browser clients via SSE."""
        if not self.enabled():
            return {"ok": False, "error": "Intercom disabled"}
        with self._lock:
            session = self._sessions.get(payload.get("session", ""))
            if session:
                session["last_activity"] = now_ms()
                session["talking"] = payload.get("from")
        if self.sse:
            self.sse.publish("intercom", {
                "session": payload.get("session"),
                "pcm_b64": payload.get("pcm_b64", ""),
                "from": payload.get("from"),
                "from_name": payload.get("from_name", ""),
                "seq": payload.get("seq", 0),
            })
        return {"ok": True}

    def stop_talking(self, session_id: str) -> dict:
        with self._lock:
            session = self._sessions.get(session_id)
            if session:
                session["talking"] = None
        return {"ok": True}

    def _client_token(self) -> str:
        if self.config.get("require_token"):
            return self.config.get("auth_token", "")
        return ""

    def _gc(self) -> None:
        cutoff = now_ms() - SESSION_TTL_MS
        with self._lock:
            stale = [sid for sid, s in self._sessions.items()
                     if s["last_activity"] < cutoff]
            for sid in stale:
                session = self._sessions.pop(sid)
                if self.events:
                    self.events.emit("intercom_stop",
                                     "Intercom session timed out",
                                     data={"session": sid})
