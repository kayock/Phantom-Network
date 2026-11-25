"""v1.4.0 GhostChat conversation-interface repairs (steer §3).

Covers: canonical mobile conversation keys, legacy direct:mob_* merge,
mobile-name resolution for remote-hosted phones, orphan (temporary test
client) hiding without deleting history, ordering, null-safe sender
search, and the v1.3.2 address-gated delivery with presence refresh.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(__file__, "..", "..", "..")))

from companion.chat import ChatEngine, conversation_key  # noqa: E402
from companion.util import now_ms  # noqa: E402
from companion.peers import PeerRegistry  # noqa: E402


class _Cfg(dict):
    def get(self, k, default=None):
        return super().get(k, default)

    def set(self, k, v):
        self[k] = v


class _FakeMobile:
    def __init__(self):
        self._clients = {}

    def get(self, cid):
        return self._clients.get(cid)

    def all(self):
        return list(self._clients.values())

    def inbox_append(self, cid, msg):
        self._inbox = getattr(self, "_inbox", {})
        self._inbox.setdefault(cid, []).append(msg)

    def public_list(self, *a, **k):
        return []

    def set_online(self, cid, online=True):
        c = self._clients.get(cid)
        if c is not None:
            c["online"] = online


class _Identity:
    def __init__(self, did="dev_me", name="Station"):
        self.did = did
        self.name = name

    def __call__(self):
        return {"device_id": self.did, "name": self.name,
                "kind": "computer"}


class _Client:
    def __init__(self):
        self.calls = []

    def request(self, method, url, body=None, timeout=5):
        self.calls.append((method, url))
        return {"acked": True, "id": (body or {}).get("message", {})
                .get("id", "")}


class _Events:
    def emit(self, *a, **k):
        pass


def _engine(tmp_path):
    cfg = _Cfg()
    eng = ChatEngine(str(tmp_path), cfg, PeerRegistry(str(tmp_path)),
                     _Events(), None, _Identity(), None)
    eng.wire_mobile(_FakeMobile())
    return eng


def test_phone_conversation_canonical_key_on_host(tmp_path):
    """A phone sending to its host produces ONE mobile: conv — not a
    direct:mob_* duplicate (the v1.3.1-era duplicate-conversation bug)."""
    eng = _engine(tmp_path)
    msg = {"id": "m1", "ts": now_ms(), "from": {"device_id": "mob_abc",
                                            "name": "iPhone se",
                                            "kind": "mobile",
                                            "host_device_id": "dev_me"},
           "to_kind": "direct", "to_id": "dev_me", "body": "hi from phone",
           "conv": conversation_key("direct", "dev_me")}
    eng._deliver({"id": "dev_me", "kind": "computer", "local": True,
                  "address": "127.0.0.1", "port": 0}, msg)
    convs = eng.conversations()
    keys = [c["conv"] for c in convs]
    assert "mobile:mob_abc" in keys
    assert "direct:mob_abc" not in keys
    entry = next(c for c in convs if c["conv"] == "mobile:mob_abc")
    assert entry["title"] == "iPhone se"
    assert entry["icon"] == "📱"


def test_legacy_direct_mob_conversation_merged(tmp_path):
    """Pre-existing direct:mob_* rows merge into mobile:<id> (view-level;
    stored messages untouched)."""
    eng = _engine(tmp_path)
    eng._store_and_publish({
        "id": "m2", "ts": now_ms(), "from": {"device_id": "mob_old",
                                             "name": "iPad"},
        "to_kind": "direct", "to_id": "dev_me", "body": "legacy",
        "conv": conversation_key("direct", "mob_old"), "outgoing": False,
        "status": "delivered"})
    convs = eng.conversations()
    keys = [c["conv"] for c in convs]
    assert "mobile:mob_old" in keys
    assert "direct:mob_old" not in keys
    entry = next(c for c in convs if c["conv"] == "mobile:mob_old")
    assert entry["title"] in ("iPad", "mob_old")   # name if hosted, else id


def test_remote_hosted_phone_title_resolution(tmp_path):
    """A phone hosted by ANOTHER companion resolves its name from peer
    identity payloads — never shows the raw mob_ id."""
    eng = _engine(tmp_path)
    # pretend the registry knows a peer hosting the phone
    peer, _created = eng.registry.observe_beacon(
        {"device_id": "dev_host", "name": "Windows-box", "api_port": 8415},
        "127.0.0.1")
    peer.mobile_clients = [{"client_id": "mob_remote", "name": "iPhone se",
                            "online": True}]
    title = eng._remote_mobile_title("mob_remote")
    assert title == "iPhone se"
    assert eng._remote_mobile_online("mob_remote") is True
    assert eng._remote_mobile_title("mob_unknown") == ""


def test_orphan_temp_client_hidden_but_history_kept(tmp_path):
    """A vanished temporary test client disappears from active contacts
    after 24 h; its messages stay in the store."""
    eng = _engine(tmp_path)
    old_ts = int(time.time() * 1000) - 25 * 3600 * 1000
    eng._store_and_publish({
        "id": "m3", "ts": old_ts,
        "from": {"device_id": "mob_ghost", "name": "temp test",
                 "kind": "mobile"},
        "to_kind": "mobile", "to_id": "dev_me", "body": "test clutter",
        "conv": conversation_key("mobile", "mob_ghost"),
        "outgoing": False, "status": "delivered"})
    convs = eng.conversations()
    assert all(c["conv"] != "mobile:mob_ghost" for c in convs)
    # history retained
    hist = eng.history("mobile:mob_ghost")
    assert hist and hist[0]["body"] == "test clutter"


def test_recent_orphan_still_visible(tmp_path):
    """A recently-active unknown client stays visible (grace period)."""
    eng = _engine(tmp_path)
    eng._store_and_publish({
        "id": "m4", "ts": now_ms(),
        "from": {"device_id": "mob_new", "name": "new phone",
                 "kind": "mobile"},
        "to_kind": "mobile", "to_id": "dev_me", "body": "just arrived",
        "conv": conversation_key("mobile", "mob_new"),
        "outgoing": False, "status": "delivered"})
    convs = eng.conversations()
    assert any(c["conv"] == "mobile:mob_new" for c in convs)


def test_conversations_ordered_by_last_ts(tmp_path):
    eng = _engine(tmp_path)
    eng._store_and_publish({
        "id": "a", "ts": 100, "from": {"device_id": "dev_x", "name": "X"},
        "to_kind": "direct", "to_id": "dev_x", "body": "old",
        "conv": conversation_key("direct", "dev_x"), "outgoing": True,
        "status": "sent"})
    eng._store_and_publish({
        "id": "b", "ts": 900, "from": {"device_id": "dev_y", "name": "Y"},
        "to_kind": "direct", "to_id": "dev_y", "body": "new",
        "conv": conversation_key("direct", "dev_y"), "outgoing": True,
        "status": "sent"})
    convs = eng.conversations()
    ts_list = [c["last_ts"] for c in convs]
    assert ts_list == sorted(ts_list, reverse=True)


def test_search_null_safe_sender_name(tmp_path):
    eng = _engine(tmp_path)
    eng._store_and_publish({
        "id": "m5", "ts": 500, "from": {"device_id": "system",
                                        "kind": "computer"},
        "to_kind": "system", "to_id": "alerts", "body": "alert text",
        "conv": "system:alerts", "outgoing": False, "status": "delivered"})
    r = eng.history(q="alert")
    assert any(m["id"] == "m5" for m in r)
