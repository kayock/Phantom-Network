"""Unit tests: shared utilities + event bus (pure stdlib, no network)."""

import json
import os
import sys
import tempfile
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from companion.util import (JsonStore, atomic_write_json, human_size,  # noqa: E402
                            new_id, read_json, sha256_file)


def test_human_size():
    assert human_size(0) == "0 B"
    assert human_size(512) == "512 B"
    assert human_size(1536) == "1.5 KB"
    assert human_size(5 * 1024 ** 3).endswith("GB")
    assert human_size(None) == "—"


def test_sha256_streaming_and_progress():
    data = os.urandom(1_000_003)
    with tempfile.NamedTemporaryFile(delete=False) as fh:
        fh.write(data)
        path = fh.name
    import hashlib
    assert sha256_file(path) == hashlib.sha256(data).hexdigest()
    seen = []
    sha256_file(path, chunk_size=100_000, progress_cb=seen.append)
    assert seen[-1] == os.path.getsize(path)
    # cancellation raises
    ev = threading.Event()
    ev.set()
    with pytest.raises(KeyboardInterrupt):
        sha256_file(path, cancel=ev)
    os.unlink(path)


def test_atomic_json_roundtrip_and_corruption_guard():
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "x.json")
        atomic_write_json(p, {"hello": ["wörld", 42]})
        assert read_json(p) == {"hello": ["wörld", 42]}
        # corrupt file → default returned, not crash
        with open(p, "w") as fh:
            fh.write("{not json")
        assert read_json(p, {"def": 1}) == {"def": 1}
        assert read_json(os.path.join(td, "missing.json"), []) == []


def test_json_store_update_persists():
    with tempfile.TemporaryDirectory() as td:
        store = JsonStore(os.path.join(td, "s.json"), {"peers": {}})
        store.update(lambda d: d["peers"].update({"dev1": {"name": "X"}}))
        again = JsonStore(os.path.join(td, "s.json"), {"peers": {}})
        assert again.get()["peers"]["dev1"]["name"] == "X"


def test_new_id_shape():
    assert new_id("job").startswith("job_")
    assert len(new_id("x")) == len("x_") + 16


# ----------------------------------------------------------------------
from companion.events import EventBus  # noqa: E402


def test_event_bus_persist_and_reload():
    with tempfile.TemporaryDirectory() as td:
        bus = EventBus(td)
        bus.emit("device_connected", "Alpha joined", data={"device_id": "d1"})
        bus.emit("transfer_failed", "boom", severity="error")
        bus2 = EventBus(td)
        events = bus2.query()
        assert len(events) == 2
        assert events[0]["text"] == "boom"          # newest first
        assert events[1]["type"] == "device_connected"


def test_event_bus_filters():
    with tempfile.TemporaryDirectory() as td:
        bus = EventBus(td)
        for i in range(10):
            bus.emit("message", f"msg {i}")
        bus.emit("audio_playback", "sound", severity="warn")
        msgs = bus.query(types=["message"])
        assert len(msgs) == 10
        assert bus.query(search="msg 7")[0]["text"] == "msg 7"
        assert bus.query(severity="warn")[0]["type"] == "audio_playback"
        assert bus.query(limit=3)[0]["text"] == "sound"


def test_event_bus_live_subscription():
    with tempfile.TemporaryDirectory() as td:
        bus = EventBus(td)
        got = []
        un = bus.subscribe(lambda ev: got.append(ev))
        bus.emit("system", "hello live")
        assert got and got[0]["text"] == "hello live"
        un()
        bus.emit("system", "second")
        assert len(got) == 1
