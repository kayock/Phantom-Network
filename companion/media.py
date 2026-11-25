"""Phantom Media (v1.4.0) — lightweight distributed media sessions.

Rooms hold shared queues built from files inside AUTHORIZED directories
(the same PathSandbox grants as the File Commander — no path escapes).
State lives per room in ``media_rooms.json``; the room's "owner" is the
companion where the queue was created; every browser schedules playback
locally using measured clock offsets (the Media Party scheduler, evolved).

Streaming: browsers play files directly from the OWNING companion via
``/api/media/stream`` (HTTP Range supported — byte seeking works; the
companion never transcodes or proxies long-lived media, so a device plays
what its own browser can decode).

Honesty: synchronization precision is ±50–150 ms on a LAN (documented);
crash recovery resumes with an approximate position and says so.

Non-goals (unchanged): no heavyweight media server, no transcoding, no
metadata scraping, no DRM circumvention, no acquisition services.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from typing import Dict, List, Optional

from .util import now_ms, iso_now, atomic_write_json, read_json

MAX_ROOMS = 24
MAX_QUEUE_ITEMS = 500
HISTORY_CAP = 200
AUDIBLE_MAX_VOLUME = 0.8          # matches the audio amplitude ceiling

MEDIA_EXTS = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".flac": "audio/flac", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".oga": "audio/ogg", ".opus": "audio/opus", ".weba": "audio/webm",
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
    ".mkv": "video/x-matroska", ".mov": "video/quicktime",
}


class MediaError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def mime_for(name: str) -> Optional[str]:
    return MEDIA_EXTS.get(os.path.splitext(name)[1].lower())


class MediaEngine:
    def __init__(self, data_dir: str, config, registry, sandbox, identity,
                 sse, events, log=None):
        self.data_dir = data_dir
        self.config = config
        self.registry = registry
        self.sandbox = sandbox
        self.identity = identity
        self.sse = sse
        self.events = events
        self.log = log
        self._lock = threading.RLock()
        self._path = os.path.join(data_dir, "media_rooms.json")
        self._hist_path = os.path.join(data_dir, "media_history.jsonl")
        self._state: dict = read_json(self._path, None) or {"rooms": {}}

    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.config.get("media_enabled", True))

    def _persist(self) -> None:
        atomic_write_json(self._path, self._state)

    def _publish(self, etype: str, data: dict) -> None:
        if self.sse:
            self.sse.publish("media_event", {"type": etype, "for": None,
                                             "data": data})

    # ------------------------------------------------------------------
    def rooms(self) -> dict:
        me = self.identity()
        with self._lock:
            out = []
            for rid, r in self._state["rooms"].items():
                out.append(dict(r, id=rid,
                                owned_here=r.get("owner") ==
                                me.get("device_id")))
        return {"ok": True, "rooms": out, "enabled": self.enabled,
                "max_volume": AUDIBLE_MAX_VOLUME}

    def _room(self, room_id: str) -> dict:
        r = self._state["rooms"].get(room_id)
        if r is None:
            raise MediaError(404, "no_room", "Unknown media room")
        return r

    def room_create(self, body: dict) -> dict:
        name = str(body.get("name") or "").strip()[:60]
        if not name:
            raise MediaError(400, "bad_room", "Room name required")
        with self._lock:
            if len(self._state["rooms"]) >= MAX_ROOMS:
                raise MediaError(409, "too_many_rooms",
                                 f"Room cap ({MAX_ROOMS}) reached")
            rid = "mr_" + secrets.token_hex(4)
            self._state["rooms"][rid] = {
                "name": name,
                "owner": self.identity().get("device_id"),
                "members": [self.identity().get("device_id")],
                "queue": [], "playing": False, "position": 0,
                "item_started": 0, "paused_at": 0,
                "repeat": "off", "shuffle": False,
                "volumes": {}, "created": now_ms(),
            }
            self._persist()
        self._publish("room_created", {"room_id": rid})
        return {"ok": True, "room_id": rid}

    def room_delete(self, room_id: str) -> dict:
        with self._lock:
            self._state["rooms"].pop(room_id, None)
            self._persist()
        self._publish("room_deleted", {"room_id": room_id})
        return {"ok": True}

    def room_join(self, body: dict) -> dict:
        with self._lock:
            r = self._room(str(body.get("room_id") or ""))
            me = self.identity().get("device_id")
            if me not in r["members"]:
                r["members"].append(me)
                self._persist()
        self._publish("room_updated", {"room_id": body.get("room_id"),
                                       "members": r["members"]})
        return {"ok": True, "room": dict(r, id=body.get("room_id"))}

    def room_leave(self, body: dict) -> dict:
        with self._lock:
            r = self._room(str(body.get("room_id") or ""))
            me = self.identity().get("device_id")
            if me in r["members"]:
                r["members"].remove(me)
                self._persist()
        self._publish("room_updated", {"room_id": body.get("room_id"),
                                       "members": r["members"]})
        return {"ok": True}

    # ------------------------------------------------------------------
    # queue
    # ------------------------------------------------------------------
    def enqueue(self, body: dict) -> dict:
        """Add an authorized file to a room queue.  The file must be
        readable through THIS companion's authorized shares (the browser
        sends a path it browsed via /api/fs/list); the owning stream URL
        is computed for all members."""
        with self._lock:
            rid = str(body.get("room_id") or "")
            r = self._room(rid)
            path = str(body.get("path") or "")
            fh, size, name = self._guard(path)
            if len(r["queue"]) >= MAX_QUEUE_ITEMS:
                raise MediaError(409, "queue_full",
                                 f"Queue cap ({MAX_QUEUE_ITEMS}) reached")
            item = {
                "id": "mi_" + secrets.token_hex(5),
                "path": path, "name": name, "size": size,
                "mime": mime_for(name) or "application/octet-stream",
                "added_by": str(body.get("added_by") or
                                self.identity().get("device_id")),
                "added_at": now_ms(),
                "duration_s": body.get("duration_s"),
                "host_device_id": self.identity().get("device_id"),
                "base": str(body.get("base") or ""),   # origin base URL
            }
            pos = int(body.get("position", -1))
            if 0 <= pos <= len(r["queue"]):
                r["queue"].insert(pos, item)
            else:
                r["queue"].append(item)
            self._persist()
        self._publish("queue_updated", {"room_id": rid, "queue": r["queue"]})
        return {"ok": True, "item": item}

    def _guard(self, path: str):
        """Open an authorized file for streaming (no duplicate copy)."""
        from .fsops import open_download, FsError
        try:
            fh, size, name = open_download(self.sandbox, path)
            return fh, size, name
        except FsError as exc:
            raise MediaError(exc.status, exc.code, exc.message)

    def stream_url_for(self, room_id: str, item: dict,
                       viewer_device: str) -> str:
        """URL a viewer device should use to stream this item.  Same-host
        viewers stream from the local companion; others from the item's
        owning companion (no duplication — the authorized share is read
        by the owner and served byte-for-byte over HTTP Range)."""
        me = self.identity().get("device_id")
        if item.get("host_device_id") == me:
            return f"/api/media/stream?path={item['path']}"
        host = self.registry.get(item.get("host_device_id") or "")
        if host and host.addresses:
            return (f"http://{host.addresses[0]}:{host.api_port}"
                    f"/api/media/stream?path={item['path']}")
        return ""

    def dequeue(self, body: dict) -> dict:
        with self._lock:
            rid = str(body.get("room_id") or "")
            r = self._room(rid)
            iid = str(body.get("item_id") or "")
            r["queue"] = [x for x in r["queue"] if x["id"] != iid]
            if r["position"] >= len(r["queue"]):
                r["position"] = max(0, len(r["queue"]) - 1)
            self._persist()
        self._publish("queue_updated", {"room_id": rid, "queue": r["queue"]})
        return {"ok": True}

    def reorder(self, body: dict) -> dict:
        with self._lock:
            rid = str(body.get("room_id") or "")
            r = self._room(rid)
            order = body.get("order") or []
            by_id = {x["id"]: x for x in r["queue"]}
            if set(order) != set(by_id):
                raise MediaError(400, "bad_order",
                                 "Order must be a permutation of the queue")
            r["queue"] = [by_id[i] for i in order]
            self._persist()
        self._publish("queue_updated", {"room_id": rid, "queue": r["queue"]})
        return {"ok": True}

    # ------------------------------------------------------------------
    # playback control
    # ------------------------------------------------------------------
    def control(self, body: dict) -> dict:
        with self._lock:
            rid = str(body.get("room_id") or "")
            r = self._room(rid)
            action = str(body.get("action") or "")
            now = now_ms()
            if action == "play":
                if not r["queue"]:
                    raise MediaError(409, "empty_queue",
                                     "Nothing queued in this room")
                if not r["playing"]:
                    r["playing"] = True
                    r["item_started"] = now
                    r["paused_at"] = r["paused_at"] or 0
                elif body.get("position_s") is not None:
                    # seek while playing: restart the clock at the offset
                    r["paused_at"] = int(body["position_s"] * 1000)
                    r["item_started"] = now
            elif action == "pause":
                if r["playing"]:
                    elapsed = (now - r["item_started"]) / 1000
                    r["paused_at"] = round(r["paused_at"] / 1000
                                           + elapsed, 3)
                    r["playing"] = False
            elif action == "stop":
                r["playing"] = False
                r["paused_at"] = 0
                r["position"] = 0
                r["item_started"] = 0
            elif action == "next":
                self._advance(r, +1)
            elif action == "prev":
                self._advance(r, -1)
            elif action == "seek":
                r["paused_at"] = int(float(body.get("position_s", 0))
                                     * 1000)
                r["item_started"] = now
            elif action == "repeat":
                r["repeat"] = str(body.get("mode") or "off")
            elif action == "shuffle":
                r["shuffle"] = bool(body.get("on"))
            elif action == "volume":
                dev = str(body.get("device") or self.identity()
                          .get("device_id"))
                level = float(body.get("level", AUDIBLE_MAX_VOLUME))
                r["volumes"][dev] = max(0.0, min(AUDIBLE_MAX_VOLUME,
                                                 level))
            else:
                raise MediaError(400, "bad_action", f"Unknown action {action}")
            self._persist()
            pub = self._public_state(rid, r)
        self._publish("media_state", dict(pub, room_id=rid))
        self._record_history(rid, action, pub)
        return {"ok": True, "room": pub}

    def _advance(self, r: dict, delta: int) -> None:
        if not r["queue"]:
            return
        nxt = r["position"] + delta
        if nxt >= len(r["queue"]):
            nxt = 0 if r.get("repeat") == "all" else len(r["queue"]) - 1
        elif nxt < 0:
            # prev at the first track: wrap under repeat-all, else stay
            nxt = len(r["queue"]) - 1 if r.get("repeat") == "all" else 0
        r["position"] = max(0, nxt)
        r["paused_at"] = 0
        r["item_started"] = now_ms()
        r["playing"] = True

    def _public_state(self, rid: str, r: dict) -> dict:
        item = r["queue"][r["position"]] if r["queue"] else None
        me = self.identity()
        out = {
            "name": r["name"], "members": list(r["members"]),
            "queue": [dict(x, stream_url=self.stream_url_for(rid, x,
                                                             me["device_id"]))
                      for x in r["queue"]],
            "position": r["position"], "playing": r["playing"],
            "item": item, "repeat": r["repeat"], "shuffle": r["shuffle"],
            "volumes": dict(r["volumes"]),
            "max_volume": AUDIBLE_MAX_VOLUME,
            "server_now": now_ms(),
        }
        if r["playing"] and item:
            out["offset_s"] = round(r["paused_at"] / 1000
                                    + (now_ms() - r["item_started"]) / 1000,
                                    3)
        else:
            out["offset_s"] = round(r["paused_at"] / 1000, 3)
        return out

    def state(self, room_id: str) -> dict:
        with self._lock:
            r = self._room(room_id)
            pub = self._public_state(room_id, r)
        return {"ok": True, "room": pub}

    def history(self, room_id: str = "", limit: int = 100) -> dict:
        try:
            with open(self._hist_path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            lines = []
        out = []
        for line in reversed(lines[-limit:]):
            try:
                e = json_load(line)
                if room_id and e.get("room_id") != room_id:
                    continue
                out.append(e)
            except Exception:
                continue
        return {"ok": True, "history": out}

    def _record_history(self, rid: str, action: str, pub: dict) -> None:
        try:
            entry = {"ts": now_ms(), "iso": iso_now(), "room_id": rid,
                     "action": action,
                     "item": (pub.get("item") or {}).get("name", ""),
                     "by": self.identity().get("device_id")}
            with open(self._hist_path, "a", encoding="utf-8") as fh:
                fh.write(json_dumps(entry) + "\n")
            # cap the file
            with open(self._hist_path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
            if len(lines) > HISTORY_CAP:
                with open(self._hist_path, "w", encoding="utf-8") as fh:
                    fh.writelines(lines[-HISTORY_CAP:])
        except OSError:
            pass


def json_load(line: str):
    import json as _j
    return _j.loads(line)


def json_dumps(obj) -> str:
    import json as _j
    return _j.dumps(obj, ensure_ascii=False)
