"""v1.1.0 integration regressions (real companion subprocesses):

- safe default shares + setup status + capabilities
- paginated directory transfer (>20k files) and cap refusal
- sync comparison strategies (size / mtime / hash) incl. same-size-different-content
- source-identity resume guard
- partial-completion moves (source retained, honestly reported)
- large-file transfer > 2 GiB incl. resume across the 2^31 offset,
  bounded memory, and a >4 GiB Range boundary probe on a sparse file
"""

import hashlib
import os
import shutil
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from conftest import Companion, free_port, wait_job_done  # noqa: E402


@pytest.fixture()
def fresh():
    """A freshly-installed companion (no setup performed)."""
    c = Companion("FreshInstall").start()
    yield c
    c.stop()
    shutil.rmtree(c.data_dir, ignore_errors=True)


def test_safe_default_shares_nothing(fresh):
    r = fresh.get(f"/api/fs/list?path={os.path.expanduser('~')}")
    assert r["ok"] is False and r.get("error") == "outside_shares"
    st = fresh.get("/api/setup/status")
    assert st["ok"] and st["setup_complete"] is False
    assert st["shares"] == []
    # suggestions exist and are visible to the operator
    assert isinstance(st["suggestions"], list)
    # the wizard path works: authorize a dir → it becomes browsable
    work = tempfile.mkdtemp(prefix="pn-wizard-")
    try:
        fresh.post("/api/fs/share", {"path": work, "mode": "rw"})
        r2 = fresh.get(f"/api/fs/list?path={work}")
        assert r2["ok"] is True
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_identity_advertises_capabilities(fresh):
    ident = fresh.identity()
    assert "audio_enabled" in ident and "intercom_enabled" in ident \
        and "shares_count" in ident
    fresh.post("/api/fs/share", {"path": tempfile.mkdtemp(), "mode": "rw"})
    assert fresh.identity()["shares_count"] >= 1


def _pair_with_shares(pair):
    a, b = pair
    src = tempfile.mkdtemp(prefix="pn-g-src-")
    dst = tempfile.mkdtemp(prefix="pn-g-dst-")
    b.post("/api/fs/share", {"path": src, "mode": "rw"})
    a.post("/api/fs/share", {"path": dst, "mode": "rw"})
    return a, b, src, dst


def _cleanup_shares(a, b, src, dst):
    a.post("/api/fs/unshare", {"path": dst})
    b.post("/api/fs/unshare", {"path": src})
    shutil.rmtree(src, ignore_errors=True)
    shutil.rmtree(dst, ignore_errors=True)


def test_directory_transfer_beyond_20k_files(pair):
    """Defect G: >20 000 files transfer COMPLETELY, not truncated."""
    a, b, src, dst = _pair_with_shares(pair)
    try:
        big = os.path.join(src, "bigtree")
        os.makedirs(big)
        for i in range(21000):
            with open(os.path.join(big, f"f{i:05d}.dat"), "wb") as fh:
                fh.write(b"")                      # empty files keep it fast
        job = a.post("/api/jobs", {
            "kind": "pull_dir", "src_base": b.base, "src_path": big,
            "dest_path": os.path.join(dst, "bigtree"), "conflict": "rename",
        })["job"]
        done = wait_job_done(a, job["id"], timeout=420)
        assert done["status"] == "completed", done.get("error")
        assert done["files_done"] == 21000
        assert done["files_total"] == 21000
        n_local = sum(len(fns) for _, _, fns in os.walk(os.path.join(dst, "bigtree")))
        assert n_local == 21000
    finally:
        _cleanup_shares(a, b, src, dst)


def test_tree_cap_refusal_is_explicit(pair):
    a, b, src, dst = _pair_with_shares(pair)
    try:
        tree = os.path.join(src, "capped")
        os.makedirs(tree)
        for i in range(150):
            with open(os.path.join(tree, f"f{i}.dat"), "wb") as fh:
                fh.write(b"x")
        b.post("/api/config", {"max_dir_files": 100})
        try:
            job = a.post("/api/jobs", {
                "kind": "pull_dir", "src_base": b.base, "src_path": tree,
                "dest_path": os.path.join(dst, "capped"), "conflict": "rename",
            })["job"]
            done = wait_job_done(a, job["id"], timeout=60)
            assert done["status"] == "failed"
            assert "file limit" in (done.get("error") or "")
            assert not os.path.exists(os.path.join(dst, "capped"))
        finally:
            b.post("/api/config", {"max_dir_files": 200000})
    finally:
        _cleanup_shares(a, b, src, dst)


def test_sync_same_size_different_content(pair):
    """Defect H: equal size ≠ identical content."""
    a, b, src, dst = _pair_with_shares(pair)
    try:
        with open(os.path.join(src, "same.bin"), "wb") as fh:
            fh.write(b"A" * 4096)
        with open(os.path.join(dst, "same.bin"), "wb") as fh:
            fh.write(b"B" * 4096)                     # same size, different content
        # size mode: skips (and says so honestly)
        plan_size = a.post("/api/sync/preview", {
            "src_base": b.base, "src_path": src, "dst_base": "", "dst_path": dst,
            "compare": "size"})
        assert [c["rel"] for c in plan_size["copy"]] == []
        # hash mode: detects the difference
        plan_hash = a.post("/api/sync/preview", {
            "src_base": b.base, "src_path": src, "dst_base": "", "dst_path": dst,
            "compare": "hash"})
        assert [c["rel"] for c in plan_hash["copy"]] == ["same.bin"]
        assert plan_hash["compare"] == "hash"
        # apply → engine verifies SHA-256 → content replaced
        a.post("/api/sync/apply", {"plan": plan_hash, "delete_confirmed": False})
        deadline = time.time() + 40
        while time.time() < deadline:
            if open(os.path.join(dst, "same.bin"), "rb").read()[:1] == b"A":
                break
            time.sleep(0.5)
        assert open(os.path.join(dst, "same.bin"), "rb").read() == b"A" * 4096
    finally:
        _cleanup_shares(a, b, src, dst)


def test_sync_mtime_mode(pair):
    a, b, src, dst = _pair_with_shares(pair)
    try:
        with open(os.path.join(src, "m.bin"), "wb") as fh:
            fh.write(b"newer")
        with open(os.path.join(dst, "m.bin"), "wb") as fh:
            fh.write(b"older")
        old = time.time() - 3600
        os.utime(os.path.join(dst, "m.bin"), (old, old))
        plan = a.post("/api/sync/preview", {
            "src_base": b.base, "src_path": src, "dst_base": "", "dst_path": dst,
            "compare": "mtime"})
        assert [c["rel"] for c in plan["copy"]] == ["m.bin"]
    finally:
        _cleanup_shares(a, b, src, dst)


def test_resume_refuses_changed_source(pair):
    """Spec §7: no silent resume against a changed source; partial kept."""
    a, b = pair
    src = tempfile.mkdtemp(prefix="pn-id-src-")
    dst = tempfile.mkdtemp(prefix="pn-id-dst-")
    b.post("/api/fs/share", {"path": src, "mode": "rw"})
    a.post("/api/fs/share", {"path": dst, "mode": "rw"})
    old_bps = a.get("/api/config")["config"]["bandwidth_limit_bps"]
    a.post("/api/config", {"bandwidth_limit_bps": 20_000_000})
    try:
        with open(os.path.join(src, "model.bin"), "wb") as fh:
            fh.write(os.urandom(40 * 1024 * 1024))
        job = a.post("/api/jobs", {
            "kind": "pull", "src_base": b.base,
            "src_path": os.path.join(src, "model.bin"),
            "dest_path": os.path.join(dst, "model.bin"), "conflict": "replace",
        })["job"]
        # let ~8 MB land, then pause
        deadline = time.time() + 10
        while time.time() < deadline:
            j = a.get(f"/api/jobs/{job['id']}")["job"]
            if j["transferred"] > 8 * 1024 * 1024:
                break
            time.sleep(0.2)
        a.post(f"/api/jobs/{job['id']}/control", {"action": "pause"})
        part = os.path.join(dst, "model.bin.part")
        assert os.path.exists(part)
        # mutate the SOURCE while paused
        with open(os.path.join(src, "model.bin"), "ab") as fh:
            fh.write(b"MUTATED-DURING-PAUSE")
        os.utime(os.path.join(src, "model.bin"), None)
        a.post(f"/api/jobs/{job['id']}/control", {"action": "resume"})
        done = wait_job_done(a, job["id"], timeout=90)
        assert done["status"] == "failed", done
        assert "Source file changed" in (done.get("error") or "")
        # partial data was NOT discarded silently
        assert os.path.exists(part)
        # retry from scratch works (conflict replace discards the .part)
        a.post(f"/api/jobs/{job['id']}/control", {"action": "retry"})
        done2 = wait_job_done(a, job["id"], timeout=180)
        assert done2["status"] == "completed" and done2["verify"]["status"] == "ok"
    finally:
        a.post("/api/config", {"bandwidth_limit_bps": old_bps})
        _cleanup_shares(a, b, src, dst)


def test_move_with_undeletable_source_reports_partial(pair):
    """Spec §6/§8: verified copy + retained source = completed_partial."""
    a, b = pair
    src = tempfile.mkdtemp(prefix="pn-mv-src-")
    dst = tempfile.mkdtemp(prefix="pn-mv-dst-")
    b.post("/api/fs/share", {"path": src, "mode": "rw"})
    a.post("/api/fs/share", {"path": dst, "mode": "rw"})
    try:
        fpath = os.path.join(src, "locked.bin")
        with open(fpath, "wb") as fh:
            fh.write(os.urandom(1024 * 1024))
        os.chmod(src, 0o555)          # directory read-only → unlink fails
        try:
            job = a.post("/api/jobs", {
                "kind": "pull", "src_base": b.base, "src_path": fpath,
                "dest_path": os.path.join(dst, "locked.bin"),
                "conflict": "replace", "delete_source": True,
            })["job"]
            done = wait_job_done(a, job["id"], timeout=90)
            assert done["status"] == "completed_partial", done
            # data safe in BOTH places, honestly reported
            assert os.path.exists(fpath), "source must be preserved"
            assert os.path.exists(os.path.join(dst, "locked.bin"))
            assert "source could not be removed" in (done.get("move_error") or "")
        finally:
            os.chmod(src, 0o755)
    finally:
        _cleanup_shares(a, b, src, dst)


def test_large_file_transfer_above_2gib_with_boundary_resume(pair):
    """Real transfer > 2 GiB; resume from an offset beyond 2**31; memory bound.

    Source 2.6 GiB (random 64 MB blocks + zeros), 20 MB/s limiter so the
    pause lands past the 2^31 byte boundary.  Skipped automatically when the
    environment lacks ~6 GB of working disk.
    """
    a, b = pair
    # adaptive sizing: the full 2.6 GiB target needs ~2*2.6 GiB + headroom;
    # on smaller disks shrink toward the 2.2 GiB boundary floor or skip
    # honestly — never claim a transfer that did not happen
    free = shutil.disk_usage("/tmp").free
    size = int(min(2.6 * 1024 ** 3, (free - 1.6 * 1024 ** 3) / 2))
    if size < 2.2 * 1024 ** 3:
        pytest.skip(f"insufficient disk for the large-file test "
                    f"({free/2**30:.1f} GB free; need ~6.8 GB)")
    src = tempfile.mkdtemp(prefix="pn-lf-src-", dir="/tmp")
    dst = tempfile.mkdtemp(prefix="pn-lf-dst-", dir="/tmp")
    b.post("/api/fs/share", {"path": src, "mode": "rw"})
    a.post("/api/fs/share", {"path": dst, "mode": "rw"})
    old_bps = a.get("/api/config")["config"]["bandwidth_limit_bps"]
    a.post("/api/config", {"bandwidth_limit_bps": 20_000_000})   # 20 MB/s
    try:
        fpath = os.path.join(src, "gguf-sim.bin")
        h = hashlib.sha256()
        with open(fpath, "wb") as fh:
            block = os.urandom(64 * 1024 * 1024)
            written = 0
            while written < size:
                n = min(64 * 1024 * 1024, size - written)
                fh.write(block[:n])
                h.update(block[:n])
                written += n
        expected = h.hexdigest()
        job = a.post("/api/jobs", {
            "kind": "pull", "src_base": b.base, "src_path": fpath,
            "dest_path": os.path.join(dst, "gguf-sim.bin"), "conflict": "replace",
        })["job"]
        # pause once past 2**31 bytes (2.2 GiB) → resume offset > 2^31
        deadline = time.time() + 300
        while time.time() < deadline:
            j = a.get(f"/api/jobs/{job['id']}")["job"]
            if j["transferred"] > 2.2 * 1024 ** 3:
                break
            time.sleep(0.5)
        assert j["transferred"] > 2 ** 31, "did not reach the 2^31 boundary"
        a.post(f"/api/jobs/{job['id']}/control", {"action": "pause"})
        time.sleep(1.0)
        # memory bound while holding >2 GiB of partial state
        rss = a.get("/api/stats")["process"]["rss_bytes"]
        assert rss < 250 * 1024 * 1024, f"companion RSS {rss} — unbounded growth"
        a.post(f"/api/jobs/{job['id']}/control", {"action": "resume"})
        deadline = time.time() + 600
        j = None
        while time.time() < deadline:
            data = a.get("/api/jobs")
            hit = [x for x in data["history"] if x["id"] == job["id"]]
            if hit:
                j = hit[0]
                break
            time.sleep(1.0)
        assert j["status"] == "completed", j.get("error")
        assert j["verify"]["status"] == "ok"
        assert os.path.getsize(os.path.join(dst, "gguf-sim.bin")) == size
        os.remove(fpath)          # free the source before the digest pass
        # independent digest over the destination (reads 2.6 GiB — honest check)
        d = hashlib.sha256()
        with open(os.path.join(dst, "gguf-sim.bin"), "rb") as fh:
            while True:
                blk = fh.read(64 * 1024 * 1024)
                if not blk:
                    break
                d.update(blk)
        assert d.hexdigest() == expected
    finally:
        a.post("/api/config", {"bandwidth_limit_bps": old_bps})
        _cleanup_shares(a, b, src, dst)


def test_range_request_across_4gib_boundary(pair):
    """>4 GiB offsets: sparse file, single Range window over 2**32.

    Verifies the HTTP layer's 64-bit offset handling without requiring a
    real 4 GB copy (disk-limited environment — documented in TEST_REPORT).
    """
    a, b = pair
    src = tempfile.mkdtemp(prefix="pn-sp-", dir="/tmp")
    b.post("/api/fs/share", {"path": src, "mode": "rw"})
    try:
        fpath = os.path.join(src, "sparse.bin")
        big = int(4.5 * 1024 ** 3)
        with open(fpath, "wb") as fh:
            fh.truncate(big)                 # sparse: ~0 bytes of real disk
        start = 2 ** 32 - 500                # crosses the 4 GiB boundary
        end = start + 999
        # ask the SOURCE companion directly (the Range layer lives there);
        # http.client with explicit host/port avoids any proxy interference
        import http.client
        from urllib.parse import urlparse
        u = urlparse(b.base)
        path_q = f"/api/fs/download?path={urllib_path(fpath)}"
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
        conn.request("GET", path_q, headers={"Range": f"bytes={start}-{end}"})
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        assert resp.status == 206
        assert resp.headers["Content-Range"] == f"bytes {start}-{end}/{big}"
        assert len(payload) == 1000
        assert payload == b"\x00" * 1000     # sparse hole reads as zeros
    finally:
        b.post("/api/fs/unshare", {"path": src})
        shutil.rmtree(src, ignore_errors=True)


def urllib_path(p: str) -> str:
    import urllib.parse
    return urllib.parse.quote(p, safe="")
