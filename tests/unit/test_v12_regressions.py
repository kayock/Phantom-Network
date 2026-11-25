"""v1.2.0 regression tests (unit level): verified-deletion integrity,
Windows name validation, config compatibility with v1.1.0 installs,
ENOSPC behaviour and release-hygiene helpers."""

import hashlib
import json
import os
import sys
import tempfile
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from companion.errors import FsError  # noqa: E402
from companion.events import EventBus  # noqa: E402
from companion.fsops import windows_name_problem  # noqa: E402
from companion.transfers import TransferEngine  # noqa: E402


def _engine(td, **overrides):
    """Build a TransferEngine with a shared sandbox for the temp dir."""
    from companion.config import Config as C
    from companion.fsops import PathSandbox
    cfg = C(td)
    cfg.set("shares", [{"path": td, "label": "t", "mode": "rw"}])
    for k, v in overrides.items():
        cfg.set(k, v)
    sb = PathSandbox(cfg)
    return TransferEngine(td, cfg, sb, EventBus(td), None)


def _write(path, data, mtime_ns=None):
    with open(path, "wb") as fh:
        fh.write(data)
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))


# ---------------------------------------------------------------------------
# §5: stale checksum cache must never authorize destructive deletion
# ---------------------------------------------------------------------------
def test_stale_cache_cannot_authorize_deletion():
    """Same size + restored mtime + poisoned cache: deletion must still
    refuse because the CURRENT content is re-hashed."""
    with tempfile.TemporaryDirectory() as td:
        eng = _engine(td)
        src = os.path.join(td, "valuable.bin")
        data = b"A" * 4096
        _write(src, data)
        old_hash = hashlib.sha256(data).hexdigest()
        info = eng.file_hash(src)                    # cache poisoned for src
        assert info["sha256"] == old_hash
        # attacker/user modifies content, KEEPS size, RESTORES mtime
        keep_mtime = os.stat(src).st_mtime_ns
        _write(src, b"B" * 4096, mtime_ns=keep_mtime)
        # v1.1.0 logic would have served the cached (size, mtime) hash
        with pytest.raises(FsError) as e:
            eng.verified_delete(src, old_hash,
                                expected_mtime_ns=keep_mtime,
                                expected_size=4096)
        assert e.value.code == "verify_mismatch"
        assert os.path.exists(src), "source must be preserved"


def test_replaced_same_path_same_size_mtime_refused():
    """File replaced at the same path (new inode) with matching size and
    restored mtime: deletion refused unless content matches the expectation."""
    with tempfile.TemporaryDirectory() as td:
        eng = _engine(td)
        src = os.path.join(td, "replaced.bin")
        data = b"X" * 1024
        _write(src, data)
        h = hashlib.sha256(data).hexdigest()
        keep = os.stat(src).st_mtime_ns
        os.remove(src)
        _write(src, b"Y" * 1024, mtime_ns=keep)      # new inode, same size
        with pytest.raises(FsError) as e:
            eng.verified_delete(src, h, expected_mtime_ns=keep,
                                expected_size=1024)
        assert e.value.code == "verify_mismatch"
        # identical content replacement (harmless) still deletes
        os.remove(src)
        _write(src, data, mtime_ns=keep)
        eng.verified_delete(src, h, expected_size=1024)
        assert not os.path.exists(src)


def test_deletion_refuses_symlink():
    with tempfile.TemporaryDirectory() as td:
        eng = _engine(td)
        real = os.path.join(td, "real.bin")
        _write(real, b"payload")
        link = os.path.join(td, "link.bin")
        os.symlink(real, link)
        h = hashlib.sha256(b"payload").hexdigest()
        with pytest.raises(FsError) as e:
            eng.verified_delete(link, h)
        assert e.value.code == "symlink_refused"
        assert os.path.exists(real), "link target must survive"


def test_deletion_rehash_is_not_served_from_cache(monkeypatch):
    """Prove the cache is bypassed: even a deliberately poisoned cache
    entry cannot influence the destructive verification."""
    with tempfile.TemporaryDirectory() as td:
        eng = _engine(td)
        src = os.path.join(td, "cached.bin")
        data = b"Z" * 512
        _write(src, data)
        h = hashlib.sha256(data).hexdigest()
        # poison the cache with a WRONG hash under the current identity key
        st = os.stat(src)
        eng._hash_cache[src] = ((st.st_size, st.st_mtime_ns), "0" * 64)
        eng.verified_delete(src, h, expected_size=512)   # must still succeed
        assert not os.path.exists(src)


def test_source_modified_during_verification():
    """Identity re-check right before unlink catches in-flight changes."""
    with tempfile.TemporaryDirectory() as td:
        eng = _engine(td)
        src = os.path.join(td, "racy.bin")
        data = os.urandom(4 * 1024 * 1024)
        _write(src, data)
        h = hashlib.sha256(data).hexdigest()

        import companion.util as U
        orig = U.sha256_file

        def slow_sha(path, cancel=None, chunk=None):
            time.sleep(0.25)                 # window for a concurrent edit
            return orig(path, cancel=cancel)

        import companion.transfers as T
        def mutator():
            time.sleep(0.05)
            with open(src, "ab") as fh:
                fh.write(b"APPENDED-DURING-HASH")
        t = threading.Thread(target=mutator, daemon=True)
        U.sha256_file = slow_sha
        try:
            t.start()
            with pytest.raises(FsError) as e:
                eng.verified_delete(src, h)
            assert e.value.code in ("verify_mismatch", "source_changed")
        finally:
            t.join()
            U.sha256_file = orig
        assert os.path.exists(src)


# ---------------------------------------------------------------------------
# §7: Windows destination-name validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,why", [
    ("aux.txt", "reserved"),
    ("CON", "reserved"),
    ("con.tar.gz", "reserved"),
    ("LPT4.log", "reserved"),
    ("report?.pdf", "invalid"),
    ('quote"name.mp4', "invalid"),
    ("trail.dot.", "trailing"),
    ("trail space ", "trailing"),
    ("x" * 300, "limit"),
])
def test_windows_name_problems(name, why):
    msg = windows_name_problem(name)
    assert msg and why in msg or why in (msg or ""), (name, msg)


@pytest.mark.parametrize("name", [
    "ok name.txt", "модель-50b.gguf", "data.tar.gz", "Ärger.zip",
    "dots...inside.bin", ".hidden",
])
def test_windows_name_accepts_safe_names(name):
    assert windows_name_problem(name) is None


# ---------------------------------------------------------------------------
# §10: configuration compatibility with v1.1.0 installs
# ---------------------------------------------------------------------------
def test_v110_config_upgrade_preserves_and_merges():
    from companion.config import Config
    with tempfile.TemporaryDirectory() as td:
        # a config written by v1.1.0 (no v1.2.0 keys)
        v110 = {"device_name": "Ubuntu Station", "device_id": "dev_abc123",
                "shares": [{"path": "/mnt/media", "label": "media",
                            "mode": "ro"}],
                "share_home_rw": False, "bandwidth_limit_bps": 52428800,
                "auth_token": "tok123"}
        with open(os.path.join(td, "config.json"), "w") as fh:
            json.dump(v110, fh)
        cfg = Config(td)
        # preserved values
        assert cfg.get("device_id") == "dev_abc123"
        assert cfg.get("shares") == v110["shares"]
        assert cfg.get("bandwidth_limit_bps") == 52428800
        assert cfg.get("auth_token") == "tok123"
        # new defaults merged in
        assert cfg.get("admin_local_only") is True
        assert cfg.get("max_dir_files") == 200000
        # nothing was lost on disk
        on_disk = json.load(open(os.path.join(td, "config.json")))
        assert on_disk["device_name"] == "Ubuntu Station"


# ---------------------------------------------------------------------------
# §8: disk exhaustion during a transfer (mocked ENOSPC — no tmpfs here)
# ---------------------------------------------------------------------------
def test_enospc_during_stream_fails_preserving_source_and_part():
    with tempfile.TemporaryDirectory() as td:
        eng = _engine(td, min_free_space_bytes=0)
        src = os.path.join(td, "src.bin")
        dst = os.path.join(td, "dst.bin")
        _write(src, os.urandom(256 * 1024))
        h = hashlib.sha256(open(src, "rb").read()).hexdigest()

        import companion.transfers as T
        orig = T.TransferEngine._stream_local
        def enospc(self, real_src, fh, offset, size, job, cancel, pause):
            fh.write(b"\0" * 1024)
            raise OSError(28, "No space left on device")
        T.TransferEngine._stream_local = enospc
        try:
            job = eng.create_job({
                "kind": "pull", "src_base": "self", "src_path": src,
                "dest_path": dst, "conflict": "replace", "sha256": h,
            })
            deadline = time.time() + 20
            hit = None
            while time.time() < deadline:
                for hj in eng.list_jobs()["history"] + \
                        eng.list_jobs()["jobs"]:
                    if hj["id"] == job["id"] and hj["status"] in (
                            "failed", "interrupted"):
                        hit = hj
                        break
                if hit:
                    break
                time.sleep(0.2)
            assert hit, "job never reached a terminal state"
        finally:
            T.TransferEngine._stream_local = orig
        # OSError is classified recoverable → "interrupted" (free space,
        # then retry) — honest either way; never a completed job
        assert hit["status"] in ("failed", "interrupted")
        assert hit.get("error")
        assert os.path.exists(src), "source must survive ENOSPC"
        assert not os.path.exists(dst), "destination must not finalize"


# ---------------------------------------------------------------------------
# §15: release hygiene helpers usable from CI
# ---------------------------------------------------------------------------
def test_release_validator_flags_corrupted_script(tmp_path):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..",
                                    "packaging"))
    import importlib
    vr = importlib.import_module("validate_release")
    bad = tmp_path / "broken.ps1"
    bad.write_bytes(b'$x = "path\\with\\vt\x0bhere"\n')
    root = tmp_path
    (root / "packaging").mkdir(exist_ok=True)
    bad.rename(root / "packaging" / "broken.ps1")
    problems = vr.scan_tree(root)
    assert any("control byte 0x0b" in p for p in problems), problems
