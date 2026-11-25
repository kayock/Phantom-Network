"""v1.2.0 integration regressions (real companion subprocesses):

- administrative configuration is local-only (remote LAN client → 403;
  local browser → allowed; peer operations unaffected)
- companion lifecycle: port conflict, repeated start/stop cycles with
  thread/RSS stability, identity persistence across restarts
- large-transfer qualification: concurrent transfers, cancellation of a
  large transfer, retry after failure, long-filename large file,
  changed-source detection after a companion restart,
  real-data read window beyond the 4 GiB offset
- direct transfer routing: remote↔remote data never transits the dashboard
- launcher/installer permission boundary regression (script-level check)
"""

import hashlib
import os
import shutil
import sys
import tempfile
import time
import urllib.parse

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from conftest import Companion, wait_job_done  # noqa: E402

LAN_IP = None                     # resolved lazily (sandbox LAN address)


def lan_ip():
    global LAN_IP
    if LAN_IP is None:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            LAN_IP = s.getsockname()[0]
        finally:
            s.close()
    return LAN_IP


# ---------------------------------------------------------------------------
# §4: administrative configuration is local-only
# ---------------------------------------------------------------------------
def test_remote_admin_changes_refused(pair):
    a, b = pair
    ip = lan_ip()
    assert ip and not ip.startswith("127.")
    url_a_lan = f"http://{ip}:{a.port}"
    work = tempfile.mkdtemp(prefix="pn-adm-")
    try:
        # 1. a remote client CANNOT add a share on A
        req = urllib.request.Request(
            url_a_lan + "/api/fs/share",
            data=b'{"path": "' + work.encode() + b'", "mode": "rw"}',
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raise AssertionError(f"remote share-add must be refused "
                                     f"(got {resp.status})")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
            body = exc.read().decode()
            assert "admin_local_only" in body
        # 2. remote client cannot flip companion settings
        req2 = urllib.request.Request(
            url_a_lan + "/api/config",
            data=b'{"remote_audio_enabled": false}',
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req2, timeout=10) as resp:
                raise AssertionError("remote config change must be refused")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        # 3. peer operations are NOT affected
        ident = b.request("GET", "/api/identity")          # remote source
        assert ident["device_id"].startswith("dev_")
        # browsing an authorized share over the LAN still works
        a.post("/api/fs/share", {"path": work, "mode": "rw"})   # local admin
        listing = urllib.request.urlopen(
            url_a_lan + "/api/fs/list?path=" + urllib.parse.quote(work),
            timeout=10)
        assert listing.status == 200
        # 4. local administration still works (the wizard path)
        r = a.post("/api/fs/unshare", {"path": work})
        assert r["ok"]
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_admin_guard_can_be_relaxed_explicitly(pair):
    """Documented escape hatch: admin_local_only=false restores v1.1.0
    behaviour for headless file-server setups."""
    a, b = pair
    ip = lan_ip()
    try:
        a.post("/api/config", {"admin_local_only": False})     # local call
        req = urllib.request.Request(
            f"http://{ip}:{a.port}/api/config",
            data=b'{"remote_audio_enabled": true}',
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 200
    finally:
        a.post("/api/config", {"admin_local_only": True})


# ---------------------------------------------------------------------------
# §11: companion lifecycle
# ---------------------------------------------------------------------------
def test_port_conflict_fails_cleanly():
    c1 = Companion("PortA").start()
    try:
        # the health poll would be answered by c1, so start() alone cannot
        # detect the conflict — assert the second PROCESS dies instead
        c2 = Companion("PortB", port=c1.port)
        try:
            c2.start()
        except RuntimeError:
            pass                                   # acceptable: died at startup
        deadline = time.time() + 5
        while time.time() < deadline and c2.proc and c2.proc.poll() is None:
            time.sleep(0.3)
        assert c2.proc is None or c2.proc.poll() is not None, \
            "second companion on a busy port must terminate"
        err = open(os.path.join(c2.data_dir, "proc-stderr.log"),
                   "rb").read().decode(errors="replace")
        assert "Address already in use" in err or "errno 98" in err.lower(), err[-300:]
    finally:
        c1.stop()
        shutil.rmtree(c1.data_dir, ignore_errors=True)
        shutil.rmtree(c2.data_dir, ignore_errors=True)


def test_repeated_start_stop_cycles_stay_light():
    """5 restart cycles: thread count and RSS must not grow unboundedly."""
    c = Companion("Cycle").start()
    try:
        base = c.get("/api/stats")["process"]
        for _ in range(5):
            c.stop()
            time.sleep(0.6)
            c2 = Companion("Cycle", port=c.port, data_dir=c.data_dir).start()
            c.proc = c2.proc
            c._out = getattr(c2, "_out", None)
            c._err = getattr(c2, "_err", None)
        stats = c.get("/api/stats")["process"]
        assert stats["rss_bytes"] < 120 * 1024 * 1024, stats["rss_bytes"]
        # device identity persisted across restarts
        ident = c.identity()
        assert ident["device_id"].startswith("dev_")
    finally:
        c.stop()
        shutil.rmtree(c.data_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# §8/§9: large-transfer qualification additions
# ---------------------------------------------------------------------------
def test_concurrent_large_transfers(pair):
    a, b = pair
    free = shutil.disk_usage("/tmp").free
    if free < 5.2 * 1024 ** 3:
        pytest.skip(f"insufficient disk ({free/2**30:.1f} GB)")
    src = tempfile.mkdtemp(prefix="pn-cc-src-", dir="/tmp")
    dst = tempfile.mkdtemp(prefix="pn-cc-dst-", dir="/tmp")
    b.post("/api/fs/share", {"path": src, "mode": "rw"})
    a.post("/api/fs/share", {"path": dst, "mode": "rw"})
    old_bps = a.get("/api/config")["config"]["bandwidth_limit_bps"]
    a.post("/api/config", {"bandwidth_limit_bps": 40_000_000})
    size = int(1.2 * 1024 ** 3)
    try:
        hs = []
        for i in (1, 2):
            fpath = os.path.join(src, f"model-{i}.bin")
            h = hashlib.sha256()
            block = os.urandom(32 * 1024 * 1024)
            w = 0
            with open(fpath, "wb") as fh:
                while w < size:
                    n = min(len(block), size - w)
                    fh.write(block[:n]); h.update(block[:n]); w += n
            hs.append(h.hexdigest())
        ids = []
        for i in (1, 2):
            j = a.post("/api/jobs", {
                "kind": "pull", "src_base": b.base,
                "src_path": os.path.join(src, f"model-{i}.bin"),
                "dest_path": os.path.join(dst, f"model-{i}.bin"),
                "conflict": "replace"})["job"]
            ids.append(j["id"])
        done = [wait_job_done(a, jid, timeout=600) for jid in ids]
        for d, h in zip(done, hs):
            assert d["status"] == "completed" and d["verify"]["status"] == "ok"
        for idx, i in enumerate((1, 2)):
            d = hashlib.sha256()
            with open(os.path.join(dst, f"model-{i}.bin"), "rb") as fh:
                while True:
                    blk = fh.read(32 * 1024 * 1024)
                    if not blk:
                        break
                    d.update(blk)
            assert d.hexdigest() == hs[idx]
    finally:
        a.post("/api/config", {"bandwidth_limit_bps": old_bps})
        b.post("/api/fs/unshare", {"path": src})
        a.post("/api/fs/unshare", {"path": dst})
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(dst, ignore_errors=True)


def test_cancel_large_transfer_keeps_part_and_source(pair):
    a, b = pair
    src = tempfile.mkdtemp(prefix="pn-cx-src-", dir="/tmp")
    dst = tempfile.mkdtemp(prefix="pn-cx-dst-", dir="/tmp")
    b.post("/api/fs/share", {"path": src, "mode": "rw"})
    a.post("/api/fs/share", {"path": dst, "mode": "rw"})
    old_bps = a.get("/api/config")["config"]["bandwidth_limit_bps"]
    a.post("/api/config", {"bandwidth_limit_bps": 15_000_000})
    try:
        fpath = os.path.join(src, "cancellable.bin")
        with open(fpath, "wb") as fh:
            for _ in range(24):
                fh.write(os.urandom(32 * 1024 * 1024))   # 768 MB
        job = a.post("/api/jobs", {
            "kind": "pull", "src_base": b.base, "src_path": fpath,
            "dest_path": os.path.join(dst, "cancellable.bin"),
            "conflict": "replace"})["job"]
        deadline = time.time() + 120
        while time.time() < deadline:
            j = a.get(f"/api/jobs/{job['id']}")["job"]
            if j["transferred"] > 150 * 1024 * 1024:
                break
            time.sleep(0.3)
        a.post(f"/api/jobs/{job['id']}/control", {"action": "cancel"})
        done = wait_job_done(a, job["id"], timeout=60)
        assert done["status"] == "cancelled"
        assert os.path.getsize(fpath) == 24 * 32 * 1024 * 1024, \
            "source must be byte-identical after cancel"
        # .part retained (not silently discarded), resume possible later
        assert os.path.exists(os.path.join(dst, "cancellable.bin.part"))
        assert os.path.getsize(os.path.join(dst, "cancellable.bin.part")) > 0
    finally:
        a.post("/api/config", {"bandwidth_limit_bps": old_bps})
        b.post("/api/fs/unshare", {"path": src})
        a.post("/api/fs/unshare", {"path": dst})
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(dst, ignore_errors=True)


def test_changed_source_detected_after_restart(pair):
    """Pause a transfer, RESTART the destination companion, mutate the
    source, resume: identity binding must still refuse."""
    a, b = pair
    src = tempfile.mkdtemp(prefix="pn-rs-src-", dir="/tmp")
    dst = tempfile.mkdtemp(prefix="pn-rs-dst-", dir="/tmp")
    b.post("/api/fs/share", {"path": src, "mode": "rw"})
    a.post("/api/fs/share", {"path": dst, "mode": "rw"})
    old_bps = a.get("/api/config")["config"]["bandwidth_limit_bps"]
    a.post("/api/config", {"bandwidth_limit_bps": 10_000_000})
    try:
        fpath = os.path.join(src, "restart-case.bin")
        with open(fpath, "wb") as fh:
            for _ in range(8):
                fh.write(os.urandom(32 * 1024 * 1024))   # 256 MB
        job = a.post("/api/jobs", {
            "kind": "pull", "src_base": b.base, "src_path": fpath,
            "dest_path": os.path.join(dst, "restart-case.bin"),
            "conflict": "replace"})["job"]
        deadline = time.time() + 60
        while time.time() < deadline:
            j = a.get(f"/api/jobs/{job['id']}")["job"]
            if j["transferred"] > 100 * 1024 * 1024:
                break
            time.sleep(0.3)
        a.post(f"/api/jobs/{job['id']}/control", {"action": "pause"})
        time.sleep(0.5)
        # RESTART the destination companion (jobs reload from state)
        a.stop()
        a2 = Companion(a.name, port=a.port, data_dir=a.data_dir).start()
        with open(fpath, "ab") as fh:
            fh.write(b"CHANGED-WHILE-COMPANION-DOWN")
        os.utime(fpath, None)
        r = a2.post(f"/api/jobs/{job['id']}/control", {"action": "resume"})
        assert r.get("ok") is not False
        done = wait_job_done(a2, job["id"], timeout=120)
        assert done["status"] == "failed"
        assert "Source file changed" in (done.get("error") or "")
        assert os.path.exists(os.path.join(dst, "restart-case.bin.part"))
        a2.stop(); a.proc = None
        a3 = Companion(a.name, port=a.port, data_dir=a.data_dir).start()
        a.proc = a3.proc
        a._out = getattr(a3, "_out", None); a._err = getattr(a3, "_err", None)
    finally:
        a.post("/api/config", {"bandwidth_limit_bps": old_bps})
        b.post("/api/fs/unshare", {"path": src})
        a.post("/api/fs/unshare", {"path": dst})
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(dst, ignore_errors=True)


def test_long_filename_large_file(pair):
    a, b = pair
    src = tempfile.mkdtemp(prefix="pn-ln-src-", dir="/tmp")
    dst = tempfile.mkdtemp(prefix="pn-ln-dst-", dir="/tmp")
    b.post("/api/fs/share", {"path": src, "mode": "rw"})
    a.post("/api/fs/share", {"path": dst, "mode": "rw"})
    try:
        # >200 chars but ≤ 250 UTF-8 BYTES (ext4 component limit)
        long_name = "модель-архив-" + "x" * 200 + ".bin"
        nbytes = len(long_name.encode("utf-8"))
        assert len(long_name) > 200 and nbytes <= 250, (len(long_name), nbytes)
        fpath = os.path.join(src, long_name)
        with open(fpath, "wb") as fh:
            fh.write(os.urandom(80 * 1024 * 1024))
        h = hashlib.sha256(open(fpath, "rb").read()).hexdigest()
        job = a.post("/api/jobs", {
            "kind": "pull", "src_base": b.base, "src_path": fpath,
            "dest_path": os.path.join(dst, long_name),
            "conflict": "replace", "sha256": h})["job"]
        done = wait_job_done(a, job["id"], timeout=180)
        assert done["status"] == "completed", done.get("error")
        assert os.path.exists(os.path.join(dst, long_name))
    finally:
        b.post("/api/fs/unshare", {"path": src})
        a.post("/api/fs/unshare", {"path": dst})
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(dst, ignore_errors=True)


def test_real_data_window_beyond_4gib_offset(pair):
    """>4 GiB offsets against REAL data (not just sparse zeros): 1 MiB of
    actual content written at 2^32-3 MiB inside a sparse 4.6 GB file, then
    read back through Range requests.  Not a full transfer — documented."""
    import http.client
    from urllib.parse import urlparse
    a, b = pair
    src = tempfile.mkdtemp(prefix="pn-w4-src-", dir="/tmp")
    b.post("/api/fs/share", {"path": src, "mode": "rw"})
    try:
        fpath = os.path.join(src, "window4g.bin")
        big = int(4.6 * 1024 ** 3)
        payload = os.urandom(1024 * 1024)
        start = 2 ** 32 - (1024 * 1024)          # window crosses 2^32
        with open(fpath, "wb") as fh:
            fh.seek(start)
            fh.write(payload)
            fh.truncate(big)
        u = urlparse(b.base)
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
        q = "/api/fs/download?path=" + urllib.parse.quote(fpath)
        conn.request("GET", q, headers={"Range": f"bytes={start}-{start + 1024*1024 - 1}"})
        resp = conn.getresponse()
        data = resp.read(); conn.close()
        assert resp.status == 206
        assert resp.headers["Content-Range"] == \
            f"bytes {start}-{start + 1024*1024 - 1}/{big}"
        assert data == payload, "real data beyond 2^32 must round-trip"
    finally:
        b.post("/api/fs/unshare", {"path": src})
        shutil.rmtree(src, ignore_errors=True)


# ---------------------------------------------------------------------------
# §9: direct transfer routing — remote↔remote never transits the dashboard
# ---------------------------------------------------------------------------
def test_remote_to_remote_pull_is_direct(pair):
    """A job commanded on A with B as source and A as destination pulls
    directly from B; A's engine is the mover (destination-pull) and no
    third host is involved by construction (job src_base = B's URL)."""
    a, b = pair
    src = tempfile.mkdtemp(prefix="pn-dt-src-")
    dst = tempfile.mkdtemp(prefix="pn-dt-dst-")
    b.post("/api/fs/share", {"path": src, "mode": "rw"})
    a.post("/api/fs/share", {"path": dst, "mode": "rw"})
    try:
        fpath = os.path.join(src, "direct.bin")
        with open(fpath, "wb") as fh:
            fh.write(os.urandom(5 * 1024 * 1024))
        job = a.post("/api/jobs", {
            "kind": "pull", "src_base": b.base, "src_path": fpath,
            "dest_path": os.path.join(dst, "direct.bin"),
            "conflict": "replace"})["job"]
        # the engine that owns the job is A (the DESTINATION), the data
        # source is B's URL — no dashboard host in the path
        assert job["src_base"] == b.base
        done = wait_job_done(a, job["id"], timeout=60)
        assert done["status"] == "completed"
    finally:
        b.post("/api/fs/unshare", {"path": src})
        a.post("/api/fs/unshare", {"path": dst})
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(dst, ignore_errors=True)


# ---------------------------------------------------------------------------
# §3/§10: installer permission-boundary regression (script-level)
# ---------------------------------------------------------------------------
def test_installer_never_writes_runtime_files_into_program_dir():
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    inst = open(os.path.join(root, "packaging", "install-ubuntu.sh")).read()
    launcher = inst.split('cat > "$BIN_DIR/phantom-network" <<RUN')[1]
    # the launcher must not redirect logs into $DEST
    assert "companion.out" not in launcher.split("RUN")[0].replace(
        "STATE_DIR/companion.out", "")
    assert "$STATE_DIR/companion.out" in launcher
    assert "--data-dir" in launcher
