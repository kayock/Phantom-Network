"""Long-running source SHA-256 preparation with progress and joinable workers.

Ordinary peer HTTP calls keep short timeouts. Callers start or join a prepare
job, then poll with short requests until the digest is ready. Identical
unchanged sources share one worker.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable, Dict, Optional

from .errors import FsError
from .util import now_ms, sha256_file


def _key(real: str, size: int, mtime_ns: int) -> str:
    return f"{real}:{size}:{mtime_ns}"


class HashPrepareService:
    """Background SHA-256 jobs keyed by (path, size, mtime_ns)."""

    def __init__(self, sandbox, hash_cache: Optional[dict] = None,
                 log=None, test_chunk_delay_s: float = 0.0):
        self.sandbox = sandbox
        self._hash_cache = hash_cache if hash_cache is not None else {}
        self.log = log
        # Deterministic slow-hash hook for regression tests only.
        self.test_chunk_delay_s = float(test_chunk_delay_s or 0.0)
        self._lock = threading.RLock()
        self._jobs: Dict[str, dict] = {}
        self._by_key: Dict[str, str] = {}     # identity key -> job id
        self._cancels: Dict[str, threading.Event] = {}

    # ------------------------------------------------------------------
    def prepare(self, path: str) -> dict:
        """Start or join hashing for ``path``. Returns a public snapshot."""
        real = self.sandbox.authorize(path, "r")
        if not os.path.isfile(real):
            raise FsError(400, "not_a_file", "Path is not a regular file")
        try:
            st = os.stat(real)
        except OSError as exc:
            raise FsError(500, "os_error", str(exc)) from exc
        if not os.path.isfile(real):
            raise FsError(400, "not_a_file", "Path is not a regular file")

        key = _key(real, st.st_size, st.st_mtime_ns)
        # Fast path: process cache for this exact identity.
        cached = self._hash_cache.get(real)
        cache_key = f"{real}:{st.st_size}:{st.st_mtime_ns}"
        if cached and cached[0] == cache_key:
            return {
                "ok": True, "hash_id": None, "status": "completed",
                "path": real, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                "processed": st.st_size, "sha256": cached[1],
                "speed_bps": 0.0, "eta_s": 0, "elapsed_s": 0.0,
                "error": None, "joined": False, "cached": True,
            }

        with self._lock:
            existing = self._by_key.get(key)
            if existing and existing in self._jobs:
                job = self._jobs[existing]
                if job["status"] in ("queued", "running", "completed"):
                    snap = self._snapshot(job)
                    snap["joined"] = True
                    snap["cached"] = False
                    return snap
            job_id = f"hash_{now_ms()}_{os.urandom(3).hex()}"
            job = {
                "id": job_id,
                "key": key,
                "path": real,
                "size": st.st_size,
                "mtime_ns": st.st_mtime_ns,
                "processed": 0,
                "status": "queued",
                "sha256": None,
                "error": None,
                "started": None,
                "finished": None,
                "speed_bps": 0.0,
                "eta_s": None,
                "elapsed_s": 0.0,
            }
            cancel = threading.Event()
            self._jobs[job_id] = job
            self._by_key[key] = job_id
            self._cancels[job_id] = cancel
            threading.Thread(target=self._worker, args=(job_id,),
                             name=f"pn-hash-{job_id[-6:]}",
                             daemon=True).start()
            snap = self._snapshot(job)
            snap["joined"] = False
            snap["cached"] = False
            return snap

    def status(self, hash_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(hash_id)
            if job is None:
                raise FsError(404, "no_hash_job", "Unknown hash prepare job")
            snap = self._snapshot(job)
            snap["joined"] = False
            snap["cached"] = False
            return snap

    def cancel(self, hash_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(hash_id)
            if job is None:
                raise FsError(404, "no_hash_job", "Unknown hash prepare job")
            cancel = self._cancels.get(hash_id)
            if cancel is not None:
                cancel.set()
            if job["status"] in ("queued", "running"):
                job["status"] = "cancelled"
                job["error"] = "Cancelled"
                job["finished"] = now_ms()
                self._by_key.pop(job["key"], None)
            snap = self._snapshot(job)
            snap["joined"] = False
            snap["cached"] = False
            return snap

    def stop_all(self) -> None:
        with self._lock:
            for ev in self._cancels.values():
                ev.set()

    # ------------------------------------------------------------------
    def _worker(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            cancel = self._cancels.get(job_id)
            if job is None or cancel is None:
                return
            job["status"] = "running"
            job["started"] = now_ms()
            real = job["path"]
            size = job["size"]
            mtime_ns = job["mtime_ns"]
            key = job["key"]
        started = time.monotonic()
        last_pub = 0.0

        def progress(processed: int) -> None:
            nonlocal last_pub
            if self.test_chunk_delay_s > 0:
                time.sleep(self.test_chunk_delay_s)
            now = time.monotonic()
            elapsed = max(now - started, 1e-6)
            with self._lock:
                j = self._jobs.get(job_id)
                if j is None:
                    return
                j["processed"] = processed
                j["elapsed_s"] = round(elapsed, 3)
                j["speed_bps"] = processed / elapsed
                remain = max(size - processed, 0)
                j["eta_s"] = (remain / j["speed_bps"]) if j["speed_bps"] else None
            # keep CPU free for polls; progress is query-driven
            if now - last_pub > 0.25:
                last_pub = now

        try:
            # Re-check identity immediately before reading bytes.
            st = os.stat(real)
            if st.st_size != size or st.st_mtime_ns != mtime_ns:
                raise FsError(409, "source_changed",
                              "Source file changed before checksum started")
            digest = sha256_file(real, cancel=cancel, progress_cb=progress)
            st2 = os.stat(real)
            if st2.st_size != size or st2.st_mtime_ns != mtime_ns:
                raise FsError(409, "source_changed",
                              "Source file changed during checksum calculation")
            cache_key = f"{real}:{size}:{mtime_ns}"
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None:
                    return
                if cancel.is_set() or job["status"] == "cancelled":
                    job["status"] = "cancelled"
                    job["error"] = job.get("error") or "Cancelled"
                else:
                    job["sha256"] = digest
                    job["processed"] = size
                    job["status"] = "completed"
                    job["error"] = None
                    job["eta_s"] = 0
                    self._hash_cache[real] = (cache_key, digest)
                job["finished"] = now_ms()
                job["elapsed_s"] = round(time.monotonic() - started, 3)
                if job["status"] == "completed":
                    self._by_key.pop(key, None)
        except KeyboardInterrupt:
            with self._lock:
                job = self._jobs.get(job_id)
                if job is not None:
                    job["status"] = "cancelled"
                    job["error"] = "Cancelled"
                    job["finished"] = now_ms()
                    self._by_key.pop(key, None)
        except FsError as exc:
            with self._lock:
                job = self._jobs.get(job_id)
                if job is not None:
                    job["status"] = "failed"
                    job["error"] = exc.message
                    job["finished"] = now_ms()
                    self._by_key.pop(key, None)
        except Exception as exc:
            if self.log:
                self.log.exception("hash prepare %s failed", job_id)
            with self._lock:
                job = self._jobs.get(job_id)
                if job is not None:
                    job["status"] = "failed"
                    job["error"] = str(exc) or exc.__class__.__name__
                    job["finished"] = now_ms()
                    self._by_key.pop(key, None)
        finally:
            # Drop finished jobs after a grace window so late pollers succeed.
            def _gc():
                time.sleep(120)
                with self._lock:
                    j = self._jobs.get(job_id)
                    if j and j["status"] in ("completed", "failed", "cancelled"):
                        self._jobs.pop(job_id, None)
                        self._cancels.pop(job_id, None)
            threading.Thread(target=_gc, name="pn-hash-gc", daemon=True).start()

    @staticmethod
    def _snapshot(job: dict) -> dict:
        return {
            "ok": True,
            "hash_id": job["id"],
            "status": job["status"],
            "path": job["path"],
            "size": job["size"],
            "mtime_ns": job["mtime_ns"],
            "processed": job["processed"],
            "sha256": job["sha256"],
            "speed_bps": job.get("speed_bps") or 0.0,
            "eta_s": job.get("eta_s"),
            "elapsed_s": job.get("elapsed_s") or 0.0,
            "error": job.get("error"),
        }
