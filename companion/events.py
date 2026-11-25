"""Event Center backend: in-memory event bus + persistent JSONL event log.

Every notable occurrence (device up/down, transfers, messages, audio, errors)
is recorded as an :class:`Event` and:

1. published to live subscribers (the SSE hub forwards to browser clients),
2. appended to ``<data_dir>/events.jsonl`` (ring file, oldest trimmed),
3. counted in rolling per-type statistics for the diagnostics page.

Events are strictly local: nothing is transmitted to any external service.
"""

from __future__ import annotations

import json
import os
import threading
from collections import deque
from typing import Callable, Dict, List, Optional

from .util import iso_now, new_id, now_ms

MAX_PERSISTED_EVENTS = 5000

EVENT_TYPES = [
    "device_connected", "device_disconnected", "device_discovered",
    "device_lost", "transfer_started", "transfer_progress", "transfer_paused",
    "transfer_resumed", "transfer_completed", "transfer_failed",
    "verification_failed", "message", "message_delivered", "audio_playback",
    "audio_stopped", "intercom_start", "intercom_stop", "companion_error",
    "config_changed", "network_changed", "sync_completed", "system",
]


class Event:
    __slots__ = ("id", "ts", "iso", "type", "severity", "source", "text", "data")

    def __init__(self, type_: str, text: str, severity: str = "info",
                 source: str = "self", data: Optional[dict] = None,
                 ts: Optional[int] = None):
        self.id = new_id("ev")
        self.ts = ts if ts is not None else now_ms()
        self.iso = iso_now()
        self.type = type_ if type_ in EVENT_TYPES else "system"
        self.severity = severity if severity in ("info", "warn", "error") else "info"
        self.source = source
        self.text = text
        self.data = data or {}

    def to_dict(self) -> dict:
        return {
            "id": self.id, "ts": self.ts, "iso": self.iso, "type": self.type,
            "severity": self.severity, "source": self.source,
            "text": self.text, "data": self.data,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        ev = cls(d.get("type", "system"), d.get("text", ""),
                 d.get("severity", "info"), d.get("source", "self"),
                 d.get("data"), d.get("ts"))
        ev.id = d.get("id", ev.id)
        ev.iso = d.get("iso", ev.iso)
        return ev


class EventBus:
    """Thread-safe event publisher with persistence and in-memory history."""

    def __init__(self, data_dir: str, history_limit: int = 2000):
        self._lock = threading.RLock()
        self._path = os.path.join(data_dir, "events.jsonl")
        self._memory: deque = deque(maxlen=history_limit)
        self._subscribers: List[Callable[[dict], None]] = []
        self._stats: Dict[str, int] = {}
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        """Replay the persisted log into memory (newest last in file)."""
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._memory.append(Event.from_dict(json.loads(line)))
                    except ValueError:
                        continue
        except OSError:
            pass

    def _persist(self, ev: Event) -> None:
        try:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev.to_dict(), ensure_ascii=False) + "\n")
            # trim when the file grows beyond the retention cap
            if os.path.getsize(self._path) > MAX_PERSISTED_EVENTS * 400:
                self._rewrite()
        except OSError:
            pass

    def _rewrite(self) -> None:
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                for old in list(self._memory)[-MAX_PERSISTED_EVENTS:]:
                    fh.write(json.dumps(old.to_dict(), ensure_ascii=False) + "\n")
            os.replace(tmp, self._path)
        except OSError:
            pass

    # ------------------------------------------------------------------
    def subscribe(self, callback: Callable[[dict], None]) -> Callable[[], None]:
        """Subscribe a live listener; returns an unsubscribe function."""
        with self._lock:
            self._subscribers.append(callback)
        return lambda: self._unsubscribe(callback)

    def _unsubscribe(self, callback) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def emit(self, type_: str, text: str, severity: str = "info",
             source: str = "self", data: Optional[dict] = None) -> Event:
        ev = Event(type_, text, severity, source, data)
        with self._lock:
            self._memory.append(ev)
            self._stats[ev.type] = self._stats.get(ev.type, 0) + 1
        self._persist(ev)
        payload = ev.to_dict()
        for cb in list(self._subscribers):
            try:
                cb(payload)
            except Exception:
                pass
        return ev

    # ------------------------------------------------------------------
    def query(self, types: Optional[List[str]] = None, search: str = "",
              since_ts: int = 0, limit: int = 500,
              severity: Optional[str] = None) -> List[dict]:
        """Filter events (newest first).  Filters are AND-combined."""
        with self._lock:
            items = list(self._memory)
        needle = search.lower().strip()
        out = []
        for ev in reversed(items):
            if ev.ts < since_ts:
                continue
            if types and ev.type not in types:
                continue
            if severity and ev.severity != severity:
                continue
            if needle and needle not in ev.text.lower() \
                    and needle not in ev.type.lower():
                continue
            out.append(ev.to_dict())
            if len(out) >= limit:
                break
        return out

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)
