"""Phantom Voice (v1.4.0) — local voice communications.

A signaling registry and relay for WebRTC voice sessions.  The companion
NEVER terminates media: browsers exchange SDP/ICE offers/answers/candidates
directly over the LAN (``iceServers: []`` — host candidates suffice on one
subnet), while the companions relay the opaque signaling blobs over the
SSE hub and the existing mobile private streams, and between companions
over the same peer HTTP channel used by GhostChat.

Modes:
  call  — direct 1:1 call (computer or phone participants)
  room  — named group voice room, members join/leave freely
  ptt   — push-to-talk channel: join without ringing, talk while held
          (audio still P2P WebRTC; the classic PCM intercom remains a
          separate, explicit compatibility mode)

Privacy contract (unchanged): every participant must have explicitly
answered or joined; there is no hidden capture, no recording, no
auto-answer.  Microphone access is a browser permission the user grants
per session, with visible UI state.

All call state is in-memory; history is an append-only JSONL for the UI.
A call that rings past RING_TIMEOUT_S becomes "missed" and is recorded as
a GhostChat system message so the recipient sees it even after a restart.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from typing import Dict, List, Optional

from .util import now_ms, iso_now, read_json

RING_TIMEOUT_S = 45
MAX_CALLS = 200          # registry cap (finished calls are history anyway)
MAX_SIGNALS_PER_CALL = 2000


class VoiceError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def _now() -> int:
    return now_ms()


class Call:
    __slots__ = ("id", "mode", "room", "origin", "targets", "state",
                 "created", "connected", "ended", "ended_by", "reason",
                 "signal_count", "origin_host", "mirrored")

    def __init__(self, call_id: str, mode: str, origin: dict,
                 targets: List[dict], room: str = "",
                 origin_host: str = "", mirrored: bool = False):
        self.id = call_id
        self.mode = mode            # call | room | ptt
        self.room = room
        self.origin = origin        # {device_id|client_id, name, kind}
        self.targets = targets      # participant descriptors
        self.state = "ringing"      # ringing|active|ended|missed|rejected
        self.created = _now()
        self.connected = 0
        self.ended = 0
        self.ended_by = ""
        self.reason = ""
        self.signal_count = 0
        # v1.4.0 cross-companion calls: the ORIGIN companion owns the call
        # record; other companions hold a MIRROR and forward answer/signal/
        # end actions to the origin over the peer channel.
        self.origin_host = origin_host
        self.mirrored = mirrored

    def participants(self) -> List[str]:
        out = []
        if self.origin.get("device_id"):
            out.append(self.origin["device_id"])
        elif self.origin.get("client_id"):
            out.append(self.origin["client_id"])
        for t in self.targets:
            key = t.get("device_id") or t.get("client_id")
            if key:
                out.append(key)
        return out

    def public(self) -> dict:
        return {
            "id": self.id, "mode": self.mode, "room": self.room,
            "origin": self.origin, "targets": self.targets,
            "state": self.state,
            "created": self.created,
            "connected": self.connected,
            "ended": self.ended,
            "ended_by": self.ended_by,
            "duration_s": (round((self.ended or _now()) - self.connected)
                           / 1000) if self.connected else 0,
            "reason": self.reason,
        }


class VoiceEngine:
    """Registry + signaling relay.  See module docstring for the design."""

    def __init__(self, data_dir: str, config, registry, chat, identity,
                 sse, events, log=None):
        self.data_dir = data_dir
        self.config = config
        self.registry = registry      # peer registry (computers)
        self.chat = chat              # for missed-call system messages
        self.identity = identity      # callable -> this companion identity
        self.sse = sse
        self.events = events
        self.log = log
        self._lock = threading.RLock()
        self._calls: Dict[str, Call] = {}
        self._rooms: Dict[str, dict] = {}   # room_id -> {name, members, ts}
        self._hist_path = os.path.join(data_dir, "call_history.jsonl")
        hist = read_json(self._hist_path, None)
        self._hist_loaded = hist is not None or os.path.exists(self._hist_path)
        self._stop = threading.Event()
        self._monitor = threading.Thread(target=self._ring_watchdog,
                                         daemon=True)
        self._monitor.start()

    # ------------------------------------------------------------------
    # configuration
    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.config.get("voice_enabled", True))

    # ------------------------------------------------------------------
    # outbound fan-out
    # ------------------------------------------------------------------
    def _publish_computer(self, device_id: str, etype: str, payload) -> None:
        if self.sse:
            self.sse.publish("voice", {"type": etype, "for": device_id,
                                       "data": payload})

    def _publish_phone(self, client_id: str, etype: str, payload,
                       host_device_id: str = "") -> None:
        """Send a voice event to a hosted phone — locally, or relayed to
        the hosting companion over the peer channel."""
        me = self.identity() or {}
        if not host_device_id or host_device_id == me.get("device_id"):
            if self.sse:
                self.sse.publish("mobile", {"type": "voice",
                                            "for": client_id,
                                            "data": payload})
            return
        peer = self.registry.get(host_device_id)
        if peer and peer.addresses:
            # v1.3.2 lesson: verify the destination really IS this host
            # before relaying (an address alone is not an identity); never
            # gate on cached presence — probe and report honestly.
            addr = peer.addresses[0]
            if not self.registry.verify_peer(peer.device_id, addr,
                                             peer.api_port, timeout=2.5):
                if self.log:
                    self.log.warning("voice relay: host %s did not verify "
                                     "at %s — relay skipped", host_device_id,
                                     addr)
                return
            from .peers import PeerClient
            client = PeerClient(self._token_provider)
            try:
                client.request("POST",
                               f"http://{addr}:{peer.api_port}"
                               f"/api/voice/relay",
                               body={"for": client_id, "etype": etype,
                                     "payload": payload}, timeout=6)
            except Exception:
                pass    # honest failure: the phone's host is unreachable

    def _token_provider(self):
        from .peers import PeerClient
        return self.config.get("auth_token") or ""

    def _send(self, target: dict, etype: str, payload) -> None:
        if target.get("kind") == "mobile":
            self._publish_phone(target.get("client_id") or "", etype,
                                payload, target.get("host_device_id") or "")
        else:
            self._publish_computer(target.get("device_id") or "", etype,
                                   payload)

    def _fanout_except(self, call: Call, except_key: str, etype: str,
                       payload) -> None:
        me = self.identity() or {}
        me_id = me.get("device_id")
        for t in call.targets:
            key = t.get("device_id") or t.get("client_id")
            if key == except_key:
                continue
            # a mobile hosted by THIS companion is local; a mobile hosted
            # elsewhere gets its event via that host (relay); computers
            # hosted elsewhere receive via the "voice" topic only if they
            # stream from THIS companion — so remote computers must also
            # be relayed through their host
            if t.get("kind") != "mobile" and \
                    t.get("host_device_id") and \
                    t["host_device_id"] != me_id:
                self._publish_phone(t.get("client_id") or t["device_id"],
                                    etype, payload, t["host_device_id"])
            else:
                self._send(t, etype, payload)

    # ------------------------------------------------------------------
    # calls
    # ------------------------------------------------------------------
    def call(self, body: dict) -> dict:
        if not self.enabled:
            raise VoiceError(403, "voice_disabled",
                             "Voice is disabled on this companion")
        mode = str(body.get("mode") or "call")
        if mode not in ("call", "room", "ptt"):
            raise VoiceError(400, "bad_mode", "Unknown voice mode")
        origin = body.get("origin") or {}
        targets = body.get("targets") or []
        if any(not isinstance(t, dict) for t in targets):
            raise VoiceError(400, "bad_targets",
                             "Each target must be an object with "
                             "kind + device_id or client_id")
        if mode == "call" and len(targets) != 1:
            raise VoiceError(400, "bad_targets",
                             "A direct call needs exactly one target")
        for t in targets:
            if not (t.get("device_id") or t.get("client_id")):
                raise VoiceError(400, "bad_targets",
                                 "Each target needs device_id or client_id")
            if t.get("kind") not in ("computer", "mobile"):
                t["kind"] = "computer" if t.get("device_id") else "mobile"
        with self._lock:
            # simultaneous-call handling: one ringing call per target pair
            for c in self._calls.values():
                if c.state == "ringing" and mode == "call" and \
                        set(c.participants()) & set(
                            [t.get("device_id") or t.get("client_id")
                             for t in targets]) and \
                        origin.get("device_id") in c.participants():
                    raise VoiceError(409, "already_ringing",
                                     "A call is already ringing with this "
                                     "participant; answer or decline first")
            call = Call("vc_" + secrets.token_hex(6), mode, origin, targets,
                        room=str(body.get("room") or ""))
            self._calls[call.id] = call
            if len(self._calls) > MAX_CALLS:
                for k in sorted(self._calls,
                                key=lambda k: self._calls[k].created)[:-MAX_CALLS]:
                    self._calls.pop(k, None)
        me = self.identity() or {}
        for t in call.targets:
            # local targets get a direct publish; remote hosts get the
            # invite relayed (they create a MIRRORED call record so their
            # UI can ring, and forward actions back to this origin)
            t_host = t.get("host_device_id") or ""
            peer = self.registry.get(t.get("device_id") or "") \
                if t.get("kind") == "computer" else None
            remote = peer is not None and \
                peer.device_id != me.get("device_id") and \
                bool(peer.addresses)
            if t.get("kind") == "computer" and \
                    not t.get("device_id") == me.get("device_id") and \
                    (not t_host or t_host == me.get("device_id")) and \
                    remote:
                self._relay_to_host(peer, {
                    "etype": "voice_invite",
                    "payload": call.public(),
                    "origin_host": me.get("device_id"),
                    "for_device": t.get("device_id"),
                })
                continue
            # unresolvable target: publish locally (honest; the desktop
            # listens on its own companion) rather than dropping silently
            self._send(t, "voice_invite", call.public())
        self._emit("voice_call_started", call)
        return {"ok": True, "call": call.public()}

    def _relay_to_host(self, peer, body: dict) -> None:
        from .peers import PeerClient
        client = PeerClient(self._token_provider)
        try:
            client.request("POST",
                           f"http://{peer.addresses[0]}:{peer.api_port}"
                           f"/api/voice/relay", body=body, timeout=6)
        except Exception:
            pass    # honest failure — the UI shows the invite only locally

    def _forward_if_mirrored(self, call_id: str, action: str,
                             body: dict) -> Optional[dict]:
        """Mirrored call: forward the action to the origin companion."""
        with self._lock:
            call = self._calls.get(call_id)
            if call is None or not call.mirrored:
                return None
            origin_host = call.origin_host
        peer = self.registry.get(origin_host) if origin_host else None
        if not (peer and peer.addresses):
            raise VoiceError(503, "origin_unreachable",
                             "The call's origin companion is unreachable")
        from .peers import PeerClient
        client = PeerClient(self._token_provider)
        try:
            return client.request(
                "POST",
                f"http://{peer.addresses[0]}:{peer.api_port}"
                f"/api/voice/{action}", body=body, timeout=6)
        except Exception as exc:
            raise VoiceError(503, "origin_unreachable", str(exc))

    def answer(self, call_id: str, accept: bool, who: dict) -> dict:
        fwd = self._forward_if_mirrored(call_id, "answer",
                                        {"call_id": call_id,
                                         "accept": accept, "who": who})
        if fwd is not None:
            return fwd
        with self._lock:
            call = self._calls.get(call_id)
            if call is None:
                raise VoiceError(404, "no_call", "Unknown call")
            if call.state not in ("ringing", "active"):
                raise VoiceError(409, "call_state",
                                 f"Call is {call.state}")
        if accept:
            call.state = "active"
            if not call.connected:
                call.connected = _now()
            self._fanout_except(call, who.get("device_id") or
                                who.get("client_id") or "",
                                "voice_update", call.public())
        else:
            call.state = "rejected"
            call.ended = _now()
            call.ended_by = who.get("name") or "recipient"
            call.reason = "declined"
            self._fanout_except(call, "", "voice_update", call.public())
            self._record(call)
        self._emit("voice_call_update", call)
        return {"ok": True, "call": call.public()}

    def signal(self, call_id: str, who: dict, kind: str, payload) -> dict:
        """Relay an opaque signaling blob (offer/answer/ice) to the OTHER
        participants.  The companion never inspects it beyond size."""
        fwd = self._forward_if_mirrored(call_id, "signal",
                                        {"call_id": call_id, "who": who,
                                         "kind": kind, "payload": payload})
        if fwd is not None:
            return fwd
        with self._lock:
            call = self._calls.get(call_id)
            if call is None:
                raise VoiceError(404, "no_call", "Unknown call")
            if call.signal_count >= MAX_SIGNALS_PER_CALL:
                raise VoiceError(429, "signal_flood",
                                 "Too many signaling messages for this call")
            call.signal_count += 1
        if len(json.dumps(payload or "")) > 64 * 1024:
            raise VoiceError(413, "signal_too_large",
                             "Signaling payload exceeds 64 KiB")
        self._fanout_except(call, who.get("device_id") or
                            who.get("client_id") or "", "voice_signal",
                            {"kind": kind, "from": who, "payload": payload})
        return {"ok": True}

    def end(self, call_id: str, who: dict, reason: str = "hangup") -> dict:
        fwd = self._forward_if_mirrored(call_id, "end",
                                        {"call_id": call_id, "who": who,
                                         "reason": reason})
        if fwd is not None:
            return fwd
        with self._lock:
            call = self._calls.get(call_id)
            if call is None:
                raise VoiceError(404, "no_call", "Unknown call")
            if call.state in ("ended", "missed", "rejected"):
                return {"ok": True, "call": call.public()}
            call.state = "ended"
            call.ended = _now()
            call.ended_by = who.get("name") or who.get("device_id") or "peer"
            call.reason = reason
        self._fanout_except(call, "", "voice_update", call.public())
        self._record(call)
        self._emit("voice_call_update", call)
        return {"ok": True, "call": call.public()}

    def relay(self, body: dict) -> dict:
        """Host-to-host voice channel: deliver an event to a client hosted
        HERE, and — for invites — create the mirrored call record so this
        companion's UI can ring and answer (v1.4.0 cross-computer calls)."""
        etype = str(body.get("etype") or "voice_invite")
        payload = body.get("payload") or {}
        origin_host = str(body.get("origin_host") or "")
        for_device = str(body.get("for_device") or "")
        if etype == "voice_invite" and payload.get("id"):
            with self._lock:
                if payload["id"] not in self._calls:
                    call = Call(payload["id"],
                                str(payload.get("mode") or "call"),
                                payload.get("origin") or {},
                                payload.get("targets") or [],
                                room=str(payload.get("room") or ""),
                                origin_host=origin_host,
                                mirrored=True)
                    call.state = str(payload.get("state") or "ringing")
                    self._calls[call.id] = call
        if for_device:
            # a computer hosted/running HERE: publish on the voice topic
            if self.sse:
                self.sse.publish("voice", {"type": etype,
                                           "for": for_device,
                                           "data": payload})
            return {"ok": True}
        cid = str(body.get("for") or "")
        if self.sse:
            self.sse.publish("mobile", {"type": "voice", "for": cid,
                                        "data": payload})
        return {"ok": True}

    def state(self) -> dict:
        me = self.identity() or {}
        with self._lock:
            active = [c.public() for c in self._calls.values()
                      if c.state in ("ringing", "active")]
            rooms = [dict(r, id=rid) for rid, r in self._rooms.items()]
        return {"ok": True, "enabled": self.enabled,
                "device_id": me.get("device_id"),
                "calls": active, "rooms": rooms}

    def history_list(self, limit: int = 100) -> dict:
        try:
            with open(self._hist_path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            lines = []
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except (ValueError, TypeError):
                continue
        return {"ok": True, "history": list(reversed(out))}

    # ------------------------------------------------------------------
    # rooms (group voice)
    # ------------------------------------------------------------------
    def room_join(self, body: dict, who: dict) -> dict:
        room = str(body.get("room") or "").strip()[:60]
        if not room:
            raise VoiceError(400, "bad_room", "Room name required")
        with self._lock:
            r = self._rooms.setdefault(room, {"name": room, "members": [],
                                              "created": _now()})
            key = who.get("device_id") or who.get("client_id")
            if key and key not in r["members"]:
                r["members"].append(key)
            r["ts"] = _now()
        self._publish_room(room, "voice_room", {"room": room, "event": "join",
                                                "who": who,
                                                "members": r["members"]})
        return {"ok": True, "room": r}

    def room_leave(self, body: dict, who: dict) -> dict:
        room = str(body.get("room") or "").strip()
        with self._lock:
            r = self._rooms.get(room)
            if r is None:
                return {"ok": True, "room": None}
            key = who.get("device_id") or who.get("client_id")
            if key in r["members"]:
                r["members"].remove(key)
            r["ts"] = _now()
            empty = not r["members"]
            if empty:
                self._rooms.pop(room, None)
        self._publish_room(room, "voice_room", {"room": room,
                                                "event": "leave",
                                                "who": who,
                                                "members": r["members"]
                                                if not empty else []})
        return {"ok": True, "room": None if empty else r}

    def _publish_room(self, room: str, etype: str, payload) -> None:
        if self.sse:
            self.sse.publish("voice", {"type": etype, "for": None,
                                       "data": dict(payload, room=room)})
            # phones in the room (members are client ids too)
            for c in (getattr(self.chat, "mobile", None) or
                      None) and self.chat.mobile.all() or []:
                if c.client_id in payload.get("members", []):
                    self.sse.publish("mobile", {"type": "voice",
                                                "for": c.client_id,
                                                "data": dict(payload,
                                                             room=room)})

    # ------------------------------------------------------------------
    # housekeeping
    # ------------------------------------------------------------------
    def _ring_watchdog(self) -> None:
        """Expire unanswered rings into 'missed' with a GhostChat record."""
        import time as _t
        while not self._stop.wait(5.0):
            me = self.identity() or {}
            expired = []
            with self._lock:
                for call in list(self._calls.values()):
                    if call.state == "ringing" and \
                            _now() - call.created > RING_TIMEOUT_S * 1000:
                        call.state = "missed"
                        call.ended = _now()
                        call.reason = "no answer"
                        expired.append(call)
            for call in expired:
                self._fanout_except(call, "", "voice_update", call.public())
                self._record(call)
                self._missed_chat_message(call)
                self._emit("voice_call_update", call)

    def _missed_chat_message(self, call: Call) -> None:
        if not self.chat:
            return
        try:
            self.chat.system_event(
                f"Missed voice call from {call.origin.get('name') or 'a device'}",
                targets=[t for t in call.targets],
                kind="missed_call", call_id=call.id)
        except Exception:
            if self.log:
                self.log.warning("missed-call chat record failed")

    def _record(self, call: Call) -> None:
        entry = dict(call.public(), ts=call.ended or _now(),
                     iso=iso_now())
        try:
            with open(self._hist_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _emit(self, etype: str, call: Call) -> None:
        if self.events:
            try:
                self.events.emit("voice", etype,
                                 data={"call_id": call.id,
                                       "state": call.state})
            except Exception:
                pass

    def shutdown(self) -> None:
        self._stop.set()
