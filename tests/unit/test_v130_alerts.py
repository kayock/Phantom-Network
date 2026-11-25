"""v1.3.0 unit tests: monitoring alert rules (thresholds, duration,
cooldown, suppression, honest delivery)."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(__file__, "..", "..", "..")))

from companion.alerts import AlertEngine  # noqa: E402
from companion.config import Config  # noqa: E402


class FakeConfig(Config):
    """Config without a real data dir (in-memory only)."""

    def __init__(self, tmp_path):
        super().__init__(str(tmp_path))

    def set(self, key, value):
        with self._lock:
            self._data[key] = value


class FakeEvents:
    def __init__(self):
        self.emitted = []

    def emit(self, type_, text, severity="info", source="self", data=None):
        self.emitted.append({"type": type_, "text": text,
                             "severity": severity})

    def subscribe(self, cb):
        return lambda: None


class FakeChat:
    def __init__(self):
        self.sent = []

    def send(self, to_kind, to_id, body, **kw):
        self.sent.append((to_kind, to_id, body))
        return {"id": "fake"}


class FakeSse:
    def __init__(self):
        self.published = []

    def publish(self, topic, data):
        self.published.append((topic, data))


class FakeMonitor:
    def __init__(self, cpu=None, mem=None, disks=None):
        self.cpu = cpu
        self.mem = mem
        self.disks = disks or []

    def snapshot(self):
        return {
            "cpu": {"percent": self.cpu},
            "memory": {"percent": self.mem},
            "disks": self.disks,
        }


class FakeRegistry:
    def all(self):
        return []


def make_engine(tmp_path, monitor, sse=None):
    cfg = FakeConfig(tmp_path)
    events = FakeEvents()
    chat = FakeChat()
    eng = AlertEngine(cfg, FakeRegistry(), events, chat, None, monitor,
                      sse=sse or FakeSse())
    return eng, cfg, events, chat


def test_metric_rule_fires_after_duration(tmp_path):
    mon = FakeMonitor(cpu=95.0)
    eng, cfg, events, chat = make_engine(tmp_path, mon)
    eng.upsert_rule({"name": "CPU hot", "type": "cpu_high", "threshold": 90,
                     "duration_s": 5, "cooldown_s": 60})
    # evaluation happens on the 10 s loop — drive it directly
    rule = cfg.get("alert_rules")[0]
    t0 = 1_000_000
    eng._check(rule, True, "CPU 95% ≥ 90%", t0)           # condition seen
    assert len(chat.sent) == 0                            # holding period…
    eng._check(rule, True, "CPU 95% ≥ 90%", t0 + 6_000)   # …6 s later: fire
    assert len(chat.sent) == 1
    assert "ALERT" in chat.sent[0][2]
    assert events.emitted
    # cooldown suppresses an immediate re-fire
    eng._check(rule, True, "CPU 95% ≥ 90%", t0 + 7_000)
    assert len(chat.sent) == 1


def test_metric_below_threshold_never_fires(tmp_path):
    mon = FakeMonitor(cpu=42.0)
    eng, cfg, events, chat = make_engine(tmp_path, mon)
    eng.upsert_rule({"name": "CPU hot", "type": "cpu_high", "threshold": 90,
                     "duration_s": 0, "cooldown_s": 0})
    rule = cfg.get("alert_rules")[0]
    eng._check(rule, *(eng._metric_state(rule, mon.snapshot())), 1_000_000)
    assert not chat.sent


def test_disk_low_rule(tmp_path):
    mon = FakeMonitor(disks=[{"mount": "/", "percent": 97.0}])
    eng, cfg, events, chat = make_engine(tmp_path, mon)
    eng.upsert_rule({"name": "Disk full soon", "type": "disk_low",
                     "threshold": 5, "duration_s": 0, "cooldown_s": 0})
    rule = cfg.get("alert_rules")[0]
    triggered, detail = eng._metric_state(rule, mon.snapshot())
    assert triggered and "5%" in detail


def test_unavailable_telemetry_does_not_fire(tmp_path):
    mon = FakeMonitor(cpu=None, mem=None)
    eng, cfg, events, chat = make_engine(tmp_path, mon)
    eng.upsert_rule({"name": "CPU hot", "type": "cpu_high", "threshold": 90})
    rule = cfg.get("alert_rules")[0]
    triggered, detail = eng._metric_state(rule, mon.snapshot())
    assert triggered is False
    assert "unavailable" in detail


def test_event_rule_device_offline(tmp_path):
    mon = FakeMonitor()
    eng, cfg, events, chat = make_engine(tmp_path, mon)
    eng.upsert_rule({"name": "Station down", "type": "device_offline",
                     "device_id": "dev_station", "cooldown_s": 60})
    eng._on_event({"type": "device_lost", "text": "Device went offline: Station",
                   "data": {"device_id": "dev_station"}})
    assert chat.sent and "Station" in chat.sent[0][2]
    # other devices do not trigger it
    n = len(chat.sent)
    eng._on_event({"type": "device_lost", "text": "gone",
                   "data": {"device_id": "dev_other"}})
    assert len(chat.sent) == n


def test_disabled_rules_do_nothing(tmp_path):
    mon = FakeMonitor(cpu=99.0)
    eng, cfg, events, chat = make_engine(tmp_path, mon)
    eng.upsert_rule({"name": "off", "type": "cpu_high", "threshold": 10,
                     "enabled": False, "duration_s": 0, "cooldown_s": 0})
    eng._evaluate_metrics()
    assert not chat.sent
    assert not events.emitted


def test_sound_and_event_delivery_channels(tmp_path):
    mon = FakeMonitor()
    sse = FakeSse()
    eng, cfg, events, chat = make_engine(tmp_path, mon, sse=sse)
    eng.upsert_rule({"name": "cue", "type": "device_online", "cooldown_s": 0,
                     "notify": {"event": True, "chat": False, "sound": True}})
    eng._on_event({"type": "device_connected", "text": "up",
                   "data": {"device_id": "x"}})
    assert not chat.sent                       # chat channel disabled
    assert any(t == "alert" and d.get("sound") is True
               for t, d in sse.published)


def test_history_recorded(tmp_path):
    mon = FakeMonitor()
    eng, cfg, events, chat = make_engine(tmp_path, mon)
    eng.upsert_rule({"name": "hist", "type": "companion_error",
                     "cooldown_s": 0})
    eng._on_event({"type": "companion_error", "text": "boom", "data": {}})
    hist = eng.history()
    assert hist and hist[0]["name"] == "hist"
    assert hist[0]["severity"] == "warn"
