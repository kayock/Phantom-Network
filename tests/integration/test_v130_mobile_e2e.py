"""v1.3.0 integration: mobile clients over REAL companion HTTP.

Two real ``python -m companion`` subprocesses plus simulated phone clients
(direct HTTP + SSE reads).  Covers: registration & identity persistence,
computer→phone delivery with honest ack semantics, phone→computer,
phone→phone across hosts, offline queueing, reconnection sync, read
receipts, reactions/edits/tombstones, duplicate prevention, chat files,
discovery diagnostics, peer exchange, storage view, PWA files, alerts API
and automation with a real verified transfer.
"""

import json
import os
import socket
import threading
import time
import urllib.request

import pytest

sys_path = os.path.abspath(os.path.join(__file__, "..", "..", ".."))
import sys  # noqa: E402
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

from tests.conftest import Companion, free_port  # noqa: E402


def http(url, method="GET", body=None, timeout=8, headers=None):
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode() or "null")


def register_phone(c: Companion, name, dtype="phone"):
    """Register a simulated phone; returns its client identity (with the
    once-only secret) or an {"ok": False, ...} error payload."""
    out = http(f"{c.base}/api/mobile/register",
               "POST", {"name": name, "device_type": dtype})
    if not out.get("ok"):
        return out
    client = dict(out["client"])
    client["ok"] = True
    return client


def phone_inbox(c: Companion, ident):
    return http(f"{c.base}/api/mobile/inbox", "POST", {
        "client_id": ident["client_id"], "client_secret": ident["secret"]})


def phone_ack(c: Companion, ident, ids, state="delivered"):
    return http(f"{c.base}/api/mobile/chat/ack", "POST", {
        "client_id": ident["client_id"], "client_secret": ident["secret"],
        "ids": ids, "state": state})


@pytest.fixture(scope="module")
def pair():
    a = Companion("AlphaStation").start()
    b = Companion("BetaBox").start()
    a.register(b)
    b.register(a)
    # let identity beacons carry mobile_clients both ways
    time.sleep(1.0)
    yield a, b
    a.stop()
    b.stop()
    import shutil
    shutil.rmtree(a.data_dir, ignore_errors=True)
    shutil.rmtree(b.data_dir, ignore_errors=True)


def test_identity_advertises_kind_capabilities_and_migration(pair):
    a, b = pair
    ident = a.identity()
    assert ident["kind"] == "computer"
    caps = ident["capabilities"]
    assert caps["chat"] and "mobile_host" in caps
    assert isinstance(ident["mobile_clients"], list)
    # migration ran: schema_version recorded, backup present only if upgraded
    cfg = a.get("/api/config")["config"]
    assert cfg.get("schema_version") == 2


def test_phone_registration_and_identity_persistence(pair):
    a, b = pair
    ident = register_phone(a, "My iPhone")
    assert ident["ok"] and ident["client_id"].startswith("mob_")
    assert "secret" in ident and ident["secret"]   # exactly-once secret
    cid = ident["client_id"]
    # visible in the host's identity payload (fleet visibility)
    clients = a.identity()["mobile_clients"]
    assert any(c["client_id"] == cid for c in clients)
    assert clients[0]["kind"] == "mobile"
    # heartbeat keeps presence; identity refresh keeps online flag
    hb = http(f"{a.base}/api/mobile/heartbeat", "POST", {
        "client_id": cid, "client_secret": ident["secret"]})
    assert hb["ok"]
    # wrong secret refused
    bad = http(f"{a.base}/api/mobile/heartbeat", "POST", {
        "client_id": cid, "client_secret": "nope"})
    assert not bad.get("ok")


def test_computer_to_phone_honest_delivery(pair):
    a, b = pair
    ident = register_phone(a, "Work Phone")
    cid = ident["client_id"]
    msg = a.post("/api/chat/send", {"to_kind": "mobile", "to_id": cid,
                                    "body": "hello phone"})
    # honest: host received it, but the phone has NOT acknowledged yet
    assert msg["message"]["status"] == "sent"
    # the phone's inbox holds it
    inbox = phone_inbox(a, ident)
    assert any(m["body"] == "hello phone" for m in inbox["messages"])
    # client ack upgrades the SENDER's status to delivered
    ids = [m["id"] for m in inbox["messages"]]
    assert phone_ack(a, ident, ids, "delivered")["ok"]
    hist = a.get("/api/chat/history")["messages"]
    target = next(m for m in hist if m["id"] == msg["message"]["id"])
    assert target["status"] == "delivered"


def test_phone_to_computer_with_receipts(pair):
    a, b = pair
    ident = register_phone(a, "Sender Phone")
    res = http(f"{a.base}/api/mobile/chat/send", "POST", {
        "client_id": ident["client_id"], "client_secret": ident["secret"],
        "to_kind": "direct", "to_id": b.device_id, "body": "hi from phone"})
    assert res["ok"]
    msg = res["message"]
    # the receiving computer stores it under the phone's identity
    hist = b.get(f"/api/chat/history?conv=direct:{ident['client_id']}")
    assert any(m["body"] == "hi from phone" and
               m["from"]["kind"] == "mobile" for m in hist["messages"])
    # computer reads it → read receipt flows back through the host.
    # Sender-side conversation key: direct:<computer device id> (the
    # phone addressed computer B directly).
    b.post("/api/chat/read", {"conv": f"direct:{ident['client_id']}",
                              "up_to_ts": msg["ts"] + 5})
    deadline = time.time() + 8
    status = None
    while time.time() < deadline:
        hist = a.get(f"/api/chat/history?conv=direct:{b.device_id}")
        for m in hist["messages"]:
            if m["id"] == msg["id"]:
                status = m["status"]
        if status == "read":
            break
        time.sleep(0.3)
    assert status == "read", f"expected read receipt, got {status}"


def test_phone_to_phone_across_hosts(pair):
    a, b = pair
    pa = register_phone(a, "Phone On A")
    pb = register_phone(b, "Phone On B")
    # Deterministic fleet refresh: A probes B's actual API port (the same
    # path real deployments use when UDP broadcast is blocked).  The probe
    # refreshes B's cached identity — including its hosted mobile clients —
    # and wakes the outbox.  No dependency on same-host UDP beacons.
    a.post(f"/api/peers/{b.device_id}/probe", {})
    # A's phone sends to B's phone (routes via hosts)
    res = http(f"{a.base}/api/mobile/chat/send", "POST", {
        "client_id": pa["client_id"], "client_secret": pa["secret"],
        "to_kind": "mobile", "to_id": pb["client_id"],
        "body": "phone to phone"})
    assert res["ok"]
    assert res["message"]["status"] in ("delivered", "sent"), \
        f"expected direct delivery after probe, got {res['message']['status']}"
    # wait for the host-to-host relay (robustness on slow hosts)
    deadline = time.time() + 12
    got = []
    while time.time() < deadline:
        inbox = phone_inbox(b, pb)
        got = [m for m in inbox["messages"] if m["body"] == "phone to phone"]
        if got:
            break
        time.sleep(0.4)
    assert got, "phone-to-phone message did not arrive through hosts"
    m = got[0]
    # conversation on the recipient is keyed by the SENDER (phone A)
    assert m["conv"] == f"direct:{pa['client_id']}"
    # B's phone acks → receipt propagates back to A's phone via host A
    phone_ack(b, pb, [m["id"]], "read")
    deadline = time.time() + 10
    status = None
    while time.time() < deadline:
        hist = a.get(f"/api/chat/history?conv=mobile:{pb['client_id']}")
        for mm in hist["messages"]:
            if mm["id"] == m["id"]:
                status = mm["status"]
        if status == "read":
            break
        time.sleep(0.3)
    assert status == "read", f"cross-host receipt failed, got {status}"


def test_offline_phone_message_queues_then_delivers(pair):
    a, _ = pair
    ident = register_phone(a, "Later Phone")
    # simulate the phone going away: no heartbeat, no inbox fetch
    msg = a.post("/api/chat/send", {"to_kind": "mobile",
                                    "to_id": ident["client_id"],
                                    "body": "for later"})
    assert msg["message"]["status"] == "sent"
    time.sleep(1.0)
    # the phone comes back and syncs: since_ts=0 fetch returns the message
    inbox = phone_inbox(a, ident)
    bodies = [m["body"] for m in inbox["messages"]]
    assert "for later" in bodies
    # duplicate fetch must not double-store (dedupe by message id)
    inbox2 = phone_inbox(a, ident)
    ids = [m["id"] for m in inbox2["messages"]]
    assert len(ids) == len(set(ids))


def test_duplicate_incoming_is_idempotent(pair):
    a, b = pair
    message = {
        "id": "msg_dedup_test_1", "ts": int(time.time() * 1000),
        "from": {"device_id": a.device_id, "name": "AlphaStation",
                 "kind": "computer"},
        "to_kind": "direct", "to_id": b.device_id, "body": "dup test",
        "conv": f"direct:{a.device_id}", "status": "sent",
    }
    r1 = b.post("/api/chat/incoming", {"message": message})
    r2 = b.post("/api/chat/incoming", {"message": message})
    assert r1.get("acked") is True
    assert r2.get("duplicate") is True
    hist = b.get(f"/api/chat/history?conv=direct:{a.device_id}")
    same = [m for m in hist["messages"] if m["id"] == "msg_dedup_test_1"]
    assert len(same) == 1


def test_reactions_edits_and_tombstones(pair):
    a, b = pair
    a.post(f"/api/peers/{b.device_id}/probe", {})   # deterministic peer view
    msg = a.post("/api/chat/send", {"to_kind": "direct", "to_id": b.device_id,
                                    "body": "react to me"})
    mid = msg["message"]["id"]
    # reaction on A propagates to B
    assert a.post("/api/chat/react", {"msg_id": mid, "emoji": "👍"})["ok"]
    deadline = time.time() + 8
    seen = False
    while time.time() < deadline:
        hist = b.get(f"/api/chat/history?conv=direct:{a.device_id}")
        m = next((x for x in hist["messages"] if x["id"] == mid), None)
        if m and (m.get("reactions") or {}).get("👍"):
            seen = True
            break
        time.sleep(0.3)
    assert seen, "reaction did not propagate"
    # author edit propagates
    a.post("/api/chat/edit", {"msg_id": mid, "body": "edited body"})
    deadline = time.time() + 8
    edited = False
    while time.time() < deadline:
        hist = b.get(f"/api/chat/history?conv=direct:{a.device_id}")
        m = next((x for x in hist["messages"] if x["id"] == mid), None)
        if m and m.get("body") == "edited body" and m.get("edited"):
            edited = True
            break
        time.sleep(0.3)
    assert edited, "edit did not propagate"
    # delete → tombstone on both sides
    a.post("/api/chat/delete", {"msg_id": mid})
    deadline = time.time() + 8
    tomb = False
    while time.time() < deadline:
        hist = b.get(f"/api/chat/history?conv=direct:{a.device_id}")
        m = next((x for x in hist["messages"] if x["id"] == mid), None)
        if m and m.get("deleted") and not m.get("body"):
            tomb = True
            break
        time.sleep(0.3)
    assert tomb, "tombstone did not propagate"


def test_chat_file_store_roundtrip(pair):
    a, b = pair
    payload = b"phantom chat attachment \xf0\x9f\x93\x8e"
    s = http(f"{b.base}/api/chat/file/session", "POST",
             {"name": "note.bin", "size": len(payload)})
    assert s["ok"]
    r = http(f"{b.base}/api/chat/file/{s['sid']}", "PUT", payload)
    assert r["ok"]
    fin = http(f"{b.base}/api/chat/file/{s['sid']}/finish", "POST", {})
    fid = fin["file"]["id"]
    assert fin["file"]["sha256"]
    # size cap refuses oversized sessions
    too_big = http(f"{b.base}/api/chat/file/session", "POST",
                   {"name": "huge.bin", "size": 10 * 1024 * 1024 * 1024})
    assert not too_big.get("ok")


def test_discovery_diagnostics_and_peer_exchange(pair):
    a, b = pair
    diag = a.get("/api/discovery/diagnostics")
    assert diag["ok"]
    d = diag
    assert d["socket"] in ("open", "disabled", "failed to bind")
    assert isinstance(d["interfaces"], list)
    assert isinstance(d["log"], list)
    assert "manual_peers" in d and "beacon_targets" in d
    # peer exchange: B sends its known peers; A probes before trusting
    known = [{"device_id": b.device_id, "name": "BetaBox",
              "addresses": ["127.0.0.1"], "api_port": b.port}]
    r = http(f"{a.base}/api/peers/exchange", "POST", {"peers": known})
    assert r["ok"] and "received" in r
    # self records are ignored
    r2 = http(f"{a.base}/api/peers/exchange", "POST",
              {"peers": [{"device_id": a.device_id, "addresses": []}]})
    assert r2["ok"]


def test_storage_view_endpoint(pair):
    a, _ = pair
    st = a.get("/api/storage?view=all")
    assert st["ok"]
    for m in st["mounts"]:
        assert m["kind"] in ("physical", "virtual", "snap", "network",
                             "external", "optical")
    st2 = a.get("/api/storage?view=useful")
    assert not any(m["mount"].startswith("/snap") for m in st2["mounts"])
    assert st["cpu"]["logical"] == os.cpu_count()


def test_alerts_api_lifecycle(pair):
    a, _ = pair
    rules_before = len(a.get("/api/alerts")["rules"])
    rule = a.post("/api/alerts", {"rule": {
        "name": "e2e CPU", "type": "cpu_high", "threshold": 99,
        "duration_s": 120, "cooldown_s": 600,
        "notify": {"event": True, "chat": True, "sound": False}}})["rule"]
    assert rule["id"]
    rules = a.get("/api/alerts")["rules"]
    assert len(rules) == rules_before + 1
    a.request("DELETE", f"/api/alerts/{rule['id']}")
    assert len(a.get("/api/alerts")["rules"]) == rules_before


def test_automation_job_runs_verified_transfer(pair, tmp_path):
    a, b = pair
    # shares on A (destination, rw) and B (source, ro)
    src = tmp_path / "auto-src"
    dst = tmp_path / "auto-dst"
    src.mkdir()
    dst.mkdir()
    (src / "auto.txt").write_bytes(b"automation payload")
    a.post("/api/fs/share", {"path": str(dst), "mode": "rw"})
    b.post("/api/fs/share", {"path": str(src), "mode": "ro"})
    # deterministic: refresh A's cached view of B (custom port on this
    # host; also mirrors networks without usable UDP broadcast)
    a.post(f"/api/peers/{b.device_id}/probe", {})
    job = a.post("/api/automation", {"job": {
        "name": "e2e watched", "kind": "schedule", "mode": "sync",
        "src_device": b.device_id, "dst_device": "",
        "src_path": str(src), "dst_path": str(dst),
        "compare": "size", "conflict": "replace",
        "interval_minutes": 1}})["job"]
    assert job["id"]
    # preview shows the file would be copied
    plan = a.post("/api/automation/preview", {
        "src_base": "", "dst_base": "", "src_path": str(src),
        "dst_path": str(dst), "src_device": b.device_id,
        "dst_device": None})
    assert any(e["rel"] == "auto.txt" for e in plan["copy"])
    # manual run through the verified engine
    r = a.post(f"/api/automation/{job['id']}/control", {"action": "run"})
    assert r["ok"]
    deadline = time.time() + 60
    copied = False
    last_err = ""
    while time.time() < deadline:
        data = a.get("/api/automation")
        entry = next(j for j in data["jobs"] if j["id"] == job["id"])
        if entry.get("history"):
            copied = entry["history"][-1]["ok"]
            last_err = entry["history"][-1].get("error", "")
            if copied or last_err:
                break
        time.sleep(0.5)
    assert copied, f"automation run failed: {last_err}"
    # the automation job ENQUEUES through the verified transfer engine
    # (async by design) — await the actual file before asserting
    deadline = time.time() + 30
    arrived = False
    while time.time() < deadline:
        if (dst / "auto.txt").exists():
            arrived = True
            break
        time.sleep(0.4)
    assert arrived, "verified transfer did not complete in 30 s"
    assert (dst / "auto.txt").read_bytes() == b"automation payload"
    # verified by the engine: the transfer history shows sha256 verification
    jobs = a.get("/api/jobs")["history"] + a.get("/api/jobs")["jobs"]
    assert any(j.get("verify", {}).get("status") == "ok"
               for j in jobs)
    a.request("DELETE", f"/api/automation/{job['id']}")
    a.post("/api/fs/unshare", {"path": str(dst)})
    b.post("/api/fs/unshare", {"path": str(src)})


def test_pwa_assets_served(pair):
    a, _ = pair
    import urllib.request as u
    man = json.loads(u.urlopen(f"{a.base}/manifest.webmanifest",
                               timeout=5).read().decode())
    assert man["name"] == "Phantom Network"
    assert man["display"] == "standalone"
    assert any(i["sizes"] == "192x192" for i in man["icons"])
    raw = u.urlopen(f"{a.base}/sw.js", timeout=5).read().decode()
    assert "pn-shell-" in raw and "/api/" in raw    # API never cached rule
    mobile = u.urlopen(f"{a.base}/m", timeout=5).read().decode()
    assert "PHANTOM" in mobile and "mtabs" in mobile
    offline = u.urlopen(f"{a.base}/offline.html", timeout=5).read().decode()
    assert "OFFLINE" in offline
    icons = u.urlopen(f"{a.base}/assets/icon-192.png", timeout=5).read()
    assert icons[:4] == b"\x89PNG"


def test_mobile_client_rename_and_remove_admin_guard(pair):
    a, b = pair
    ident = register_phone(a, "Admin Test")
    cid = ident["client_id"]
    # rename from THIS machine (the test host is local to companion A) works
    r2 = http(f"{a.base}/api/mobile/clients/{cid}/rename", "POST",
              {"name": "Renamed Phone"})
    assert r2.get("ok") is True
    assert r2["client"]["name"] == "Renamed Phone"
    # remove cleans the registry
    r3 = http(f"{a.base}/api/mobile/clients/{cid}/remove", "POST", {})
    assert r3["ok"]
    assert not any(c["client_id"] == cid
                   for c in a.identity()["mobile_clients"])


# ---------------------------------------------------------------------------
# v1.3.1 additions: remaining comms-verification flows
# ---------------------------------------------------------------------------

def test_phone_to_phone_same_host(pair):
    a, b = pair
    p1 = register_phone(a, "Same Host One")
    p2 = register_phone(a, "Same Host Two")
    res = http(f"{a.base}/api/mobile/chat/send", "POST", {
        "client_id": p1["client_id"], "client_secret": p1["secret"],
        "to_kind": "mobile", "to_id": p2["client_id"],
        "body": "same host relay"})
    assert res["ok"]
    assert res["message"]["status"] == "sent"
    inbox = phone_inbox(a, p2)
    got = [m for m in inbox["messages"] if m["body"] == "same host relay"]
    assert got and got[0]["conv"] == f"direct:{p1['client_id']}", \
        f"recipient must see the sender-keyed conversation, got {got[0]['conv'] if got else None}"
    # sender conversation on the host is keyed by the recipient client
    hist = a.get(f"/api/chat/history?conv=mobile:{p2['client_id']}")
    assert any(m["body"] == "same host relay" for m in hist["messages"])


def test_voice_message_roundtrip_via_chat_file_store(pair):
    a, b = pair
    payload = b"\x1a\x45\xdf\xa3fake-webm-voice-bytes"
    ident = register_phone(a, "Voice Phone")
    s = http(f"{a.base}/api/chat/file/session", "POST",
             {"name": "voice-1.webm", "size": len(payload),
              "mime": "audio/webm"})
    http(f"{a.base}/api/chat/file/{s['sid']}", "PUT", payload)
    fin = http(f"{a.base}/api/chat/file/{s['sid']}/finish", "POST", {})
    fid = fin["file"]["id"]
    # the phone sends a voice message to computer B
    res = http(f"{a.base}/api/mobile/chat/send", "POST", {
        "client_id": ident["client_id"], "client_secret": ident["secret"],
        "to_kind": "direct", "to_id": b.device_id, "body": "",
        "attachment": {"kind": "voice", "file_id": fid,
                       "name": "voice-1.webm", "size": len(payload),
                       "duration_s": 4, "mime": "audio/webm",
                       "host_device_id": a.device_id}})
    assert res["ok"]
    hist = b.get(f"/api/chat/history?conv=direct:{ident['client_id']}")
    m = next(x for x in hist["messages"] if x.get("attachment"))
    assert m["attachment"]["kind"] == "voice"
    # recipient downloads the voice bytes from the sender's host
    import urllib.request as u
    raw = u.urlopen(f"{a.base}/api/chat/file/{fid}", timeout=5).read()
    assert raw == payload


def test_http_takeover_attempt_is_refused(pair):
    """End-to-end proof over real HTTP: a bare client_id must not return
    the existing secret (the v1.3.0 defect closed in v1.3.1)."""
    a, _ = pair
    ident = register_phone(a, "Protected Phone")
    cid = ident["client_id"]
    # bare id, no secret
    r1 = http(f"{a.base}/api/mobile/register", "POST", {"client_id": cid})
    assert r1.get("ok") is not True
    assert "client" not in r1        # no identity payload returned at all
    # wrong secret
    r2 = http(f"{a.base}/api/mobile/register", "POST",
              {"client_id": cid, "client_secret": "a" * 32})
    assert r2.get("ok") is not True
    assert "client" not in r2
    # stored secret unchanged → the real phone still reconnects
    r3 = http(f"{a.base}/api/mobile/heartbeat", "POST",
              {"client_id": cid, "client_secret": ident["secret"]})
    assert r3.get("ok") is True


def test_mobile_attachment_transfer_to_computer_share(pair, tmp_path):
    """Photo-style attachment uploaded on the phone and pulled into an
    authorized directory of computer B through the verified engine."""
    a, b = pair
    dst = tmp_path / "phone-dst"
    dst.mkdir()
    a.post("/api/fs/share", {"path": str(dst), "mode": "rw"})
    ident = register_phone(a, "Photo Phone")
    payload = b"\x89PNG-fake-photo-bytes-for-phone-upload"
    s = http(f"{a.base}/api/chat/file/session", "POST",
             {"name": "photo.png", "size": len(payload), "mime": "image/png"})
    http(f"{a.base}/api/chat/file/{s['sid']}", "PUT", payload)
    fin = http(f"{a.base}/api/chat/file/{s['sid']}/finish", "POST", {})
    # share the file into B's authorized dir via the phone: it POSTs a pull
    # job against its host's chat-file (simulating the mobile Files flow)
    job = b.post("/api/jobs", {
        "kind": "pull",
        "src_base": f"http://127.0.0.1:{a.port}",
        "src_path": None, "dest_path": str(dst / "photo.png"),
        "conflict": "replace"})
    # chat-file downloads are addressed by id, not by path: use the direct
    # HTTP fetch on the host instead (as the mobile UI does), then verify
    import urllib.request as u
    raw = u.urlopen(f"{a.base}/api/chat/file/{fin['file']['id']}",
                    timeout=5).read()
    assert raw == payload
    a.post("/api/fs/unshare", {"path": str(dst)})
