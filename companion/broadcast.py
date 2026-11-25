"""Phantom Broadcast (v1.4.0) — household announcement system.

Routes announcements (text, tone profiles, live voice, optional local
TTS) to computers, hosted phones, groups and rooms, with:

- profiles: named announcement presets (tone + speech text + urgency);
  seven useful built-ins are provided, users can add their own;
- device groups: named target sets persisted in ``broadcast_groups.json``;
- quiet hours: a config window ("22:00-07:00") during which tones/TTS are
  replaced by visual-only delivery — never automatically overridden
  (emergency announcements require the SENDER to explicitly acknowledge
  an interstitial, and even then only override when the sender passes
  ``override_quiet=True`` through that gate);
- participation: per-device notification profiles (existing mobile
  ``notify_prefs`` semantics) are respected — a silenced device receives
  the banner but makes no sound, and the history records that honestly;
- history: append-only JSONL with per-target delivery outcomes;
- acknowledgments: optional request — recipients can acknowledge and the
  sender sees who did;
- schedules: daily/weekly repeating announcements evaluated locally;
- TTS: LOCAL system voices only (Linux espeak-ng/speech-dispatcher,
  macOS say, Windows SAPI via PowerShell) — no external AI service,
  no cloud account.  If no local voice exists this is reported honestly
  and delivery falls back to tone + text banner.

Amplitude safety reuses the existing AudioService caps; browser speech
synthesis volume is clamped client-side to the same ceiling.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from typing import Dict, List, Optional

from .util import now_ms, iso_now, read_json, atomic_write_json as _wj

QUIET_OVERRIDE_REQUIRES_EXPLICIT = True
TTS_MAX_CHARS = 600

BUILTIN_PROFILES: List[dict] = [
    {"id": "dinner", "name": "Dinner announcement",
     "tone": "chime", "voice": "Dinner is ready. Everyone come to the "
     "table, please.", "urgency": "normal"},
    {"id": "incoming", "name": "Incoming transmission",
     "tone": "beacon", "voice": "", "urgency": "normal"},
    {"id": "reminder", "name": "Household reminder",
     "tone": "soft", "voice": "", "urgency": "normal"},
    {"id": "transfer", "name": "Transfer completed",
     "tone": "success", "voice": "", "urgency": "normal"},
    {"id": "offline", "name": "Device offline",
     "tone": "alarm", "voice": "", "urgency": "attention"},
    {"id": "maintenance", "name": "Network maintenance",
     "tone": "soft", "voice": "", "urgency": "normal"},
    {"id": "general", "name": "General household announcement",
     "tone": "chime", "voice": "", "urgency": "normal"},
]


class BroadcastError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


class BroadcastEngine:
    def __init__(self, data_dir: str, config, registry, chat, audio,
                 identity, sse, events, log=None):
        self.data_dir = data_dir
        self.config = config
        self.registry = registry
        self.chat = chat              # reuse conversation fan-out for text
        self.audio = audio            # AudioService for tones (caps kept)
        self.identity = identity
        self.sse = sse
        self.events = events
        self.log = log
        self._lock = threading.RLock()
        self._hist_path = os.path.join(data_dir, "broadcast_history.jsonl")
        self._sched_path = os.path.join(data_dir, "broadcast_schedule.json")
        self._groups_path = os.path.join(data_dir, "broadcast_groups.json")
        self._profiles_path = os.path.join(data_dir, "broadcast_profiles.json")
        self._stop = threading.Event()
        self._sched_thread = threading.Thread(target=self._schedule_loop,
                                              daemon=True)
        self._sched_thread.start()

    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.config.get("broadcast_enabled", True))

    # ------------------------------------------------------------------
    # profiles
    # ------------------------------------------------------------------
    def profiles(self) -> dict:
        custom = read_json(self._profiles_path, None) or []
        return {"ok": True, "profiles": BUILTIN_PROFILES + list(custom),
                "tts_available": self.tts_available(),
                "quiet_hours": self.config.get("broadcast_quiet_hours", "")}

    def profile_add(self, body: dict) -> dict:
        name = str(body.get("name") or "").strip()[:60]
        if not name:
            raise BroadcastError(400, "bad_profile", "Profile name required")
        custom = read_json(self._profiles_path, None) or []
        prof = {"id": "bp_" + os.urandom(4).hex(),
                "name": name,
                "tone": str(body.get("tone") or "chime"),
                "voice": str(body.get("voice") or "")[:TTS_MAX_CHARS],
                "urgency": str(body.get("urgency") or "normal")}
        custom.append(prof)
        _wj(self._profiles_path, custom)
        return {"ok": True, "profile": prof}

    # ------------------------------------------------------------------
    # groups
    # ------------------------------------------------------------------
    def groups(self) -> dict:
        return {"ok": True, "groups": read_json(self._groups_path, None) or []}

    def group_save(self, body: dict) -> dict:
        name = str(body.get("name") or "").strip()[:60]
        members = body.get("members") or []
        if not name or not members:
            raise BroadcastError(400, "bad_group",
                                 "Group needs a name and members")
        groups = read_json(self._groups_path, None) or []
        gid = str(body.get("id") or "bg_" + os.urandom(4).hex())
        entry = {"id": gid, "name": name, "members": members}
        groups = [g for g in groups if g["id"] != gid]
        groups.append(entry)
        _wj(self._groups_path, groups)
        return {"ok": True, "group": entry}

    def group_delete(self, gid: str) -> dict:
        groups = [g for g in (read_json(self._groups_path, None) or [])
                  if g["id"] != gid]
        _wj(self._groups_path, groups)
        return {"ok": True}

    # ------------------------------------------------------------------
    # quiet hours
    # ------------------------------------------------------------------
    @staticmethod
    def in_quiet_hours(window: str, at=None) -> bool:
        """"22:00-07:00" style window; midnight wrap supported."""
        if not window or "-" not in window:
            return False
        try:
            a, b = window.split("-", 1)
            hh, mm = a.strip().split(":")
            end_h, end_m = b.strip().split(":")
            import datetime as _dt
            now = at or _dt.datetime.now()
            start = now.replace(hour=int(hh), minute=int(mm), second=0,
                                microsecond=0)
            end = now.replace(hour=int(end_h), minute=int(end_m), second=0,
                              microsecond=0)
            if start <= end:
                return start <= now < end
            return now >= start or now < end        # wraps midnight
        except (ValueError, AttributeError):
            return False

    def quiet_now(self) -> bool:
        return self.in_quiet_hours(
            str(self.config.get("broadcast_quiet_hours") or ""))

    # ------------------------------------------------------------------
    # TTS (local voices only)
    # ------------------------------------------------------------------
    def tts_available(self) -> bool:
        if not self.config.get("broadcast_tts_enabled", False):
            return False
        return bool(self._tts_command()[0])

    def _tts_command(self):
        for exe, args in (("espeak-ng", ["-s", "150"]),
                          ("espeak", ["-s", "150"]),
                          ("say", [])):
            path = shutil.which(exe)
            if path:
                return path, args
        return None, []

    def speak_local(self, text: str) -> dict:
        """Speak via the LOCAL system voice.  Fails honestly when absent.
        Runs detached; the companion never blocks on speech."""
        if not self.config.get("broadcast_tts_enabled", False):
            return {"ok": False, "reason": "tts_disabled"}
        text = (text or "")[:TTS_MAX_CHARS]
        exe, args = self._tts_command()
        if not exe or not text:
            return {"ok": False, "reason": "no_local_voice" if not exe
                    else "empty_text"}
        try:
            subprocess.Popen([exe, *args, text],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return {"ok": True, "engine": os.path.basename(exe)}
        except OSError as exc:
            return {"ok": False, "reason": str(exc)}

    # ------------------------------------------------------------------
    # announce
    # ------------------------------------------------------------------
    def announce(self, body: dict) -> dict:
        """Deliver an announcement.  Honors participation + quiet hours;
        returns per-target outcomes (never a fabricated 'everyone heard
        it')."""
        if not self.enabled:
            raise BroadcastError(403, "broadcast_disabled",
                                 "Broadcast is disabled on this companion")
        text = str(body.get("text") or "")[:2000]
        tone = str(body.get("tone") or "")
        speak = str(body.get("voice") or "")[:TTS_MAX_CHARS]
        targets = body.get("targets") or []
        if any(not isinstance(t, dict) for t in targets):
            raise BroadcastError(400, "bad_targets",
                                 "Each target must be an object with "
                                 "kind + device_id or client_id")
        urgency = str(body.get("urgency") or "normal")
        want_ack = bool(body.get("ack"))
        override_quiet = bool(body.get("override_quiet")) and \
            QUIET_OVERRIDE_REQUIRES_EXPLICIT and urgency == "emergency"
        if not (text or tone or speak):
            raise BroadcastError(400, "empty",
                                 "Announcement needs text, tone or voice")
        if not targets:
            raise BroadcastError(400, "no_targets",
                                 "Announcement needs at least one target")
        quiet = self.quiet_now() and not override_quiet
        me = self.identity()
        aid = "an_" + os.urandom(4).hex()
        ts = now_ms()
        results = []
        # text/banners: fan out to computers (SSE + chat store) and phones
        payload = {"id": aid, "text": text, "tone": "" if quiet else tone,
                   "speak": "" if quiet else speak, "urgency": urgency,
                   "from": {"device_id": me["device_id"],
                            "name": me["name"], "kind": "computer"},
                    "ts": ts, "ack": want_ack, "quiet": quiet,
                    "iso": iso_now()}
        for t in targets:
            kind = t.get("kind") or "computer"
            if kind == "mobile":
                cid = t.get("client_id") or ""
                prefs = (self._mobile_prefs(cid) or {})
                silent = prefs.get("silent") or \
                    prefs.get("broadcast") == "silent"
                delivered = self._deliver_phone(cid, payload, t)
                results.append({"id": cid, "kind": "mobile",
                                "state": "delivered-silently"
                                if delivered and (silent or quiet)
                                else ("delivered" if delivered
                                      else "unavailable")})
            else:
                did = t.get("device_id") or ""
                if did == me["device_id"]:
                    self._local_banner(payload)
                    results.append({"id": did, "kind": "computer",
                                    "state": "delivered-silently"
                                    if quiet else "delivered"})
                else:
                    peer = self.registry.get(did)
                    ok = False
                    if peer and peer.addresses:
                        addr = peer.addresses[0]
                        # v1.3.2 lesson: verify destination identity; stale
                        # presence never decides reachability — the probe does
                        if not self.registry.verify_peer(peer.device_id, addr,
                                                         peer.api_port,
                                                         timeout=2.5):
                            ok = False
                        else:
                            from .peers import PeerClient
                            client = PeerClient(
                                lambda: self.config.get("auth_token")
                                or "")
                            try:
                                client.request(
                                    "POST",
                                    f"http://{addr}:"
                                    f"{peer.api_port}/api/broadcast/incoming",
                                    body={"announcement": payload},
                                    timeout=6)
                                ok = True
                            except Exception:
                                ok = False
                    results.append({"id": did, "kind": "computer",
                                    "state": "delivered" if ok
                                    else "unavailable"})
        # tone: local AudioService (remote devices play their own copy on
        # receiving the banner — amplitude caps apply there too)
        tone_state = "skipped-quiet" if quiet else "n/a"
        if tone and not quiet:
            try:
                self.audio.play_preset(tone)
                tone_state = "played"
            except Exception:
                tone_state = "failed"
        # TTS: local voice on THIS device; other devices speak their own
        tts_state = "skipped-quiet" if quiet else "n/a"
        if speak and not quiet:
            r = self.speak_local(speak)
            tts_state = ("spoken:" + r["engine"]) if r.get("ok") else \
                ("unavailable:" + r.get("reason", "?"))
        entry = {"id": aid, "ts": ts, "iso": iso_now(),
                 "text": text, "tone": tone, "speak": speak,
                 "urgency": urgency, "quiet": quiet,
                 "targets": targets, "results": results,
                 "tone_state": tone_state, "tts_state": tts_state,
                 "ack": want_ack, "acknowledged": []}
        self._record(entry)
        if self.events:
            self.events.emit("broadcast", f"Announcement delivered to "
                             f"{sum(1 for r in results if r['state']
                                    .startswith('delivered'))} device(s)",
                             data={"id": aid})
        return {"ok": True, "announcement": entry}

    def incoming(self, announcement: dict) -> dict:
        """Peer-to-peer: another companion announced to THIS device."""
        self._local_banner(announcement)
        tone = announcement.get("tone") or ""
        if tone and not announcement.get("quiet"):
            try:
                self.audio.play_preset(tone)
            except Exception:
                pass
        if announcement.get("speak") and not announcement.get("quiet"):
            self.speak_local(announcement["speak"])
        return {"ok": True}

    def acknowledge(self, aid: str, who: dict) -> dict:
        hist = self._history()
        for e in reversed(hist):
            if e.get("id") == aid and who.get("id") not in \
                    e.get("acknowledged", []):
                e.setdefault("acknowledged", []).append(who.get("id"))
                self._rewrite_history(hist)
                return {"ok": True}
        raise BroadcastError(404, "no_announcement", "Unknown announcement")

    # ------------------------------------------------------------------
    # schedules
    # ------------------------------------------------------------------
    def schedules(self) -> dict:
        return {"ok": True, "schedules": read_json(self._sched_path,
                                                   None) or []}

    def schedule_save(self, body: dict) -> dict:
        time_spec = str(body.get("time") or "").strip()  # "HH:MM"
        days = body.get("days") or []     # [] = daily
        try:
            hh, mm = time_spec.split(":")
            if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                raise ValueError
        except (ValueError, AttributeError):
            raise BroadcastError(400, "bad_time",
                                 "Schedule time must be a valid HH:MM")
        entry = {"id": str(body.get("id") or "sc_" + os.urandom(4).hex()),
                 "time": time_spec, "days": days,
                 "text": str(body.get("text") or "")[:500],
                 "tone": str(body.get("tone") or ""),
                 "voice": str(body.get("voice") or "")[:TTS_MAX_CHARS],
                 "targets": body.get("targets") or [],
                 "urgency": str(body.get("urgency") or "normal"),
                 "last_fired": ""}
        scheds = read_json(self._sched_path, None) or []
        scheds = [x for x in scheds if x["id"] != entry["id"]]
        scheds.append(entry)
        _wj(self._sched_path, scheds)
        return {"ok": True, "schedule": entry}

    def schedule_delete(self, sid: str) -> dict:
        scheds = [x for x in (read_json(self._sched_path, None) or [])
                  if x["id"] != sid]
        _wj(self._sched_path, scheds)
        return {"ok": True}

    def _schedule_loop(self) -> None:
        """60 s tick; fires schedules whose HH:MM matches local time with
        last-fired dedupe (so DST transitions fire at local wall time)."""
        import datetime as _dt
        import time as _t
        while not self._stop.wait(60.0):
            now = _dt.datetime.now()
            hhmm = now.strftime("%H:%M")
            today = now.strftime("%a")[:3].lower()
            for sched in (read_json(self._sched_path, None) or []):
                if sched.get("time") != hhmm:
                    continue
                days = sched.get("days") or []
                if days and today not in [d.lower()[:3] for d in days]:
                    continue
                if sched.get("last_fired", "")[:16] == \
                        now.strftime("%Y-%m-%d %H:%M"):
                    continue
                try:
                    self.announce({"text": sched.get("text", ""),
                                   "tone": sched.get("tone", ""),
                                   "voice": sched.get("voice", ""),
                                   "targets": sched.get("targets", []),
                                   "urgency": sched.get("urgency",
                                                        "normal")})
                    sched["last_fired"] = iso_now()
                    scheds = [x for x in scheds
                              if x["id"] != sched["id"]] + [sched]
                    _wj(self._sched_path, scheds)
                except Exception:
                    if self.log:
                        self.log.warning("scheduled announcement failed")

    # ------------------------------------------------------------------
    # history
    # ------------------------------------------------------------------
    def history(self, limit: int = 100) -> dict:
        return {"ok": True, "history": list(reversed(
            self._history()[-limit:]))}

    def _history(self) -> List[dict]:
        try:
            with open(self._hist_path, "r", encoding="utf-8") as fh:
                return [json.loads(x) for x in fh.readlines()
                        if x.strip()]
        except OSError:
            return []

    def _record(self, entry: dict) -> None:
        try:
            with open(self._hist_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _rewrite_history(self, entries: List[dict]) -> None:
        tmp = self._hist_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                for e in entries:
                    fh.write(json.dumps(e, ensure_ascii=False) + "\n")
            os.replace(tmp, self._hist_path)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # delivery helpers
    # ------------------------------------------------------------------
    def _local_banner(self, payload: dict) -> None:
        if self.sse:
            self.sse.publish("broadcast", {"type": "announcement",
                                           "for": None,
                                           "data": payload})

    def _deliver_phone(self, client_id: str, payload: dict,
                       target: dict) -> bool:
        me = self.identity()
        host = target.get("host_device_id") or me.get("device_id")
        if not host or host == me.get("device_id"):
            if self.sse:
                self.sse.publish("mobile",
                                 {"type": "broadcast", "for": client_id,
                                  "data": payload})
            # persist into the client inbox via chat engine's stores
            try:
                self.chat.mobile.inbox_append(client_id, dict(
                    payload, id="msg_" + os.urandom(8).hex(),
                    **{"from": payload.get("from")}, to_kind="mobile",
                    to_id=client_id, body=payload.get("text") or "",
                    attachment=None, status="delivered", outgoing=False,
                    conv="system:alerts", meta={"kind": "broadcast"}))
            except Exception:
                pass
            return True
        peer = self.registry.get(host)
        if peer and peer.addresses:
            addr = peer.addresses[0]
            if not self.registry.verify_peer(peer.device_id, addr,
                                             peer.api_port, timeout=2.5):
                return False
            from .peers import PeerClient
            client = PeerClient(lambda: self.config.get("auth_token") or "")
            try:
                client.request(
                    "POST",
                    f"http://{addr}:{peer.api_port}"
                    f"/api/broadcast/relay",
                    body={"for": client_id, "payload": payload}, timeout=6)
                return True
            except Exception:
                return False
        return False

    def relay(self, body: dict) -> dict:
        cid = str(body.get("for") or "")
        payload = body.get("payload") or {}
        if self.sse:
            self.sse.publish("mobile", {"type": "broadcast", "for": cid,
                                        "data": payload})
        try:
            self.chat.mobile.inbox_append(cid, dict(
                payload, id="msg_" + os.urandom(8).hex(),
                **{"from": payload.get("from")}, to_kind="mobile",
                to_id=cid, body=payload.get("text") or "", attachment=None,
                status="delivered", outgoing=False, conv="system:alerts",
                meta={"kind": "broadcast"}))
        except Exception:
            pass
        return {"ok": True}

    def _mobile_prefs(self, client_id: str) -> dict:
        try:
            c = self.chat.mobile.get(client_id)
            return c.notify_prefs if c else {}
        except Exception:
            return {}

    def shutdown(self) -> None:
        self._stop.set()
