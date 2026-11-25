"""v1.3.2 routing / discovery / conversations repairs."""

from __future__ import annotations

import json
from pathlib import Path

from companion.chat import ChatEngine, conversation_key
from companion.peers import PeerRegistry
from companion.util import prefer_lan_address


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


def test_prefer_lan_over_cgnat():
    assert prefer_lan_address(["100.113.89.37", "10.0.0.104"]) == "10.0.0.104"
    assert prefer_lan_address(["10.0.0.202"]) == "10.0.0.202"


def test_conversations_survives_remote_hosted_phone(tmp_path: Path):
    """Windows (and any non-host) must not 500 when history contains mobile: keys
    for phones hosted elsewhere — the v1.3.1 sidebar crash."""
    reg = PeerRegistry(str(tmp_path))
    peer, _ = reg.observe_beacon({
        "device_id": "dev_host", "name": "Station", "api_port": 8415,
        "mobile_clients": [{"client_id": "mob_abc", "name": "iPhone se",
                            "online": True}],
    }, "10.0.0.202")
    assert peer.status == "online"

    chat = ChatEngine(str(tmp_path), _Cfg(), reg, None, None, lambda: {"device_id": "dev_windows", "name": "Windows"})
    chat.wire_mobile(_FakeMobile())  # local registry empty — phone is remote

    # Seed a conversation key that only exists because THIS computer messaged
    # a phone hosted by Station.
    msg = {
        "id": "msg_x", "ts": 1000, "from": {"device_id": "dev_windows",
                                            "name": "Windows", "kind": "computer"},
        "to_kind": "mobile", "to_id": "mob_abc",
        "body": "hello phone", "outgoing": True, "status": "sent",
        "conv": conversation_key("mobile", "mob_abc"),
    }
    chat._messages.append(msg)

    convs = chat.conversations()
    assert any(c["conv"] == "mobile:mob_abc" for c in convs)
    hit = next(c for c in convs if c["conv"] == "mobile:mob_abc")
    assert hit["title"] == "iPhone se"
    assert hit["online"] is True


def test_direct_resolve_tries_offline_peer_with_address(tmp_path: Path):
    reg = PeerRegistry(str(tmp_path))
    peer, _ = reg.observe_beacon({
        "device_id": "dev_station", "name": "Station", "api_port": 8415,
    }, "10.0.0.202")
    reg.mark_offline("dev_station")
    assert reg.get("dev_station").status == "offline"
    assert reg.get("dev_station").addresses

    chat = ChatEngine(str(tmp_path), _Cfg(), reg, None, None, lambda: {"device_id": "dev_windows", "name": "Windows"})
    targets = chat._resolve_targets("direct", "dev_station")
    assert len(targets) == 1
    assert targets[0]["address"] == "10.0.0.202"


def test_direct_resolve_local_host_for_mobile_to_self(tmp_path: Path):
    reg = PeerRegistry(str(tmp_path))
    chat = ChatEngine(str(tmp_path), _Cfg(), reg, None, None, lambda: {"device_id": "dev_station", "name": "Station"})
    targets = chat._resolve_targets("direct", "dev_station")
    assert targets and targets[0].get("local") is True


def test_discovery_accepts_protocol_2_beacon_filter():
    """Mirror the rx gate: pn>=1 with kind=beacon must pass."""
    # Pure logic check matching discovery.py condition.
    def accept(packet):
        pn = packet.get("pn")
        return packet.get("kind") == "beacon" and isinstance(pn, int) and pn >= 1

    assert accept({"pn": 1, "kind": "beacon"})
    assert accept({"pn": 2, "kind": "beacon"})
    assert not accept({"pn": 2, "kind": "probe"})
    assert not accept({"pn": 0, "kind": "beacon"})
    assert not accept({"kind": "beacon"})  # missing pn
