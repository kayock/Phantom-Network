"""v1.3.0 unit tests: mobile client registry (no HTTP — pure logic)."""

import json
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(__file__, "..", "..", "..")))

from companion.mobile import MobileClientRegistry  # noqa: E402
from companion.errors import FsError  # noqa: E402
from companion.config import Config  # noqa: E402


class FakeEvents:
    def __init__(self):
        self.emitted = []

    def emit(self, type_, text, severity="info", source="self", data=None):
        self.emitted.append((type_, text))


class FakeSse:
    def __init__(self):
        self.published = []

    def publish(self, topic, data):
        self.published.append((topic, data))


class NullLog:
    def warning(self, *a):
        pass

    def info(self, *a):
        pass


def make_registry(tmp_path, ttl_ms=45000):
    cfg = Config(str(tmp_path))
    cfg.set("mobile_client_ttl_ms", ttl_ms)
    reg = MobileClientRegistry(str(tmp_path), cfg, FakeEvents(), FakeSse(),
                               NullLog())
    return reg, cfg


def test_register_assigns_identity_and_secret_once(tmp_path):
    reg, _ = make_registry(tmp_path)
    ident = reg.register({"name": "My iPhone", "device_type": "phone"})
    assert ident["client_id"].startswith("mob_")
    assert ident["secret"] and len(ident["secret"]) == 32
    assert ident["name"] == "My iPhone"
    assert ident["kind"] == "mobile"
    assert ident["capabilities"]["chat"] is True
    assert ident["capabilities"]["shares"] == 0
    # public list never leaks the secret
    pub = reg.public_list()
    assert pub and "secret" not in pub[0]


def test_reregistration_is_stable_not_fresh(tmp_path):
    reg, _ = make_registry(tmp_path)
    first = reg.register({"name": "Phone", "device_type": "phone"})
    cid = first["client_id"]
    # browser refresh: same ids come back, no new identity
    second = reg.register({"client_id": cid,
                           "client_secret": first["secret"],
                           "name": "Phone", "device_type": "phone"})
    assert second["client_id"] == cid
    assert len(reg.all()) == 1


def test_identity_mismatch_rejected(tmp_path):
    reg, _ = make_registry(tmp_path)
    first = reg.register({"name": "Phone"})
    try:
        reg.register({"client_id": first["client_id"],
                      "client_secret": "wrong-secret-here-0000000000"})
        assert False, "mismatched secret must be rejected"
    except FsError as exc:
        assert exc.code == "identity_mismatch"


def test_rename_and_remove(tmp_path):
    reg, _ = make_registry(tmp_path)
    ident = reg.register({"name": "Phone"})
    cid = ident["client_id"]
    renamed = reg.rename(cid, "Family Tablet", icon="📲", device_type="tablet")
    assert renamed["name"] == "Family Tablet"
    assert renamed["device_type"] == "tablet"
    reg.remove(cid)
    assert reg.get(cid) is None
    # inbox file removed with the client
    assert not os.path.exists(os.path.join(
        str(tmp_path), f"mobile_inbox_{cid}.jsonl"))


def test_persistence_across_restart(tmp_path):
    reg, _ = make_registry(tmp_path)
    ident = reg.register({"name": "Persist Phone"})
    cid, secret = ident["client_id"], ident["secret"]
    # new registry instance = companion restart
    reg2, _ = make_registry(tmp_path)
    client = reg2.get(cid)
    assert client is not None
    assert client.secret == secret
    assert client.name == "Persist Phone"
    # heartbeat with the stored secret works after restart
    out = reg2.heartbeat(cid, secret)
    assert out["ok"] is True


def test_presence_ttl_sweep_marks_offline(tmp_path):
    reg, _ = make_registry(tmp_path, ttl_ms=50)
    ident = reg.register({"name": "TTL Phone"})
    cid = ident["client_id"]
    reg.heartbeat(cid, ident["secret"])
    assert reg.count_online() == 1
    time.sleep(0.12)
    lost = reg.sweep_offline()
    assert [c.client_id for c in lost] == [cid]
    assert reg.count_online() == 0
    # last_seen is preserved honestly (never fabricated online)
    assert reg.get(cid).last_seen > 0


def test_inbox_append_mark_since(tmp_path):
    reg, _ = make_registry(tmp_path)
    ident = reg.register({"name": "Inbox Phone"})
    cid = ident["client_id"]
    reg.inbox_append(cid, {"id": "m1", "conv": "direct:devA", "ts": 100,
                           "body": "hello", "outgoing": False})
    reg.inbox_append(cid, {"id": "m2", "conv": "direct:devA", "ts": 200,
                           "body": "second", "outgoing": False})
    rows = reg.inbox_since(cid, 0)
    assert [m["id"] for m in rows] == ["m1", "m2"]
    assert rows[0]["status"] == "delivered_to_host"   # honest pre-ack state
    later = reg.inbox_since(cid, 150)
    assert [m["id"] for m in later] == ["m2"]
    changed = reg.inbox_mark(cid, ["m1"], "delivered")
    assert changed == ["m1"]
    # marking read does not flip stored state (read is client-side)
    assert reg.inbox_mark(cid, ["m1"], "read") == []


def test_inbox_trim_ring(tmp_path):
    reg, _ = make_registry(tmp_path)
    ident = reg.register({"name": "Flood Phone"})
    cid = ident["client_id"]
    from companion.mobile import MAX_INBOX_MESSAGES
    for i in range(MAX_INBOX_MESSAGES + 200):
        reg.inbox_append(cid, {"id": f"f{i}", "ts": i, "body": "x" * 200})
    rows = reg.inbox_since(cid, 0, limit=99999)
    # the ring trim only fires above MAX_INBOX_BYTES; with these row sizes
    # every row survives.  f0 carries ts=0 and since-filtering is strictly
    # "newer than", so the earliest surviving row is f1.
    assert rows[-1]["id"] == f"f{MAX_INBOX_MESSAGES + 199}"
    assert rows[0]["id"] == "f1"
    assert len(rows) == MAX_INBOX_MESSAGES + 199


def test_unread_counts(tmp_path):
    reg, _ = make_registry(tmp_path)
    ident = reg.register({"name": "Unread Phone"})
    cid = ident["client_id"]
    for i in range(3):
        reg.inbox_append(cid, {"id": f"u{i}", "conv": "direct:devA",
                               "ts": 100 + i, "outgoing": False})
    reg.inbox_append(cid, {"id": "o1", "conv": "direct:devA",
                           "ts": 200, "outgoing": True})
    counts = reg.unread_counts(cid, {})
    assert counts == {"direct:devA": 3}
    counts2 = reg.unread_counts(cid, {"direct:devA": 101})
    # strictly greater: ts 102 counts, ts 100/101 are read
    assert counts2 == {"direct:devA": 1}


def test_duplicate_handling_on_register(tmp_path):
    """v1.3.1: a bare client_id can no longer adopt an existing identity.
    Re-attach requires the secret; new registrations stay one step."""
    reg, _ = make_registry(tmp_path)
    a = reg.register({"name": "Phone A"})
    # bare id WITHOUT secret → refused, existing secret not disclosed
    try:
        reg.register({"client_id": a["client_id"], "name": "Phone A"})
        raise AssertionError("bare client_id must not adopt the identity")
    except FsError as exc:
        assert exc.code == "secret_required"
    # with the CORRECT secret → legitimate re-attach, no secret returned
    b = reg.register({"client_id": a["client_id"],
                      "client_secret": a["secret"], "name": "Phone A"})
    assert b["client_id"] == a["client_id"]
    assert "secret" not in b          # browser already holds it
    assert len(reg.all()) == 1


def test_client_updated_event_pushed_on_rename(tmp_path):
    reg, _ = make_registry(tmp_path)
    ident = reg.register({"name": "Old"})
    reg.rename(ident["client_id"], "New")
    sse = reg.sse
    assert any(topic == "mobile" and data.get("type") == "client_updated"
               for topic, data in sse.published)
