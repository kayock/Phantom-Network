"""Unit tests: transfer engine logic — rate limiter, conflicts, space check."""

import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from companion.config import Config  # noqa: E402
from companion.errors import FsError  # noqa: E402
from companion.events import EventBus  # noqa: E402
from companion.fsops import PathSandbox  # noqa: E402
from companion.transfers import RateLimiter, TransferEngine  # noqa: E402


def test_rate_limiter_unlimited_is_instant():
    rl = RateLimiter()
    t0 = time.monotonic()
    rl.acquire(1024 * 1024)
    assert time.monotonic() - t0 < 0.05


def test_rate_limiter_limits_throughput():
    rl = RateLimiter()
    rl.set_bps(200_000)             # 200 KB/s
    t0 = time.monotonic()
    for _ in range(4):
        rl.acquire(100_000)         # 400 KB total ≈ 2 s at limit
    elapsed = time.monotonic() - t0
    assert 1.2 < elapsed < 4.0, f"rate limiting off: {elapsed:.2f}s"


def _mk_engine(td):
    cfg = Config(td)
    cfg.set("share_home_rw", False)
    cfg.set("shares", [{"path": td, "label": "test", "mode": "rw"}])
    sb = PathSandbox(cfg)
    events = EventBus(td)
    return TransferEngine(td, cfg, sb, events, None), cfg, sb


def test_conflict_rename_strategy():
    with tempfile.TemporaryDirectory() as td:
        eng, cfg, sb = _mk_engine(td)
        os.makedirs(os.path.join(td, "d"))
        dest = os.path.join(td, "d", "file.bin")
        with open(dest, "wb") as fh:
            fh.write(b"existing")
        job = {"conflict": "rename", "dest_path": dest}
        verdict = eng._resolve_conflict(job, dest, 10, "deadbeef")
        assert verdict == "renamed"
        assert job["dest_path"].endswith("file (1).bin")


def test_conflict_skip_and_replace_and_compare():
    with tempfile.TemporaryDirectory() as td:
        eng, cfg, sb = _mk_engine(td)
        os.makedirs(os.path.join(td, "d"))
        dest = os.path.join(td, "d", "f.bin")
        with open(dest, "wb") as fh:
            fh.write(b"AAAAAAAA")
        src_sha = eng.file_hash(dest)["sha256"]
        assert eng._resolve_conflict({"conflict": "skip"}, dest, 8, src_sha) == "skip"
        assert eng._resolve_conflict({"conflict": "replace"}, dest, 8, src_sha) == "go"
        # identical content + compare → skip
        assert eng._resolve_conflict({"conflict": "compare"}, dest, 8, src_sha) == "skip"
        # same size, different content → go
        # (a real modification advances mtime; this sandbox's fs has coarse
        #  timestamp granularity, so bump it explicitly to model that)
        with open(dest, "wb") as fh:
            fh.write(b"BBBBBBBB")
        st = os.stat(dest)
        os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns + 60 * 1_000_000_000))
        assert eng._resolve_conflict({"conflict": "compare"}, dest, 8, src_sha) == "go"


def test_insufficient_space_guard():
    with tempfile.TemporaryDirectory() as td:
        eng, cfg, sb = _mk_engine(td)
        eng.config.set("min_free_space_bytes", 10 ** 18)   # absurd minimum
        with pytest.raises(FsError) as e:
            eng._check_space(td, 100)
        assert e.value.code == "insufficient_space"
        eng.config.set("min_free_space_bytes", 0)
        eng._check_space(td, 100)          # passes


def test_job_persistence_across_restart():
    with tempfile.TemporaryDirectory() as td:
        eng, cfg, sb = _mk_engine(td)
        job = eng.create_job({"kind": "pull", "src_base": "http://127.0.0.1:1",
                              "src_path": "/x.bin",
                              "dest_path": os.path.join(td, "x.bin")})
        job_id = job["id"]
        eng.stop()
        # new engine instance restores the job as interrupted (resumable)
        eng2, _, _ = _mk_engine(td)
        restored = eng2.get_job(job_id)
        assert restored is not None
        assert restored["status"] == "interrupted"
        eng2.stop()


def test_verified_delete_refuses_checksum_mismatch():
    with tempfile.TemporaryDirectory() as td:
        eng, cfg, sb = _mk_engine(td)
        p = os.path.join(td, "keepme.bin")
        with open(p, "wb") as fh:
            fh.write(b"precious data")
        with pytest.raises(FsError) as e:
            eng.verified_delete(p, "0" * 64)
        assert e.value.code == "verify_mismatch"
        assert os.path.exists(p)          # original preserved
        good = eng.file_hash(p)["sha256"]
        eng.verified_delete(p, good)
        assert not os.path.exists(p)
