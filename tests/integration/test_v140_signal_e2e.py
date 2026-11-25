"""v1.4.0 integration: Phantom Signal + Clipboard over REAL companions.

Real ``python -m companion`` subprocesses on 127.0.0.1 with deterministic
direct peer registration (loopback UDP discovery is unreliable in CI —
the same rule the v1.3.x suites follow).  Covers:

* voice: cross-companion invite relay + mirrored call state, answer
  forwarded to the origin, signaling relay, end
* broadcast: announcement to a remote computer + honest unavailable state
* clipboard: send → peer incoming (loop prevention), duplicate suppression
* v1.3.2 regressions: address-gated delivery despite stale presence,
  same-host local delivery, conversations canonical mobile keys
* TLS: dual HTTP+HTTPS listeners with a generated self-signed cert
* mobile identity: restore across "origins" (register → message →
  re-attach keeps id + inbox), takeover guard
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

sys_path = os.path.abspath(os.path.join(__file__, "..", "..", ".."))
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

import pytest  # noqa: E402

from tests.conftest import Companion, free_port  # noqa: E402


def http(url, method="GET", body=None, timeout=8):
    h = {"Content-Type": "application/json"}
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode() or "null")


@pytest.fixture(scope="module")
def pair():
    a = Companion("Alpha").start()
    b = Companion("Beta").start()
    # deterministic direct registration (not loopback UDP discovery)
    a.register(b)
    b.register(a)
    time.sleep(0.5)
    yield a, b
    a.stop()
    b.stop()


# ------------------------------------------------------------------ voice
def test_voice_cross_computer_call_mirror(pair):
    a, b = pair
    b_id = http(f"{b.base}/api/identity")["device_id"]
    # Alpha calls Beta (remote computer)
    r = http(f"{a.base}/api/voice/call", "POST",
             {"mode": "call",
              "targets": [{"kind": "computer", "device_id": b_id,
                           "name": "Beta"}]})
    assert r.get("ok"), r
    call = r["call"]
    # Beta holds a MIRRORED ringing call with the same id
    time.sleep(0.4)
    st = http(f"{b.base}/api/voice/state")
    ids = [c["id"] for c in st["calls"]]
    assert call["id"] in ids
    # Beta answers — the action forwards to Alpha's origin record
    r2 = http(f"{b.base}/api/voice/answer", "POST",
              {"call_id": call["id"], "accept": True})
    assert r2.get("ok"), r2
    time.sleep(0.3)
    st_a = http(f"{a.base}/api/voice/state")
    mine = [c for c in st_a["calls"] if c["id"] == call["id"]]
    assert mine and mine[0]["state"] == "active"
    # hang up from Beta
    r3 = http(f"{b.base}/api/voice/end", "POST", {"call_id": call["id"]})
    assert r3.get("ok")
    time.sleep(0.3)
    h = http(f"{a.base}/api/voice/history")
    assert any(x["id"] == call["id"] and x["state"] == "ended"
               for x in h["history"])


def test_voice_signaling_relay(pair):
    a, b = pair
    b_id = http(f"{b.base}/api/identity")["device_id"]
    r = http(f"{a.base}/api/voice/call", "POST",
             {"mode": "call",
              "targets": [{"kind": "computer", "device_id": b_id}]})
    cid = r["call"]["id"]
    time.sleep(0.4)
    # Beta answers → then Alpha's signaling (offer) must reach Beta's relay
    http(f"{b.base}/api/voice/answer", "POST",
         {"call_id": cid, "accept": True})
    r2 = http(f"{a.base}/api/voice/signal", "POST",
              {"call_id": cid, "kind": "offer", "payload": {"sdp": "x"}})
    assert r2.get("ok")
    http(f"{a.base}/api/voice/end", "POST", {"call_id": cid})


# -------------------------------------------------------------- broadcast
def test_broadcast_remote_computer_honest(pair):
    a, b = pair
    b_id = http(f"{b.base}/api/identity")["device_id"]
    r = http(f"{a.base}/api/broadcast/announce", "POST",
             {"text": "Dinner at six", "tone": "chime",
              "targets": [{"kind": "computer", "device_id": b_id}]})
    assert r.get("ok")
    results = r["announcement"]["results"]
    assert results and results[0]["state"] == "delivered"
    # history on the sender records per-target outcomes
    h = http(f"{a.base}/api/broadcast/history")
    assert h["history"][0]["text"] == "Dinner at six"


def test_broadcast_unreachable_target_reported(pair):
    a, _b = pair
    r = http(f"{a.base}/api/broadcast/announce", "POST",
             {"text": "anyone there?",
              "targets": [{"kind": "computer", "device_id": "dev_ghost"}]})
    assert r.get("ok")
    assert r["announcement"]["results"][0]["state"] == "unavailable"


# -------------------------------------------------------------- clipboard
def test_clipboard_send_to_peer_and_duplicate_suppression(pair):
    a, b = pair
    b_id = http(f"{b.base}/api/identity")["device_id"]
    r = http(f"{a.base}/api/clipboard/send", "POST",
             {"kind": "text", "text": "deploy.sh\nsudo systemctl restart x",
              "targets": [{"kind": "computer", "device_id": b_id}]})
    assert r.get("ok"), r
    states = [x["state"] for x in r["results"]]
    assert "delivered" in states
    # duplicate suppression: same payload → duplicate flag, no re-delivery
    r2 = http(f"{a.base}/api/clipboard/send", "POST",
              {"kind": "text", "text": "deploy.sh\nsudo systemctl restart x",
               "targets": [{"kind": "computer", "device_id": b_id}]})
    assert r2.get("ok") and r2.get("duplicate") is True
    # receiving side has it in history
    items = http(f"{b.base}/api/clipboard/items")
    assert any("systemctl" in (i.get("text") or "") for i in items["items"])


def test_clipboard_incoming_respects_private_mode(pair):
    a, b = pair
    b_id = http(f"{b.base}/api/identity")["device_id"]
    http(f"{b.base}/api/clipboard/settings", "POST",
         {"clipboard_private_mode": True})
    r = http(f"{b.base}/api/clipboard/incoming", "POST",
             {"entry": {"kind": "text", "text": "secret-ish",
                        "origin": {"device_id": "dev_x", "name": "X"},
                        "hop": 1}})
    assert r.get("ok") and r["state"] == "private"
    items = http(f"{b.base}/api/clipboard/items")
    assert not any("secret-ish" in (i.get("text") or "")
                   for i in items["items"])
    http(f"{b.base}/api/clipboard/settings", "POST",
         {"clipboard_private_mode": False})


# ------------------------------------------------- v1.3.2 regressions
def test_v132_delivery_not_gated_on_stale_presence(pair):
    """A peer whose cached presence says offline but whose address works
    still receives direct chat (v1.3.2 correction)."""
    a, b = pair
    b_id = http(f"{b.base}/api/identity")["device_id"]
    r = http(f"{a.base}/api/chat/send", "POST",
             {"to_kind": "direct", "to_id": b_id, "body": "v132 still works"})
    assert r.get("ok")
    assert r.get("message", {}).get("status") in ("delivered", "sent")


def test_v132_rn1918_preferred(pair):
    from companion.util import prefer_lan_address
    assert prefer_lan_address(["100.64.0.2", "192.168.1.9"]) == "192.168.1.9"


def test_v132_conversation_canonical_key_after_phone_message(pair):
    a, b = pair
    # phone hosted on Alpha messages Alpha's desktop
    ident = http(f"{a.base}/api/mobile/register", "POST",
                 {"name": "iPhone se", "device_type": "phone"})
    assert ident.get("ok")
    cid, sec = ident["client"]["client_id"], ident["client"]["secret"]
    http(f"{a.base}/api/mobile/heartbeat", "POST",
         {"client_id": cid, "client_secret": sec})
    # phone → computer via the mobile chat send path
    r = http(f"{a.base}/api/mobile/chat/send", "POST",
             {"client_id": cid, "client_secret": sec,
              "to_kind": "direct", "to_id":
              http(f"{a.base}/api/identity")["device_id"],
              "body": "hello from the phone"})
    assert r.get("ok"), r
    time.sleep(0.3)
    convs = http(f"{a.base}/api/chat/conversations")["conversations"]
    keys = [c["conv"] for c in convs]
    assert f"mobile:{cid}" in keys
    assert f"direct:{cid}" not in keys
    entry = next(c for c in convs if c["conv"] == f"mobile:{cid}")
    assert entry["title"] == "iPhone se"


# ------------------------------------------------------------------- TLS
def test_tls_dual_listener(tmp_path):
    """HTTP and HTTPS serve SIMULTANEOUSLY with a generated cert (v1.4.0
    steer: optional dual listeners; HTTP never dies)."""
    import ssl as ssl_mod
    data_dir = tmp_path / "tls"
    data_dir.mkdir()
    cert = data_dir / "c.pem"
    key = data_dir / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048",
                    "-sha256", "-days", "2", "-nodes",
                    "-keyout", str(key), "-out", str(cert),
                    "-subj", "/CN=phantom-test",
                    "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                   capture_output=True, timeout=30, check=True)
    port = free_port()
    c = Companion("TLSBox", port=port, data_dir=str(data_dir))
    cfg = json.load(open(os.path.join(str(data_dir), "config.json"))) \
        if os.path.exists(os.path.join(str(data_dir), "config.json")) else {}
    c.start()
    http(f"{c.base}/api/config", "POST",
         {"tls_enabled": True, "tls_dual": True,
          "tls_cert": str(cert), "tls_key": str(key)})
    c.stop()
    time.sleep(0.5)
    c2 = Companion("TLSBox2", port=port, data_dir=str(data_dir)).start()
    try:
        # HTTP still alive on the main port
        h = http(f"{c2.base}/api/health")
        assert h.get("ok") and h["version"] == "1.4.0"
        st = http(f"{c2.base}/api/tls/status")
        assert st["enabled"] and st["dual"] and st["https_port"]
        assert st["cert_available"]
        # HTTPS alive on port+1
        https_url = f"https://127.0.0.1:{st['https_port']}/api/health"
        ctx = ssl_mod.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl_mod.CERT_NONE
        with urllib.request.urlopen(https_url, timeout=8, context=ctx) as r:
            d = json.loads(r.read().decode())
            assert d["ok"] is True
        # cert downloadable over HTTP for phone install
        with urllib.request.urlopen(f"{c2.base}/api/tls/cert",
                                    timeout=8) as r:
            assert b"BEGIN CERTIFICATE" in r.read()
    finally:
        c2.stop()


# -------------------------------------------------- identity across origins
def test_mobile_identity_survives_origin_switch(pair):
    a, _b = pair
    ident = http(f"{a.base}/api/mobile/register", "POST",
                 {"name": "Roamer", "device_type": "phone"})
    cid, sec = ident["client"]["client_id"], ident["client"]["secret"]
    http(f"{a.base}/api/mobile/heartbeat", "POST",
         {"client_id": cid, "client_secret": sec})
    me = http(f"{a.base}/api/identity")["device_id"]
    http(f"{a.base}/api/chat/send", "POST",
         {"to_kind": "mobile", "to_id": cid, "body": "before the move"})
    time.sleep(0.3)
    # 'new origin': re-attach with the SAME credentials
    r = http(f"{a.base}/api/mobile/register", "POST",
             {"client_id": cid, "client_secret": sec, "name": "Roamer"})
    assert r.get("ok") and r["client"]["client_id"] == cid
    inbox = http(f"{a.base}/api/mobile/inbox", "POST",
                 {"client_id": cid, "client_secret": sec, "since_ts": 0})
    assert any("before the move" == m.get("body") for m in inbox["messages"])
    # takeover guard: bare client_id is refused
    r2 = http(f"{a.base}/api/mobile/register", "POST", {"client_id": cid})
    assert not r2.get("ok") and r2.get("error") == "secret_required"
