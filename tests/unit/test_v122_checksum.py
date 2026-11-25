"""v1.2.2 regressions: long-running source checksum preparation."""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from companion.config import Config  # noqa: E402
from companion.errors import FsError  # noqa: E402
from companion.events import EventBus  # noqa: E402
from companion.fsops import PathSandbox  # noqa: E402
from companion.hashprep import HashPrepareService  # noqa: E402
from companion.peers import PeerClient  # noqa: E402
from companion.transfers import (  # noqa: E402
    HASH_POLL_HTTP_TIMEOUT, HASH_PREPARE_HTTP_TIMEOUT, TransferEngine,
)
from companion.util import sha256_file  # noqa: E402


def _engine(td, delay=0.0):
    cfg = Config(td)
    cfg.set("share_home_rw", False)
    cfg.set("shares", [{"path": td, "label": "lab", "mode": "rw"}])
    sb = PathSandbox(cfg)
    eng = TransferEngine(td, cfg, sb, EventBus(td), None)
    eng.hash_prepare.test_chunk_delay_s = delay
    return eng


def test_peer_default_timeout_unchanged():
    """Ordinary PeerClient requests keep the responsive default timeout."""
    import inspect
    sig = inspect.signature(PeerClient.request)
    assert sig.parameters["timeout"].default == 10.0
    assert HASH_PREPARE_HTTP_TIMEOUT <= 15.0
    assert HASH_POLL_HTTP_TIMEOUT <= 10.0


def test_slow_checksum_does_not_false_interrupt(tmp_path):
    """A checksum lasting >30s must still complete a valid local transfer."""
    root = str(tmp_path)
    src = os.path.join(root, "slow.bin")
    # ~40 chunks at 1s delay ≈ >30s of hashing without a huge file.
    with open(src, "wb") as fh:
        fh.write(b"X" * (40 * 1024 * 1024))
    eng = _engine(root, delay=1.0)
    try:
        t0 = time.time()
        job = eng.create_job({
            "kind": "pull", "src_base": "self", "src_path": src,
            "dest_path": os.path.join(root, "out.bin"),
            "conflict": "replace",
        })
        # Observe hashing status while the slow prepare runs.
        seen_hashing = False
        deadline = time.time() + 90
        while time.time() < deadline:
            cur = eng.get_job(job["id"]) or {}
            if cur.get("status") == "hashing":
                seen_hashing = True
                prep = cur.get("prepare") or {}
                assert prep.get("phase") == "source_checksum"
                assert "processed" in prep
            if cur.get("status") in ("completed", "failed", "interrupted",
                                     "cancelled", "completed_partial"):
                break
            time.sleep(0.2)
        done = eng.get_job(job["id"])
        if done is None:
            # completed jobs move to history
            hist = eng.list_jobs()["history"]
            done = next(j for j in hist if j["id"] == job["id"])
        elapsed = time.time() - t0
        assert elapsed > 30, elapsed
        assert seen_hashing, "Transfer Center must see hashing status"
        assert done["status"] == "completed", done
        assert done["verify"]["status"] == "ok"
        assert done.get("size", 0) == os.path.getsize(src)
        assert os.path.exists(done["dest_path"])
        assert sha256_file(done["dest_path"]) == sha256_file(src)
    finally:
        eng.stop()


def test_duplicate_prepare_joins_same_worker(tmp_path):
    root = str(tmp_path)
    path = os.path.join(root, "dup.bin")
    with open(path, "wb") as fh:
        fh.write(os.urandom(2 * 1024 * 1024))
    cfg = Config(root)
    cfg.set("share_home_rw", False)
    cfg.set("shares", [{"path": root, "label": "lab", "mode": "rw"}])
    sb = PathSandbox(cfg)
    svc = HashPrepareService(sb, test_chunk_delay_s=0.05)
    a = svc.prepare(path)
    b = svc.prepare(path)
    assert a["hash_id"] == b["hash_id"]
    assert b.get("joined") is True
    # Wait for completion
    while svc.status(a["hash_id"])["status"] in ("queued", "running"):
        time.sleep(0.05)
    done = svc.status(a["hash_id"])
    assert done["status"] == "completed"
    assert done["sha256"] == sha256_file(path)


def test_cancel_prepare(tmp_path):
    root = str(tmp_path)
    path = os.path.join(root, "cancel.bin")
    with open(path, "wb") as fh:
        fh.write(b"Z" * (8 * 1024 * 1024))
    cfg = Config(root)
    cfg.set("share_home_rw", False)
    cfg.set("shares", [{"path": root, "label": "lab", "mode": "rw"}])
    sb = PathSandbox(cfg)
    svc = HashPrepareService(sb, test_chunk_delay_s=0.2)
    snap = svc.prepare(path)
    time.sleep(0.1)
    cancelled = svc.cancel(snap["hash_id"])
    assert cancelled["status"] == "cancelled"
    # Late status remains cancelled (or gone after GC — still not completed)
    try:
        late = svc.status(snap["hash_id"])
        assert late["status"] in ("cancelled", "failed")
    except FsError as exc:
        assert exc.code == "no_hash_job"


def test_changed_source_invalidates_prepare(tmp_path):
    root = str(tmp_path)
    path = os.path.join(root, "mutate.bin")
    with open(path, "wb") as fh:
        fh.write(b"A" * (3 * 1024 * 1024))
    cfg = Config(root)
    cfg.set("share_home_rw", False)
    cfg.set("shares", [{"path": root, "label": "lab", "mode": "rw"}])
    sb = PathSandbox(cfg)
    svc = HashPrepareService(sb, test_chunk_delay_s=0.15)
    snap = svc.prepare(path)
    time.sleep(0.05)
    # Mutate size/mtime while hashing.
    with open(path, "ab") as fh:
        fh.write(b"MORE")
    deadline = time.time() + 20
    final = None
    while time.time() < deadline:
        final = svc.status(snap["hash_id"])
        if final["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert final["status"] == "failed"
    assert "changed" in (final.get("error") or "").lower()


def test_verified_delete_still_refuses_stale_cache(tmp_path):
    """Destructive deletion must never trust the prepare/cache digest alone."""
    root = str(tmp_path)
    path = os.path.join(root, "guard.bin")
    with open(path, "wb") as fh:
        fh.write(b"keep-me")
    eng = _engine(root)
    try:
        good = eng.file_hash(path)["sha256"]
        # Poison cache with a wrong digest under the current identity key.
        st = os.stat(path)
        real = os.path.realpath(path)
        eng._hash_cache[real] = (f"{real}:{st.st_size}:{st.st_mtime_ns}",
                                 "0" * 64)
        with pytest.raises(FsError) as denied:
            eng.verified_delete(path, "0" * 64)
        assert denied.value.code == "verify_mismatch"
        assert os.path.exists(path)
        # Correct fresh checksum still deletes.
        eng.verified_delete(path, good)
        assert not os.path.exists(path)
    finally:
        eng.stop()


def test_transfers_js_exposes_hashing_status():
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    text = open(os.path.join(root, "web/js/views/transfers.js"),
                encoding="utf-8").read()
    assert "CALCULATING SOURCE CHECKSUM" in text
    assert "hashing" in text
    assert "source_checksum" in text
