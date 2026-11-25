"""Monitoring alerts: optional, local, explicitly configured rules.

A rule watches one signal (CPU, RAM, disk, device presence, transfer or
companion events) and fires an alert when its condition has held for at
least ``duration_s`` and ``cooldown_s`` has passed since the previous fire.
Delivery channels are honest and local: an Event Center entry, an optional
GhostChat system-alert conversation entry, and an optional audible cue in
connected browser UIs (SSE ``alert`` event).  Nothing is enabled by
default — the operator creates rules in the UI.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

from .util import new_id, now_ms

RULE_TYPES = {
    "cpu_high": {"metric": True, "label": "CPU usage above threshold"},
    "ram_high": {"metric": True, "label": "RAM usage above threshold"},
    "disk_low": {"metric": True, "label": "Disk free space below threshold"},
    "device_offline": {"metric": False, "label": "Device goes offline"},
    "device_online": {"metric": False, "label": "Device comes online"},
    "transfer_failed": {"metric": False, "label": "Transfer fails"},
    "transfer_completed": {"metric": False, "label": "Transfer completes"},
    "companion_error": {"metric": False, "label": "Companion error logged"},
}

METRIC_EVENTS = {"device_lost": "device_offline", "device_connected":
                 "device_online", "device_discovered": "device_online",
                 "transfer_failed": "transfer_failed",
                 "verification_failed": "transfer_failed",
                 "transfer_completed": "transfer_completed",
                 "companion_error": "companion_error"}

ALERT_HISTORY_CAP = 200


class AlertEngine:
    def __init__(self, config, registry, events, chat, transfers, monitor,
                 sse=None, log=None):
        self.config = config
        self.registry = registry
        self.events = events
        self.chat = chat
        self.transfers = transfers
        self.monitor = monitor
        self.sse = sse
        self.log = log
        self._state: Dict[str, dict] = {}      # rule_id → trigger state
        self._history: List[dict] = []
        self._stop = threading.Event()
        self._lock = threading.RLock()
        if events:
            events.subscribe(self._on_event)
        threading.Thread(target=self._evaluate_loop, name="pn-alerts",
                         daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # rules CRUD
    # ------------------------------------------------------------------
    def list_rules(self) -> List[dict]:
        rules = self.config.get("alert_rules") or []
        state = {rid: {"condition_since": s.get("condition_since"),
                       "last_fired": s.get("last_fired"),
                       "fired_count": s.get("fired_count", 0)}
                 for rid, s in self._state.items()}
        out = []
        for r in rules:
            d = dict(r)
            d["state"] = state.get(r.get("id"), {})
            d["type_label"] = RULE_TYPES.get(r.get("type"), {}).get("label",
                                                                    r.get("type"))
            out.append(d)
        return out

    def upsert_rule(self, rule: dict) -> dict:
        rtype = rule.get("type")
        if rtype not in RULE_TYPES:
            from .errors import FsError
            raise FsError(400, "bad_rule", f"Unknown alert type {rtype!r}")
        rules = list(self.config.get("alert_rules") or [])
        clean = {
            "id": rule.get("id") or new_id("alert"),
            "name": str(rule.get("name") or RULE_TYPES[rtype]["label"])[:80],
            "type": rtype,
            "enabled": bool(rule.get("enabled", True)),
            "threshold": _num(rule.get("threshold"),
                              90 if rtype in ("cpu_high", "ram_high") else 10),
            "duration_s": max(0, _num(rule.get("duration_s"), 30)),
            "cooldown_s": max(0, _num(rule.get("cooldown_s"), 300)),
            "severity": rule.get("severity") if rule.get("severity") in
            ("info", "warn", "error") else "warn",
            "notify": {
                "event": bool((rule.get("notify") or {}).get("event", True)),
                "chat": bool((rule.get("notify") or {}).get("chat", True)),
                "sound": bool((rule.get("notify") or {}).get("sound", False)),
            },
        }
        # optional scope: restrict a device rule to one device / a metric to
        # one mount
        if rule.get("device_id"):
            clean["device_id"] = str(rule["device_id"])[:80]
        if rule.get("mount"):
            clean["mount"] = str(rule["mount"])[:200]
        for i, existing in enumerate(rules):
            if existing.get("id") == clean["id"]:
                rules[i] = clean
                break
        else:
            rules.append(clean)
        self.config.set("alert_rules", rules)
        return clean

    def delete_rule(self, rule_id: str) -> dict:
        rules = [r for r in (self.config.get("alert_rules") or [])
                 if r.get("id") != rule_id]
        self.config.set("alert_rules", rules)
        self._state.pop(rule_id, None)
        return {"ok": True, "remaining": len(rules)}

    # ------------------------------------------------------------------
    # metric evaluation (cpu / ram / disk)
    # ------------------------------------------------------------------
    def _evaluate_loop(self) -> None:
        while not self._stop.wait(10.0):
            try:
                self._evaluate_metrics()
            except Exception as exc:
                if self.log:
                    self.log.warning("alert evaluation error: %s", exc)

    def _evaluate_metrics(self) -> None:
        rules = [r for r in (self.config.get("alert_rules") or [])
                 if r.get("enabled") and RULE_TYPES.get(r.get("type"), {})
                 .get("metric")]
        if not rules:
            return
        snap = self.monitor.snapshot()
        now = now_ms()
        for rule in rules:
            triggered, detail = self._metric_state(rule, snap)
            self._check(rule, triggered, detail, now)

    def _metric_state(self, rule: dict, snap: dict) -> tuple:
        rtype = rule["type"]
        threshold = _num(rule.get("threshold"), 90)
        if rtype == "cpu_high":
            cpu = (snap.get("cpu") or {}).get("percent")
            if cpu is None:
                return False, "telemetry unavailable"
            return cpu >= threshold, f"CPU {cpu:.0f}% ≥ {threshold:.0f}%"
        if rtype == "ram_high":
            mem = (snap.get("memory") or {}).get("percent")
            if mem is None:
                return False, "telemetry unavailable"
            return mem >= threshold, f"RAM {mem:.0f}% ≥ {threshold:.0f}%"
        if rtype == "disk_low":
            free_pct = None
            for d in snap.get("disks") or []:
                if rule.get("mount") and d.get("mount") != rule["mount"]:
                    continue
                used = d.get("percent")
                if used is None:
                    continue
                free = max(0.0, 100.0 - used)
                if free_pct is None or free < free_pct:
                    free_pct = free      # the fullest monitored volume
            if free_pct is None:
                return False, "telemetry unavailable"
            return free_pct <= threshold, \
                f"Free space {free_pct:.0f}% ≤ {threshold:.0f}% on " \
                f"{rule.get('mount') or 'the fullest monitored volume'}"
        return False, ""

    # ------------------------------------------------------------------
    # event-driven rules
    # ------------------------------------------------------------------
    def _on_event(self, ev: dict) -> None:
        mapped = METRIC_EVENTS.get(ev.get("type"))
        if not mapped:
            return
        now = now_ms()
        for rule in (self.config.get("alert_rules") or []):
            if not rule.get("enabled") or rule.get("type") != mapped:
                continue
            if rule.get("device_id") and \
                    rule["device_id"] not in (
                        (ev.get("data") or {}).get("device_id", ""),
                        (ev.get("data") or {}).get("client_id", "")):
                continue
            # event rules fire immediately (no duration) with cooldown
            self._fire(rule, ev.get("text") or mapped, now)

    # ------------------------------------------------------------------
    # firing + delivery
    # ------------------------------------------------------------------
    def _check(self, rule: dict, triggered: bool, detail: str,
               now: int) -> None:
        st = self._state.setdefault(rule["id"], {})
        if triggered:
            st.setdefault("condition_since", now)
            holding = (now - st["condition_since"]) / 1000.0 >= \
                _num(rule.get("duration_s"), 0)
            if holding:
                self._fire(rule, detail, now)
        else:
            st.pop("condition_since", None)

    def _fire(self, rule: dict, detail: str, now: int) -> None:
        st = self._state.setdefault(rule["id"], {})
        last = st.get("last_fired") or 0
        if now - last < _num(rule.get("cooldown_s"), 300) * 1000:
            return
        st["last_fired"] = now
        st["fired_count"] = st.get("fired_count", 0) + 1
        st.pop("condition_since", None)
        text = f"ALERT [{rule.get('name')}] {detail}"
        notify = rule.get("notify") or {}
        if notify.get("event", True) and self.events:
            self.events.emit("system", text,
                             severity=rule.get("severity", "warn"),
                             data={"alert_rule": rule["id"]})
        if notify.get("chat", True) and self.chat:
            try:
                self.chat.send("system", "alerts", text)
            except Exception:
                pass
        if self.sse:
            self.sse.publish("alert", {
                "rule_id": rule["id"], "text": text,
                "severity": rule.get("severity", "warn"),
                "sound": bool(notify.get("sound")),
            })
        with self._lock:
            self._history.append({"ts": now, "iso": time.strftime(
                "%Y-%m-%dT%H:%M:%S"), "rule_id": rule["id"],
                "name": rule.get("name"), "text": detail,
                "severity": rule.get("severity", "warn")})
            del self._history[:-ALERT_HISTORY_CAP]

    def history(self) -> List[dict]:
        with self._lock:
            return list(reversed(self._history))


def _num(value, default) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)
