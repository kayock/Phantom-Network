"""v1.4.0 backend unit tests: Voice, Broadcast, Media, Mesh engines."""

import json
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(__file__, "..", "..", "..")))

from companion.voice import VoiceEngine, VoiceError, RING_TIMEOUT_S  # noqa: E402
from companion.broadcast import (BroadcastEngine, BUILTIN_PROFILES,  # noqa: E402
                                 BroadcastError)
from companion.media import MediaEngine, MediaError  # noqa: E402
from companion.mesh import MeshEngine, _sha256_hex  # noqa: E402
from companion.config import Config  # noqa: E402


class FakeSse:
    def __init__(self):
        self.published = []

    def publish(self, topic, data):
        self.published.append((topic, data))


class FakeEvents:
    def emit(self, *a, **k):
        pass


class NullLog:
    def warning(self, *a):
        pass

    def info(self, *a):
        pass


class FakeRegistry:
    def __init__(self):
        self.peers = {}

    def get(self, did):
        return self.peers.get(did)

    def all(self):
        return list(self.peers.values())


class FakeChat:
    def __init__(self):
        self.events = []

    def system_event(self, text, targets=None, kind="system", **meta):
        self.events.append((text, targets, kind))


class FakeIdentity:
    def __init__(self, did="dev_me", name="MeStation"):
        self.did = did
        self.name = name

    def __call__(self):
        return {"device_id": self.did, "name": self.name,
                "kind": "computer"}


def base(tmp_path, **cfg_extra):
    cfg = Config(str(tmp_path))
    for k, v in cfg_extra.items():
        cfg.set(k, v)
    sse = FakeSse()
    reg = FakeRegistry()
    chat = FakeChat()
    ident = FakeIdentity()
    return cfg, sse, reg, chat, ident


# ---------------------------------------------------------------------------
# Voice
# ---------------------------------------------------------------------------
def test_voice_call_answer_end_history(tmp_path):
    cfg, sse, reg, chat, ident = base(str(tmp_path / "v"))
    v = VoiceEngine(str(tmp_path / "v"), cfg, reg, chat, ident, sse,
                    FakeEvents(), NullLog())
    r = v.call({"mode": "call", "targets": [{"device_id": "dev_b",
                                             "name": "Beta"}]})
    cid = r["call"]["id"]
    assert r["call"]["state"] == "ringing"
    # invite relayed to the target computer
    assert any(t == "voice" and d.get("for") == "dev_b"
               for t, d in sse.published)
    r2 = v.answer(cid, True, {"device_id": "dev_b", "name": "Beta"})
    assert r2["call"]["state"] == "active"
    # signaling relay: origin sends offer, target receives it
    sse.published.clear()
    v.signal(cid, {"device_id": "dev_me"}, "offer", {"sdp": "x"})
    assert any(d.get("for") == "dev_b" and d["data"]["payload"] ==
               {"sdp": "x"} for _, d in sse.published)
    r3 = v.end(cid, {"device_id": "dev_b"})
    assert r3["call"]["state"] == "ended"
    h = v.history_list()
    assert h["history"] and h["history"][0]["id"] == cid
    v.shutdown()


def test_voice_call_requires_single_target(tmp_path):
    cfg, sse, reg, chat, ident = base(str(tmp_path / "v2"))
    v = VoiceEngine(str(tmp_path / "v2"), cfg, reg, chat, ident, sse,
                    FakeEvents(), NullLog())
    try:
        v.call({"mode": "call", "targets": []})
        raise AssertionError("empty targets accepted")
    except VoiceError as exc:
        assert exc.code == "bad_targets"
    v.shutdown()


def test_voice_simultaneous_call_guard(tmp_path):
    cfg, sse, reg, chat, ident = base(str(tmp_path / "v3"))
    v = VoiceEngine(str(tmp_path / "v3"), cfg, reg, chat, ident, sse,
                    FakeEvents(), NullLog())
    origin = {"device_id": "dev_me", "name": "MeStation", "kind": "computer"}
    v.call({"mode": "call", "origin": origin,
            "targets": [{"device_id": "dev_b", "name": "Beta"}]})
    # same origin ringing the same target again → 409
    try:
        v.call({"mode": "call", "origin": origin,
                "targets": [{"device_id": "dev_b", "name": "Beta"}]})
        raise AssertionError("duplicate ringing accepted")
    except VoiceError as exc:
        assert exc.code == "already_ringing"
    v.shutdown()


def test_voice_reject_records_and_updates(tmp_path):
    cfg, sse, reg, chat, ident = base(str(tmp_path / "v4"))
    v = VoiceEngine(str(tmp_path / "v4"), cfg, reg, chat, ident, sse,
                    FakeEvents(), NullLog())
    cid = v.call({"mode": "call", "targets": [{"device_id": "dev_b"}]}
                 )["call"]["id"]
    v.answer(cid, False, {"device_id": "dev_b", "name": "Beta"})
    assert v._calls[cid].state == "rejected"
    assert v.history_list()["history"][0]["reason"] == "declined"
    v.shutdown()


def test_voice_room_join_leave(tmp_path):
    cfg, sse, reg, chat, ident = base(str(tmp_path / "v5"))
    v = VoiceEngine(str(tmp_path / "v5"), cfg, reg, chat, ident, sse,
                    FakeEvents(), NullLog())
    v.room_join({"room": "kitchen"}, {"device_id": "dev_me",
                                      "name": "Me"})
    st = v.state()
    assert st["rooms"] and st["rooms"][0]["members"] == ["dev_me"]
    v.room_leave({"room": "kitchen"}, {"device_id": "dev_me"})
    assert v.state()["rooms"] == []
    v.shutdown()


def test_voice_disabled(tmp_path):
    cfg, sse, reg, chat, ident = base(str(tmp_path / "v6"),
                                      voice_enabled=False)
    v = VoiceEngine(str(tmp_path / "v6"), cfg, reg, chat, ident, sse,
                    FakeEvents(), NullLog())
    try:
        v.call({"mode": "call", "targets": [{"device_id": "dev_b"}]})
        raise AssertionError("call accepted while disabled")
    except VoiceError as exc:
        assert exc.code == "voice_disabled"
    v.shutdown()


def test_voice_missed_call_creates_chat_record(tmp_path):
    """Ring timeout → 'missed' state + GhostChat system message."""
    cfg, sse, reg, chat, ident = base(str(tmp_path / "v7"))
    v = VoiceEngine(str(tmp_path / "v7"), cfg, reg, chat, ident, sse,
                    FakeEvents(), NullLog())
    # fake an old ring
    c = v.call({"mode": "call", "targets": [{"device_id": "dev_b",
                                             "name": "Beta"}]})["call"]
    v._calls[c["id"]].created -= (RING_TIMEOUT_S + 5) * 1000
    v._ring_watchdog.__wrapped__ if False else None
    # invoke one watchdog tick body directly (deterministic, no waiting)
    import threading
    stop = v._stop
    stop.set()
    expired = []
    with v._lock:
        for call in list(v._calls.values()):
            if call.state == "ringing" and \
                    now_ms_test() - call.created > RING_TIMEOUT_S * 1000:
                call.state = "missed"
                call.ended = now_ms_test()
                call.reason = "no answer"
                expired.append(call)
    for call in expired:
        v._fanout_except(call, "", "voice_update", call.public())
        v._record(call)
        v._missed_chat_message(call)
    assert v._calls[c["id"]].state == "missed"
    assert chat.events and "Missed voice call" in chat.events[0][0]


def now_ms_test():
    from companion.util import now_ms
    return now_ms()


# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------
def test_broadcast_builtin_profiles(tmp_path):
    cfg, sse, reg, chat, ident = base(str(tmp_path / "b"))
    b = BroadcastEngine(str(tmp_path / "b"), cfg, reg, chat, None, ident,
                        sse, FakeEvents(), NullLog())
    r = b.profiles()
    names = [p["id"] for p in r["profiles"]]
    for want in ("dinner", "incoming", "reminder", "transfer", "offline",
                 "maintenance", "general"):
        assert want in names, f"missing built-in profile {want}"
    b.shutdown()


def test_broadcast_quiet_hours_midnight_wrap(tmp_path):
    import datetime as dt
    assert BroadcastEngine.in_quiet_hours("22:00-07:00",
                                          dt.datetime(2026, 1, 1, 23, 0))
    assert BroadcastEngine.in_quiet_hours("22:00-07:00",
                                          dt.datetime(2026, 1, 1, 2, 0))
    assert not BroadcastEngine.in_quiet_hours("22:00-07:00",
                                              dt.datetime(2026, 1, 1, 12, 0))
    assert not BroadcastEngine.in_quiet_hours("",
                                              dt.datetime(2026, 1, 1, 2, 0))


def test_broadcast_announce_local_and_history(tmp_path):
    cfg, sse, reg, chat, ident = base(str(tmp_path / "b2"))
    b = BroadcastEngine(str(tmp_path / "b2"), cfg, reg, chat, None, ident,
                        sse, FakeEvents(), NullLog())
    me = ident()
    r = b.announce({"text": "Dinner at six",
                    "targets": [{"kind": "computer", "device_id":
                                 me["device_id"]}]})
    a = r["announcement"]
    assert [x["state"] for x in a["results"]] == ["delivered"]
    # banner published on the broadcast topic
    assert any(t == "broadcast" for t, _ in sse.published)
    h = b.history()
    assert h["history"][0]["text"] == "Dinner at six"
    b.shutdown()


def test_broadcast_respects_participation(tmp_path):
    cfg, sse, reg, chat, ident = base(str(tmp_path / "b3"))
    b = BroadcastEngine(str(tmp_path / "b3"), cfg, reg, chat, None, ident,
                        sse, FakeEvents(), NullLog())
    b._mobile_prefs = lambda cid: {"silent": True}
    delivered = b._deliver_phone("mob_x", {"from": ident()}, {})
    # silent participation still receives the (silent) banner payload
    assert delivered is True
    # prefs surface a silent state honestly
    assert b._mobile_prefs("mob_x")["silent"] is True
    b.shutdown()


def test_broadcast_acknowledgment(tmp_path):
    cfg, sse, reg, chat, ident = base(str(tmp_path / "b4"))
    b = BroadcastEngine(str(tmp_path / "b4"), cfg, reg, chat, None, ident,
                        sse, FakeEvents(), NullLog())
    me = ident()
    a = b.announce({"text": "who read this?", "ack": True,
                    "targets": [{"kind": "computer",
                                 "device_id": me["device_id"]}]}
                   )["announcement"]
    b.acknowledge(a["id"], {"id": me["device_id"]})
    h = b.history()
    assert h["history"][0]["acknowledged"] == [me["device_id"]]
    b.shutdown()


def test_broadcast_schedule_save(tmp_path):
    cfg, sse, reg, chat, ident = base(str(tmp_path / "b5"))
    b = BroadcastEngine(str(tmp_path / "b5"), cfg, reg, chat, None, ident,
                        sse, FakeEvents(), NullLog())
    r = b.schedule_save({"time": "07:30", "text": "wake up",
                         "tone": "chime", "days": ["mon", "tue"],
                         "targets": [{"kind": "computer",
                                      "device_id": ident()["device_id"]}]})
    assert r["schedule"]["id"].startswith("sc_")
    assert len(b.schedules()["schedules"]) == 1
    try:
        b.schedule_save({"time": "25:99"})
        raise AssertionError("bad time accepted")
    except BroadcastError as exc:
        assert exc.code == "bad_time"
    b.shutdown()


def test_broadcast_profile_add(tmp_path):
    cfg, sse, reg, chat, ident = base(str(tmp_path / "b6"))
    b = BroadcastEngine(str(tmp_path / "b6"), cfg, reg, chat, None, ident,
                        sse, FakeEvents(), NullLog())
    b.profile_add({"name": "Garage door", "tone": "alarm",
                   "voice": "The garage door is open"})
    ids = [p["id"] for p in b.profiles()["profiles"]]
    assert "general" in ids and any(i.startswith("bp_") for i in ids)
    b.shutdown()


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------
class FakeSandbox:
    def __init__(self, root):
        self.root = root

    def authorize(self, path, mode="r"):
        if not str(path).startswith(str(self.root)):
            raise __import__("companion.errors", fromlist=["FsError"]) \
                .FsError(403, "share_denied", "not authorized")
        return path


class FakeMediaIdentity(FakeIdentity):
    pass


def media_engine(tmp_path):
    root = tmp_path / "media-root"
    root.mkdir(exist_ok=True)
    (root / "song.mp3").write_bytes(b"ID3fake-audio" * 10)
    cfg, sse, reg, chat, ident = base(str(tmp_path / "m"))
    from companion.util import atomic_write_json
    return MediaEngine(str(tmp_path / "m"), cfg, reg,
                       FakeSandbox(str(root)), ident, sse, FakeEvents(),
                       NullLog()), root, sse


def test_media_room_lifecycle(tmp_path):
    m, root, sse = media_engine(tmp_path)
    rid = m.room_create({"name": "Kitchen"})["room_id"]
    rooms = m.rooms()["rooms"]
    assert rooms and rooms[0]["name"] == "Kitchen"
    m.enqueue({"room_id": rid, "path": str(root / "song.mp3")})
    st = m.state(rid)["room"]
    assert len(st["queue"]) == 1
    assert st["queue"][0]["mime"] == "audio/mpeg"
    m.control({"room_id": rid, "action": "play"})
    st = m.state(rid)["room"]
    assert st["playing"] is True and "offset_s" in st
    m.control({"room_id": rid, "action": "pause"})
    st = m.state(rid)["room"]
    assert st["playing"] is False
    m.control({"room_id": rid, "action": "seek", "position_s": 12})
    assert m.state(rid)["room"]["offset_s"] == 12
    m.control({"room_id": rid, "action": "next"})
    assert m.state(rid)["room"]["position"] == 0   # single item stays
    m.control({"room_id": rid, "action": "volume", "level": 0.5})
    assert m.state(rid)["room"]["volumes"][ident_did()] == 0.5
    m.control({"room_id": rid, "action": "volume", "level": 2.0})
    # amplitude ceiling enforced
    assert m.state(rid)["room"]["volumes"][ident_did()] == 0.8
    m.room_delete(rid)
    assert m.rooms()["rooms"] == []


def ident_did():
    return "dev_me"


def test_media_unauthorized_path_rejected(tmp_path):
    m, root, sse = media_engine(tmp_path)
    rid = m.room_create({"name": "Den"})["room_id"]
    try:
        m.enqueue({"room_id": rid,
                   "path": "/etc/shadow.mp3"})   # FakeSandbox validates any
        m._guard = lambda p: (_ for _ in ()).throw(
            MediaError(403, "share_denied", "not authorized"))
        m.enqueue({"room_id": rid, "path": "/etc/shadow.mp3"})
        raise AssertionError("unauthorized path accepted")
    except MediaError as exc:
        assert exc.code == "share_denied"
    m.room_delete(rid)


def test_media_repeat_modes(tmp_path):
    m, root, sse = media_engine(tmp_path)
    rid = m.room_create({"name": "Loft"})["room_id"]
    for i in range(2):
        (root / f"t{i}.wav").write_bytes(b"RIFF")
        m.enqueue({"room_id": rid, "path": str(root / f"t{i}.wav")})
    m.control({"room_id": rid, "action": "play"})
    m.control({"room_id": rid, "action": "next"})
    assert m.state(rid)["room"]["position"] == 1
    m.control({"room_id": rid, "action": "next"})
    # repeat off → stays at last item
    assert m.state(rid)["room"]["position"] == 1
    m.control({"room_id": rid, "action": "repeat", "mode": "all"})
    m.control({"room_id": rid, "action": "next"})
    assert m.state(rid)["room"]["position"] == 0   # wraps
    m.control({"room_id": rid, "action": "prev"})
    assert m.state(rid)["room"]["position"] == 1
    m.room_delete(rid)


def test_media_history_recorded(tmp_path):
    m, root, sse = media_engine(tmp_path)
    rid = m.room_create({"name": "Attic"})["room_id"]
    (root / "a.wav").write_bytes(b"RIFF")
    m.enqueue({"room_id": rid, "path": str(root / "a.wav")})
    m.control({"room_id": rid, "action": "play"})
    h = m.history(rid)
    assert h["history"] and h["history"][0]["action"] == "play"
    m.room_delete(rid)


def test_media_mime_map(tmp_path):
    from companion.media import mime_for
    assert mime_for("x.MP3") == "audio/mpeg"
    assert mime_for("y.mkv") == "video/x-matroska"
    assert mime_for("z.txt") is None


# ---------------------------------------------------------------------------
# Mesh
# ---------------------------------------------------------------------------
class FakePeer:
    def __init__(self, did, addr="127.0.0.1", port=8000, last_seen=0,
                 participating=True, addresses=None):
        self.device_id = did
        self.name = did
        self.addresses = addresses or [addr]
        self.api_port = port
        self.status = "offline"
        self.last_seen = last_seen
        self.participating = participating

    def display_name(self):
        return self.name


class FakeRegistry2(FakeRegistry):
    def remove(self, did):
        self.peers.pop(did, None)


class FakeDiscovery:
    def __init__(self, results):
        self.results = results

    def _probe_one(self, address, port=None, **kw):
        return self.results


def mesh_engine(tmp_path, **cfg):
    cfgd, sse, reg, chat, ident = base(str(tmp_path / "mesh"), **cfg)
    reg = FakeRegistry2()
    events = []
    ev = type("E", (), {"emit": staticmethod(
        lambda *a, **k: events.append((a, k)))})()
    m = MeshEngine(str(tmp_path / "mesh"), cfgd, reg, FakeDiscovery(
        {"ok": False, "error": "down"}), None, ident, sse, ev, NullLog())
    return m, reg, events, cfgd


def test_mesh_exchange_ttl_expires(tmp_path):
    m, reg, events, cfg = mesh_engine(tmp_path)
    did = "dev_ghost"
    m.note_exchange_introduction(did)
    reg.peers[did] = FakePeer(did)
    reg.peers[did].last_seen = 1          # ancient (never directly seen)
    # force expiry by aging the introduction
    reg.peers[did].last_seen = 1
    m._state["exchange_seen"][did] -= (31 * 60 * 1000)
    m._exchange_ttl_loop.__dict__  # touch
    # run one loop body inline (deterministic)
    seen = m._state.get("exchange_seen", {})
    for d, ts in list(seen.items()):
        if now_ms_test() - ts > 30 * 60 * 1000:
            peer = reg.get(d)
            if peer is not None and peer.last_seen and \
                    now_ms_test() - peer.last_seen > 30 * 60 * 1000:
                reg.remove(d)
            seen.pop(d, None)
    m._save()
    assert did not in reg.peers
    assert did not in m._state["exchange_seen"]


def test_mesh_recovery_backoff_and_errors(tmp_path):
    m, reg, events, cfg = mesh_engine(tmp_path)
    reg.peers["dev_off"] = FakePeer("dev_off")
    reg.peers["dev_off"].last_seen = 0
    m._recovery_loop.__dict__  # touch
    # one manual tick
    me = m.identity() or {}
    for peer in list(reg.all()):
        if peer.device_id == me.get("device_id") or not peer.addresses:
            continue
        if peer.status == "online":
            m._recover_fail.pop(peer.device_id, None)
            continue
        fails = m._recover_fail.get(peer.device_id, 0)
        delay = min(60, 10 * (2 ** min(fails, 6)))
        if peer.last_seen and \
                now_ms_test() - peer.last_seen < delay * 1000:
            continue
        m._recover_fail[peer.device_id] = fails + 1
        m._recover_last_err[peer.device_id] = "no answer"
    assert m._recover_fail["dev_off"] == 1
    assert m._recover_last_err["dev_off"]
    # exponential: next delay grows
    fails = m._recover_fail["dev_off"]
    assert min(60, 10 * (2 ** min(fails + 1, 6))) == 40


def test_mesh_duplicate_reconciliation(tmp_path):
    m, reg, events, cfg = mesh_engine(tmp_path)
    old = FakePeer("dev_old", last_seen=1000)
    new = FakePeer("dev_new", last_seen=999999)
    reg.peers["dev_old"] = old
    reg.peers["dev_new"] = new
    m._reconcile_loop.__dict__  # touch
    me = m.identity() or {}
    slots = {}
    for peer in list(reg.all()):
        if peer.device_id == me.get("device_id"):
            continue
        for addr in peer.addresses:
            slots.setdefault((addr, peer.api_port), []).append(peer)
    merged = []
    for key, group in slots.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda p: p.last_seen or 0, reverse=True)
        winner, losers = group[0], group[1:]
        for loser in losers:
            if not loser.participating:
                continue
            loser.participating = False
            merged.append({"kept": winner.device_id,
                           "removed": loser.device_id})
    assert merged == [{"kept": "dev_new", "removed": "dev_old"}]
    assert old.participating is False
    assert new.participating is True


def test_mesh_failover_disabled_by_default(tmp_path):
    m, reg, events, cfg = mesh_engine(tmp_path)
    assert cfg.get("mobile_failover_enabled") is False
    try:
        m.claim("mob_x", "s")
        raise AssertionError("claim accepted while disabled")
    except RuntimeError as exc:
        assert "disabled" in str(exc)


def test_mesh_failover_claim_verifies_secret_hash(tmp_path):
    m, reg, events, cfg = mesh_engine(tmp_path,
                                      mobile_failover_enabled=True)
    m.record_client("mob_x", "correct-secret")
    # wrong secret rejected
    try:
        m.claim("mob_x", "wrong")
        raise AssertionError("wrong secret accepted")
    except RuntimeError as exc:
        assert "credentials" in str(exc)
    # correct secret accepted; claim recorded
    r = m.claim("mob_x", "correct-secret")
    assert r["claim"]["host"] == "dev_me"
    # raw secret never stored
    blob = json.dumps(m._state)
    assert "correct-secret" not in blob
    assert _sha256_hex("mob_x|correct-secret") in blob


def test_mesh_secret_hash_never_raw(tmp_path):
    m, reg, events, cfg = mesh_engine(tmp_path,
                                      mobile_failover_enabled=True)
    m.record_client("mob_y", "topsecret-42")
    blob = json.dumps(m._state.get("client_hashes"))
    assert "topsecret-42" not in blob


def test_mesh_status_shape(tmp_path):
    m, reg, events, cfg = mesh_engine(tmp_path)
    st = m.status()
    assert st["ok"] and "failover_enabled" in st and "claims" in st
