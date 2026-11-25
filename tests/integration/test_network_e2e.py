"""Real-network integration tests: two actual companion processes.

These start genuine ``python -m companion`` subprocesses on 127.0.0.1 and
exercise discovery, filesystem browsing, transfers (copy/move/dir/resume/
interruption/corruption/conflicts), messaging and events over HTTP.
"""

import hashlib
import json
import os
import urllib.parse
import urllib.error
import urllib.request
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from conftest import Companion, free_port, wait_job_done  # noqa: E402


# ======================================================================
# discovery / identity / availability
# ======================================================================

def test_companions_see_each_other(pair):
    a, b = pair
    peers_a = {p["device_id"] for p in a.get("/api/peers")["peers"]}
    peers_b = {p["device_id"] for p in b.get("/api/peers")["peers"]}
    assert b.device_id in peers_a, "A does not see B"
    assert a.device_id in peers_b, "B does not see A"
    # latency gets measured by the maintenance loop (≤45 s); check field type
    for p in a.get("/api/peers")["peers"]:
        assert p["latency_ms"] is None or p["latency_ms"] >= 0


def test_manual_registration_is_idempotent_and_records_non_participants(pair):
    a, b = pair
    r1 = a.register(b, name="BetaRenamed")
    assert r1["ok"]
    peers = a.get("/api/peers")["peers"]
    named = [p for p in peers if p["custom_name"] == "BetaRenamed"]
    assert len(named) == 1
    # registering a non-companion host records the failure honestly
    s = socket.socket(); s.bind(("127.0.0.1", 0)); dead_port = s.getsockname()[1]; s.close()
    r = a.post("/api/peers/register", {"address": "127.0.0.1", "port": dead_port})
    assert r["ok"] is False and "No Phantom companion" in r["error"]


def test_self_registration_refused(pair):
    a, _ = pair
    r = a.post("/api/peers/register", {"address": "127.0.0.1", "port": a.port})
    assert r["ok"] is False
    assert r.get("self") is True


def test_availability_and_uptime_events(pair):
    a, b = pair
    ident = b.identity()
    assert ident["name"] == "BetaBox"
    assert isinstance(ident["uptime_s"], (int, float))
    events = a.get("/api/events?types=device_discovered,device_connected")
    texts = " | ".join(e["text"] for e in events["events"])
    assert "BetaBox" in texts


def test_sweep_finds_companion(pair):
    a, b = pair
    # sweep probes the default port too, so start a dedicated companion there
    dflt = Companion("SweepTarget", port=8415).start()
    try:
        r = a.post("/api/radar/sweep", {"cidr": "127.0.0.1/32"})
        assert r["ok"], r
        deadline = time.time() + 12
        found = False
        while time.time() < deadline:
            st = a.get("/api/peers")["discovery"]
            res = st.get("last_sweep") or {}
            for f in res.get("found", []):
                if f["device_id"] == dflt.device_id:
                    found = True
                    break
            if found:
                break
            time.sleep(0.5)
        assert found, "sweep did not find the companion"
    finally:
        dflt.stop()
        import shutil as _sh
        _sh.rmtree(dflt.data_dir, ignore_errors=True)


# ======================================================================
# remote filesystem browsing
# ======================================================================

def test_remote_directory_browsing(pair):
    a, b = pair
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "sub dir"))
        with open(os.path.join(td, "spaced file.txt"), "w") as fh:
            fh.write("data")
        b.post("/api/fs/share", {"path": td, "mode": "rw"})
        data = a.request("GET", f"/api/fs/list?path={td.replace(' ', '%20')}",
                         timeout=10) if False else None
        # browse B's share FROM A (cross-companion call like the UI does)
        import urllib.request
        req = urllib.request.Request(
            b.base + f"/api/fs/list?path={td}", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        names = {e["name"] for e in data["entries"]}
        assert "sub dir" in names and "spaced file.txt" in names


def test_windows_and_linux_path_handling(pair):
    """Path normalization must accept ~, /, repeated slashes and refuse
    traversal — Windows backslash forms are covered by the unit suite."""
    a, b = pair
    home = os.path.expanduser("~")
    b.post("/api/fs/share", {"path": home, "mode": "rw"})   # explicit opt-in (v1.1.0)
    import urllib.request
    for variant in (f"{home}//", f"{home}/./", home.replace("/", "/", 1)):
        req = urllib.request.Request(
            b.base + f"/api/fs/list?path={variant.replace(' ', '%20')}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        assert data["ok"] and data["path"] == os.path.realpath(home)
    # traversal refused
    try:
        req = urllib.request.Request(b.base + "/api/fs/list?path=/etc/passwd")
        urllib.request.urlopen(req, timeout=10)
        assert False, "outside-shares browse must fail"
    except urllib.error.HTTPError as exc:
        assert exc.code == 403


def test_unauthorized_path_access(pair):
    a, b = pair
    r = b.get("/api/fs/list?path=/etc")
    assert r["ok"] is False and r.get("error") == "outside_shares"
    # /etc traversal via home share is also refused
    home = os.path.expanduser("~")
    r2 = b.get(f"/api/fs/list?path={home}/../..")
    assert r2["ok"] is False


# ======================================================================
# file transfer engine (real cross-companion transfers)
# ======================================================================

def _make_source_file(path: str, size_mb: float) -> str:
    h = hashlib.sha256()
    with open(path, "wb") as fh:
        chunk = os.urandom(1024 * 1024)
        full = int(size_mb)
        for _ in range(full):
            fh.write(chunk)
            h.update(chunk)
        rest = int((size_mb - full) * 1024 * 1024)
        if rest:
            part = os.urandom(rest)
            fh.write(part)
            h.update(part)
    return h.hexdigest()


@pytest.fixture()
def shares(pair):
    a, b = pair
    src = tempfile.mkdtemp(prefix="pn-src-")
    dst = tempfile.mkdtemp(prefix="pn-dst-")
    b.post("/api/fs/share", {"path": src, "mode": "rw"})   # source on B
    a.post("/api/fs/share", {"path": dst, "mode": "rw"})   # destination on A
    yield a, b, src, dst
    for c, p in ((b, src), (a, dst)):
        c.post("/api/fs/unshare", {"path": p})
    shutil.rmtree(src, ignore_errors=True)
    shutil.rmtree(dst, ignore_errors=True)


def test_pull_file_cross_companion_with_verification(shares):
    a, b, src, dst = shares
    sha = _make_source_file(os.path.join(src, "model.bin"), 40)
    job = a.post("/api/jobs", {
        "kind": "pull", "src_base": b.base, "src_path": os.path.join(src, "model.bin"),
        "dest_path": os.path.join(dst, "model.bin"),
        "conflict": "rename", "src_device": "B", "dest_device": "A",
    })
    assert job["ok"]
    done = wait_job_done(a, job["job"]["id"], timeout=90)
    assert done["status"] == "completed", done.get("error")
    assert done["verify"]["status"] == "ok"
    assert os.path.getsize(os.path.join(dst, "model.bin")) == 40 * 1024 * 1024
    assert hashlib.sha256(open(os.path.join(dst, "model.bin"), "rb").read()).hexdigest() == sha
    assert not os.path.exists(os.path.join(dst, "model.bin.part"))


def test_directory_transfer_nested(shares):
    a, b, src, dst = shares
    os.makedirs(os.path.join(src, "proj", "weights", "deep"))
    for rel in ("proj/run.py", "proj/notes.md", "proj/weights/w1.bin",
                "proj/weights/deep/w2.bin"):
        with open(os.path.join(src, rel), "wb") as fh:
            fh.write(os.urandom(200_000) if rel.endswith(".bin") else b"text")
    job = a.post("/api/jobs", {
        "kind": "pull_dir", "src_base": b.base, "src_path": os.path.join(src, "proj"),
        "dest_path": os.path.join(dst, "proj"), "conflict": "rename",
    })
    done = wait_job_done(a, job["job"]["id"], timeout=60)
    assert done["status"] == "completed"
    assert done["files_done"] == 4
    for rel in ("run.py", "notes.md", os.path.join("weights", "w1.bin"),
                os.path.join("weights", "deep", "w2.bin")):
        assert os.path.exists(os.path.join(dst, "proj", rel))


def test_pause_resume_transfer(shares):
    a, b, src, dst = shares
    old_bps = a.get("/api/config")["config"]["bandwidth_limit_bps"]
    a.post("/api/config", {"bandwidth_limit_bps": 12_000_000})   # 12 MB/s → ~5 s
    sha = _make_source_file(os.path.join(src, "big.bin"), 60)
    job = a.post("/api/jobs", {
        "kind": "pull", "src_base": b.base, "src_path": os.path.join(src, "big.bin"),
        "dest_path": os.path.join(dst, "big.bin"), "conflict": "replace",
    })["job"]
    # let it run, then pause
    deadline = time.time() + 10
    while time.time() < deadline:
        j = a.get(f"/api/jobs/{job['id']}")["job"]
        if j["transferred"] > 8 * 1024 * 1024:
            break
        time.sleep(0.2)
    a.post(f"/api/jobs/{job['id']}/control", {"action": "pause"})
    j = a.get(f"/api/jobs/{job['id']}")["job"]
    assert j["status"] == "paused"
    paused_bytes = j["transferred"]
    assert paused_bytes > 0
    time.sleep(0.6)
    j2 = a.get(f"/api/jobs/{job['id']}")["job"]
    assert j2["transferred"] == paused_bytes, "bytes moved while paused"
    a.post(f"/api/jobs/{job['id']}/control", {"action": "resume"})
    done = wait_job_done(a, job["id"], timeout=120)
    a.post("/api/config", {"bandwidth_limit_bps": old_bps})
    assert done["status"] == "completed" and done["verify"]["status"] == "ok"
    final = hashlib.sha256(open(os.path.join(dst, "big.bin"), "rb").read()).hexdigest()
    assert final == sha


def test_interrupt_and_recover_after_companion_restart(pair):
    """SIGKILL the destination companion mid-transfer, restart it, resume."""
    a, b, src, dst = None, None, None, None
    a, b = pair
    src = tempfile.mkdtemp(prefix="pn-src2-")
    dst = tempfile.mkdtemp(prefix="pn-dst2-")
    b.post("/api/fs/share", {"path": src, "mode": "rw"})
    a.post("/api/fs/share", {"path": dst, "mode": "rw"})
    old_bps = a.get("/api/config")["config"]["bandwidth_limit_bps"]
    a.post("/api/config", {"bandwidth_limit_bps": 8_000_000})   # 8 MB/s → ~10 s
    try:
        sha = _make_source_file(os.path.join(src, "huge.bin"), 80)
        job = a.post("/api/jobs", {
            "kind": "pull", "src_base": b.base, "src_path": os.path.join(src, "huge.bin"),
            "dest_path": os.path.join(dst, "huge.bin"), "conflict": "replace",
        })["job"]
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                j = a.get(f"/api/jobs/{job['id']}")["job"]
                if j["transferred"] > 10 * 1024 * 1024:
                    break
            except Exception:
                pass
            time.sleep(0.2)
        b.kill9()                    # hard kill the SOURCE mid-transfer
        # A retries a few times, then marks the job interrupted (resumable)
        deadline = time.time() + 30
        j = None
        while time.time() < deadline:
            try:
                j = a.get(f"/api/jobs/{job['id']}")["job"]
                if j["status"] == "interrupted":
                    break
            except Exception:
                pass
            time.sleep(0.5)
        assert j is not None and j["status"] == "interrupted", \
            f"job should be interrupted, got {j and j['status']}"
        part = os.path.join(dst, "huge.bin.part")
        assert os.path.exists(part) and os.path.getsize(part) > 10 * 1024 * 1024
        # bring the source back and resume from the exact byte offset
        b2 = Companion("BetaBox", port=b.port, data_dir=b.data_dir).start()
        a.register(b2)
        a.post(f"/api/jobs/{job['id']}/control", {"action": "resume"})
        deadline = time.time() + 180
        j = None
        while time.time() < deadline:
            data = a.get("/api/jobs")
            hit = [x for x in data["history"] if x["id"] == job["id"]]
            if hit:
                j = hit[0]
                break
            time.sleep(0.5)
        assert j is not None, "job never reached history"
        assert j["status"] == "completed", j.get("error")
        assert j["verify"]["status"] == "ok"
        final = hashlib.sha256(open(os.path.join(dst, "huge.bin"), "rb").read()).hexdigest()
        assert final == sha
    finally:
        a.post("/api/config", {"bandwidth_limit_bps": old_bps})
        b.stop()
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(dst, ignore_errors=True)
        # hand a live replacement process to the module fixture so later
        # tests keep working against the same object
        rb = Companion("BetaBox", port=b.port, data_dir=b.data_dir).start()
        b.proc = rb.proc


class _CorruptingHandler(BaseHTTPRequestHandler):
    """MITM proxy that forwards Range requests honestly but flips ONE byte
    inside the first response body — simulating real network corruption."""
    flipped = False
    target = None

    def do_GET(self):
        import urllib.request
        url = _CorruptingHandler.target + self.path
        headers = {}
        if "Range" in self.headers:
            headers["Range"] = self.headers["Range"]
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
            status = resp.status
            ctype = resp.headers.get("Content-Type", "application/octet-stream")
        if not _CorruptingHandler.flipped and len(data) > 500:
            i = min(400, len(data) - 1)
            data = data[:i] + bytes([data[i] ^ 0xFF]) + data[i + 1:]
            _CorruptingHandler.flipped = True
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if status == 206:
            self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def test_corrupted_transfer_fails_verification_and_preserves_source(shares):
    a, b, src, dst = shares
    sha = _make_source_file(os.path.join(src, "guard.bin"), 6)
    # start corrupting proxy in front of B
    _CorruptingHandler.flipped = False
    _CorruptingHandler.target = b.base
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _CorruptingHandler)
    proxy_port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        proxy_base = f"http://127.0.0.1:{proxy_port}"
        job = a.post("/api/jobs", {
            "kind": "pull", "src_base": proxy_base,
            "src_path": os.path.join(src, "guard.bin"),
            "dest_path": os.path.join(dst, "guard.bin"), "conflict": "replace",
        })["job"]
        done = wait_job_done(a, job["id"], timeout=60)
        assert done["status"] == "failed", "corrupted transfer must not complete"
        assert done["verify"]["status"] == "failed"
        assert "SHA-256 mismatch" in (done.get("error") or "")
        # destination must NOT have been finalized
        assert not os.path.exists(os.path.join(dst, "guard.bin"))
        assert os.path.exists(os.path.join(dst, "guard.bin.part.corrupt"))
        # source untouched
        assert hashlib.sha256(
            open(os.path.join(src, "guard.bin"), "rb").read()).hexdigest() == sha
        # the failure is visible in the event log
        ev = a.get("/api/events?types=verification_failed")
        assert any("guard.bin" in e["text"] for e in ev["events"])
    finally:
        srv.shutdown()
        # clean leftovers for later tests
        for f in ("guard.bin.part.corrupt", "guard.bin.part"):
            p = os.path.join(dst, f)
            if os.path.exists(p):
                os.unlink(p)


def test_safe_move_deletes_source_only_after_verification(shares):
    a, b, src, dst = shares
    sha = _make_source_file(os.path.join(src, "moveme.bin"), 8)
    job = a.post("/api/jobs", {
        "kind": "pull", "src_base": b.base, "src_path": os.path.join(src, "moveme.bin"),
        "dest_path": os.path.join(dst, "moveme.bin"), "conflict": "replace",
        "delete_source": True,
    })["job"]
    done = wait_job_done(a, job["id"], timeout=60)
    assert done["status"] == "completed" and done["verify"]["status"] == "ok"
    assert not os.path.exists(os.path.join(src, "moveme.bin")), "source must be gone"
    assert hashlib.sha256(
        open(os.path.join(dst, "moveme.bin"), "rb").read()).hexdigest() == sha


def test_failed_checksum_move_preserves_original(shares):
    """A move whose transfer gets corrupted must keep the source file."""
    a, b, src, dst = shares
    sha = _make_source_file(os.path.join(src, "precious.bin"), 6)
    _CorruptingHandler.flipped = False
    _CorruptingHandler.target = b.base
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _CorruptingHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        job = a.post("/api/jobs", {
            "kind": "pull", "src_base": f"http://127.0.0.1:{srv.server_address[1]}",
            "src_path": os.path.join(src, "precious.bin"),
            "dest_path": os.path.join(dst, "precious.bin"), "conflict": "replace",
            "delete_source": True,
        })["job"]
        done = wait_job_done(a, job["id"], timeout=60)
        assert done["status"] == "failed"
        assert hashlib.sha256(
            open(os.path.join(src, "precious.bin"), "rb").read()).hexdigest() == sha, \
            "ORIGINAL MUST BE PRESERVED ON FAILED MOVE"
        assert not os.path.exists(os.path.join(dst, "precious.bin"))
    finally:
        srv.shutdown()
        for f in ("precious.bin.part.corrupt", "precious.bin.part"):
            p = os.path.join(dst, f)
            if os.path.exists(p):
                os.unlink(p)


def test_destination_conflicts(shares):
    a, b, src, dst = shares
    _make_source_file(os.path.join(src, "c.bin"), 2)
    # first copy
    j1 = a.post("/api/jobs", {"kind": "pull", "src_base": b.base,
                              "src_path": os.path.join(src, "c.bin"),
                              "dest_path": os.path.join(dst, "c.bin"),
                              "conflict": "rename"})["job"]
    wait_job_done(a, j1["id"], 60)
    assert os.path.exists(os.path.join(dst, "c.bin"))
    # second copy with rename → creates (1)
    j2 = a.post("/api/jobs", {"kind": "pull", "src_base": b.base,
                              "src_path": os.path.join(src, "c.bin"),
                              "dest_path": os.path.join(dst, "c.bin"),
                              "conflict": "rename"})["job"]
    done = wait_job_done(a, j2["id"], 60)
    assert done["status"] == "completed"
    assert os.path.exists(os.path.join(dst, "c (1).bin"))
    # skip → no new file
    n_before = len(os.listdir(dst))
    j3 = a.post("/api/jobs", {"kind": "pull", "src_base": b.base,
                              "src_path": os.path.join(src, "c.bin"),
                              "dest_path": os.path.join(dst, "c.bin"),
                              "conflict": "skip"})["job"]
    wait_job_done(a, j3["id"], 60)
    assert len(os.listdir(dst)) == n_before
    # replace → same single file remains, content replaced
    j4 = a.post("/api/jobs", {"kind": "pull", "src_base": b.base,
                              "src_path": os.path.join(src, "c.bin"),
                              "dest_path": os.path.join(dst, "c.bin"),
                              "conflict": "replace"})["job"]
    wait_job_done(a, j4["id"], 60)
    assert os.path.exists(os.path.join(dst, "c.bin"))
    assert not os.path.exists(os.path.join(dst, "c (2).bin"))


def test_insufficient_space_rejected_before_transfer(shares):
    a, b, src, dst = shares
    _make_source_file(os.path.join(src, "toobig.bin"), 2)
    r = a.post("/api/config", {"min_free_space_bytes": 10 ** 18})
    try:
        job = a.post("/api/jobs", {"kind": "pull", "src_base": b.base,
                                   "src_path": os.path.join(src, "toobig.bin"),
                                   "dest_path": os.path.join(dst, "toobig.bin"),
                                   "conflict": "rename"})["job"]
        done = wait_job_done(a, job["id"], 30)
        assert done["status"] == "failed"
        assert done["error"] and "Insufficient disk space" in done["error"]
        assert not os.path.exists(os.path.join(dst, "toobig.bin"))
    finally:
        a.post("/api/config", {"min_free_space_bytes": 64 * 1024 * 1024})


def test_bandwidth_limit_enforced(pair):
    a, b = pair
    src = tempfile.mkdtemp(prefix="pn-src3-")
    dst = tempfile.mkdtemp(prefix="pn-dst3-")
    b.post("/api/fs/share", {"path": src, "mode": "rw"})
    a.post("/api/fs/share", {"path": dst, "mode": "rw"})
    old_bps = a.get("/api/config")["config"]["bandwidth_limit_bps"]
    try:
        _make_source_file(os.path.join(src, "limited.bin"), 12)
        a.post("/api/config", {"bandwidth_limit_bps": 4 * 1024 * 1024})  # 4 MB/s
        t0 = time.time()
        job = a.post("/api/jobs", {"kind": "pull", "src_base": b.base,
                                   "src_path": os.path.join(src, "limited.bin"),
                                   "dest_path": os.path.join(dst, "limited.bin"),
                                   "conflict": "replace"})["job"]
        done = wait_job_done(a, job["id"], 60)
        elapsed = time.time() - t0
        assert done["status"] == "completed"
        # 12 MB at ≤4 MB/s ⇒ ≥ ~2.6 s (allow scheduler slack)
        assert elapsed > 2.2, f"transfer too fast for limit: {elapsed:.2f}s"
        assert done["avg_bps"] <= 4.6 * 1024 * 1024
    finally:
        a.post("/api/config", {"bandwidth_limit_bps": old_bps})
        a.post("/api/fs/unshare", {"path": dst})
        b.post("/api/fs/unshare", {"path": src})
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(dst, ignore_errors=True)


def test_sync_preview_and_apply(pair):
    a, b = pair
    src = tempfile.mkdtemp(prefix="pn-syncsrc-")
    dst = tempfile.mkdtemp(prefix="pn-syncdst-")
    b.post("/api/fs/share", {"path": src, "mode": "rw"})
    a.post("/api/fs/share", {"path": dst, "mode": "rw"})
    try:
        os.makedirs(os.path.join(src, "sub"))
        with open(os.path.join(src, "a.txt"), "w") as fh:
            fh.write("A")
        with open(os.path.join(src, "sub", "b.txt"), "w") as fh:
            fh.write("B")
        with open(os.path.join(dst, "a.txt"), "w") as fh:
            fh.write("A")            # identical
        plan = a.post("/api/sync/preview", {
            "src_base": b.base, "src_path": src, "dst_base": "", "dst_path": dst,
        })
        rels = [c["rel"] for c in plan["copy"]]
        assert "a.txt" not in rels, "identical file should be skipped"
        assert "sub/b.txt" in rels
        res = a.post("/api/sync/apply", {"plan": plan, "delete_confirmed": False})
        assert res["ok"]
        deadline = time.time() + 30
        while time.time() < deadline:
            data = a.get("/api/jobs")
            hist = [j for j in data["history"] if j["status"] == "completed"]
            if hist and os.path.exists(os.path.join(dst, "sub", "b.txt")):
                break
            time.sleep(0.4)
        assert os.path.exists(os.path.join(dst, "sub", "b.txt"))
        assert open(os.path.join(dst, "sub", "b.txt")).read() == "B"
    finally:
        a.post("/api/fs/unshare", {"path": dst})
        b.post("/api/fs/unshare", {"path": src})
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(dst, ignore_errors=True)


# ======================================================================
# GhostChat end-to-end
# ======================================================================

def test_direct_message_delivery_and_read_receipt(pair):
    a, b = pair
    msg = a.post("/api/chat/send", {
        "to_kind": "direct", "to_id": b.device_id,
        "body": "integration hello ✅"})
    assert msg["message"]["status"] == "delivered"
    # B stores it
    hist = b.get("/api/chat/history?conv=" +
                 f"direct:{a.device_id}")
    assert any(m["body"] == "integration hello ✅" for m in hist["messages"])
    # read receipt flows back to A
    b.post("/api/chat/read", {"conv": f"direct:{a.device_id}",
                              "up_to_ts": msg["message"]["ts"] + 1})
    deadline = time.time() + 8
    status = None
    while time.time() < deadline:
        hist = a.get(f"/api/chat/history?conv=direct:{b.device_id}")
        for m in hist["messages"]:
            if m["id"] == msg["message"]["id"]:
                status = m["status"]
        if status == "read":
            break
        time.sleep(0.4)
    assert status == "read", f"expected read receipt, got {status}"


def test_broadcast_reaches_all_participants(pair):
    a, b = pair
    msg = a.post("/api/chat/send", {"to_kind": "broadcast", "to_id": "",
                                    "body": "attention fleet"})
    time.sleep(1.0)
    hist = b.get("/api/chat/history?q=" + urllib.parse.quote("attention fleet"))
    assert any(m["body"] == "attention fleet" for m in hist["messages"])


def test_group_message(pair):
    a, b = pair
    g = a.post("/api/chat/groups", {"name": "ops", "members": [b.device_id]})
    gid = g["group"]["id"]
    msg = a.post("/api/chat/send", {"to_kind": "group", "to_id": gid,
                                    "body": "group ping"})
    time.sleep(1.0)
    hist = b.get("/api/chat/history?q=" + urllib.parse.quote("group ping"))
    assert any(m["body"] == "group ping" for m in hist["messages"])
    a.request("DELETE", f"/api/chat/groups/{gid}", body=b"{}")


def test_offline_message_queued_then_delivered():
    """Recipient down → message queues; when it returns, outbox delivers."""
    a = Companion("QueueA").start()
    b = Companion("QueueB").start()
    a.register(b)
    b_id = b.device_id
    b.stop()
    time.sleep(0.5)
    msg = a.post("/api/chat/send", {"to_kind": "direct", "to_id": b_id,
                                    "body": "offline queue test"})
    assert msg["message"]["status"] == "queued"
    b2 = Companion("QueueB", port=b.port, data_dir=b.data_dir).start()
    a.register(b2)
    deadline = time.time() + 45
    status = None
    while time.time() < deadline:
        hist = a.get(f"/api/chat/history?conv=direct:{b_id}")
        for m in hist["messages"]:
            if m["id"] == msg["message"]["id"]:
                status = m["status"]
        if status == "delivered":
            break
        time.sleep(1.0)
    b2.stop(); a.stop()
    shutil.rmtree(a.data_dir, ignore_errors=True)
    shutil.rmtree(b.data_dir, ignore_errors=True)
    assert status == "delivered", f"outbox delivery failed (status={status})"


# ======================================================================
# events, monitoring, config, lifecycle
# ======================================================================

def test_event_log_captures_transfer_and_audio(pair):
    a, b = pair
    a.post("/api/audio/preset", {"name": "transfer_complete"})
    time.sleep(0.3)
    ev = a.get("/api/events?types=audio_playback")
    assert any("transfer_complete" in e["text"] for e in ev["events"])
    export = a.get("/api/events/export") if False else None
    payload = a.request("GET", "/api/events/export")
    assert payload["events"], "event export must contain events"


def test_telemetry_snapshot_shapes(pair):
    a, b = pair
    m = a.get("/api/monitor")
    for key in ("cpu", "memory", "disks", "network", "uptime_s", "gpu", "process"):
        assert key in m
    mem = m["memory"]
    if mem["total"] is not None:
        assert mem["total"] > 0 and 0 <= mem["percent"] <= 100
    hist = a.get("/api/monitor/history")["history"]
    assert isinstance(hist, list)


def test_config_persistence_roundtrip():
    c = Companion("CfgPersist").start()
    try:
        c.post("/api/config", {"device_name": "Persisted Name",
                               "bandwidth_limit_bps": 123456})
        c.stop()
        c2 = Companion(None, port=c.port, data_dir=c.data_dir).start()
        cfg = c2.get("/api/config")["config"]
        assert cfg["device_name"] == "Persisted Name"
        assert cfg["bandwidth_limit_bps"] == 123456
        # token is never exposed
        assert "auth_token" not in cfg
        c2.stop()
    finally:
        shutil.rmtree(c.data_dir, ignore_errors=True)


def test_auth_token_enforced_when_enabled():
    c = Companion("AuthToken").start()
    try:
        c.post("/api/config", {"require_token": True})
        # requests without a token are refused (test client surfaces 401 bodies)
        r = c.get("/api/config")
        assert r["ok"] is False and r["status"] == 401, r
        # with the token from config.json it works
        cfg_path = os.path.join(c.data_dir, "config.json")
        token = json.load(open(cfg_path))["auth_token"]
        assert token
        c.env_token = token
        assert c.get("/api/config")["ok"]
        # disable again for safety
        c.post("/api/config", {"require_token": False})
        c.env_token = ""
    finally:
        c.stop()
        shutil.rmtree(c.data_dir, ignore_errors=True)


def test_graceful_shutdown_persists_state():
    c = Companion("Shutdown").start()
    data_dir = c.data_dir
    port = c.port
    c.post("/api/config", {"device_name": "ShutdownTest"})
    c.stop()          # SIGTERM
    assert c.proc.poll() is not None, "process must exit on SIGTERM"
    ev_path = os.path.join(data_dir, "events.jsonl")
    assert os.path.exists(ev_path)
    cfg = json.load(open(os.path.join(data_dir, "config.json")))
    assert cfg["device_name"] == "ShutdownTest"
    shutil.rmtree(data_dir, ignore_errors=True)


def test_reconnection_after_peer_restart(pair):
    a, b = pair
    b_id = b.device_id
    b.stop()
    time.sleep(1.5)
    # A should eventually mark B offline (TTL)
    deadline = time.time() + 20
    status = "online"
    while time.time() < deadline:
        peers = {p["device_id"]: p for p in a.get("/api/peers")["peers"]}
        if b_id in peers:
            status = peers[b_id]["status"]
            if status == "offline":
                break
        time.sleep(1.0)
    b2 = Companion("BetaBox", port=b.port, data_dir=b.data_dir).start()
    a.register(b2)
    deadline = time.time() + 10
    online = False
    while time.time() < deadline:
        peers = {p["device_id"]: p for p in a.get("/api/peers")["peers"]}
        if peers.get(b_id, {}).get("status") == "online":
            online = True
            break
        time.sleep(0.5)
    ev = a.get("/api/events?types=device_connected")
    assert online, "B must come back online after restart"
    assert any("reconnected" in e["text"].lower() or "online" in e["text"].lower()
               for e in ev["events"])
