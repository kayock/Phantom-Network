"""High-performance file transfer engine (destination-pull architecture).

How a transfer works
--------------------
The Transfer Center UI creates a **pull job on the destination companion**
(``POST /api/jobs``).  The destination companion then:

1. resolves conflicts at the destination (skip / rename / replace / compare),
2. pre-checks disk space,
3. asks the *source* companion for metadata (size + cached SHA-256),
4. streams the file in 1 MiB ``Range`` chunks from the source,
   hashing SHA-256 **while receiving** (no extra full read on the receiver),
5. writes to a ``.part`` temporary file, fsyncs, then renames into place
   only after the checksum comparison succeeded,
6. for *move* jobs, asks the source to ``verified-delete`` the original
   (the source independently re-checks the checksum before unlinking).

Streams never load whole files into memory.  Job state is persisted to
``jobs.json`` continuously so interrupted transfers resume from the exact
byte offset of the ``.part`` file after a crash or restart.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional

from .fsops import windows_name_problem
from .errors import FsError
from .hashprep import HashPrepareService
from .peers import PeerClient, PeerHttpError, PeerUnreachable
from .util import atomic_write_json, now_ms

CHUNK = 1024 * 1024
HISTORY_CAP = 300
PROGRESS_EVERY_S = 1.0
AUTO_RETRY = 3
# Short peer timeouts for prepare/poll. Long hashes complete via polling,
# not by holding one HTTP request open for minutes.
HASH_PREPARE_HTTP_TIMEOUT = 15.0
HASH_POLL_HTTP_TIMEOUT = 10.0
HASH_POLL_IDLE_S = 0.5


class RateLimiter:
    """Shared token-bucket bandwidth limiter (bytes/sec, 0 = unlimited)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._bps = 0.0
        self._tokens = 0.0
        self._last = time.monotonic()

    def set_bps(self, bps: float) -> None:
        with self._lock:
            self._bps = max(0.0, float(bps))
            self._last = time.monotonic()

    def acquire(self, nbytes: int) -> None:
        """Block until ``nbytes`` of budget is available (or no limit set)."""
        with self._lock:
            if self._bps <= 0:
                return
            now = time.monotonic()
            self._tokens += (now - self._last) * self._bps
            self._last = now
            self._tokens = min(self._tokens, self._bps)  # no deep bursts
            need = float(nbytes)
            if self._tokens >= need:
                self._tokens -= need
                return
            deficit = need - self._tokens
            self._tokens = 0.0
            wait = deficit / self._bps
        time.sleep(min(wait, 5.0))
        # the slept time covered exactly the deficit; restart the clock so
        # the refill during the sleep is not counted twice
        with self._lock:
            self._last = time.monotonic()


class TransferEngine:
    """Per-companion job queue executing pull/pull_dir/move jobs."""

    def __init__(self, data_dir: str, config, sandbox, events, sse, log=None):
        self.data_dir = data_dir
        self.config = config
        self.sandbox = sandbox
        self.events = events
        self.sse = sse
        self.log = log
        self.jobs_path = os.path.join(data_dir, "jobs.json")
        self.state_path = os.path.join(data_dir, "jobs_state.json")
        self._lock = threading.RLock()
        self._jobs: Dict[str, dict] = {}
        self._history: List[dict] = []
        self._cancel_events: Dict[str, threading.Event] = {}
        self._pause_flags: Dict[str, threading.Event] = {}
        self._worker_done: Dict[str, threading.Event] = {}
        self._wake = threading.Event()          # wakes the scheduler
        self._stop = threading.Event()
        self.limiter = RateLimiter()
        self.limiter.set_bps(config.get("bandwidth_limit_bps") or 0)
        config.on_change(self._on_config_change)
        self._client = PeerClient(self._token_provider)
        self._hash_cache: Dict[str, tuple] = {}   # path -> (cache_key, sha)
        self.hash_prepare = HashPrepareService(
            sandbox, hash_cache=self._hash_cache, log=log)
        self._load_state()
        self._pool: Optional[ThreadPoolExecutor] = None
        threading.Thread(target=self._scheduler_loop, name="pn-xfer-sched",
                         daemon=True).start()

    # ------------------------------------------------------------------
    def _token_provider(self) -> str:
        if self.config.get("require_token"):
            return self.config.get("auth_token", "")
        return ""

    def _on_config_change(self, key, value):
        if key == "bandwidth_limit_bps":
            self.limiter.set_bps(value or 0)

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def _load_state(self) -> None:
        state = {}
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            pass
        for job in state.get("active", []):
            # Anything running when we died is now "interrupted"; resumable.
            if job.get("status") in ("running", "verifying", "queued",
                                     "paused", "hashing"):
                job["status"] = "interrupted"
            self._jobs[job["id"]] = job
            self._cancel_events[job["id"]] = threading.Event()
            self._pause_flags[job["id"]] = threading.Event()
            self._worker_done[job["id"]] = threading.Event()
        self._history = state.get("history", [])[-HISTORY_CAP:]

    def _persist(self) -> None:
        with self._lock:
            # the walk manifest is in-memory-only (deterministic & cheap to
            # re-enumerate after a restart) — serializing it per file would
            # make directory jobs quadratic
            active = [self._public_job_state(j) for j in self._jobs.values()
                      if j["status"] not in ("completed", "failed",
                                             "cancelled")]
            data = {"active": active, "history": self._history[-HISTORY_CAP:],
                    "saved": now_ms()}
            atomic_write_json(self.state_path, data)

    @staticmethod
    def _public_job_state(job: dict) -> dict:
        return {k: v for k, v in job.items() if k != "manifest"}

    # ------------------------------------------------------------------
    # job creation / control API
    # ------------------------------------------------------------------
    def create_job(self, spec: dict) -> dict:
        """Create and enqueue a transfer job.  See API_REFERENCE.md."""
        kind = spec.get("kind", "pull")
        if kind not in ("pull", "pull_dir"):
            raise FsError(400, "bad_kind", f"Unsupported job kind: {kind}")
        if not spec.get("src_base") or not spec.get("src_path"):
            raise FsError(400, "bad_spec", "src_base and src_path are required")
        if not spec.get("dest_path"):
            raise FsError(400, "bad_spec", "dest_path is required")
        job_id = spec.get("id") or f"job_{now_ms()}_{os.urandom(3).hex()}"
        job = {
            "id": job_id,
            "kind": kind,
            "src_base": str(spec["src_base"]).rstrip("/"),
            "src_path": spec["src_path"],
            "dest_path": spec["dest_path"],
            "dest_device": spec.get("dest_device", "self"),
            "src_device": spec.get("src_device", "remote"),
            "size": spec.get("size") or 0,
            "transferred": 0,
            "status": "queued",
            "verify": {"expected": spec.get("sha256") or None,
                       "actual": None, "status": "pending"},
            "speed_bps": 0.0, "avg_bps": 0.0, "eta_s": None,
            "error": None,
            "conflict": spec.get("conflict", "rename"),
            "delete_source": bool(spec.get("delete_source")),
            "files_total": spec.get("files_total") or (1 if kind == "pull" else 0),
            "files_done": 0,
            "current_file": None,
            "created": now_ms(), "started": None, "finished": None,
            "retries": 0,
            "prepare": None,   # checksum preparation progress (hashing)
        }
        with self._lock:
            self._jobs[job_id] = job
            self._cancel_events[job_id] = threading.Event()
            self._pause_flags[job_id] = threading.Event()
            self._worker_done[job_id] = threading.Event()
        self._persist()
        self._publish(job, "queued")
        self._wake.set()
        return job

    def list_jobs(self) -> dict:
        with self._lock:
            active = [dict(j) for j in self._jobs.values()]
            history = [dict(j) for j in self._history]
        return {"jobs": active, "history": history}

    def get_job(self, job_id: str) -> Optional[dict]:
        with self._lock:
            return dict(self._jobs[job_id]) if job_id in self._jobs else None

    def control(self, job_id: str, action: str) -> dict:
        """pause | resume | cancel | retry — returns updated job or None.

        Terminal jobs live in history; ``retry`` re-enqueues them from there
        (a silent no-op would leave the operator staring at a dead job).
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None and action == "retry":
                for i, hjob in enumerate(self._history):
                    if hjob.get("id") == job_id:
                        if hjob.get("status") not in ("failed", "cancelled",
                                                      "completed",
                                                      "completed_partial"):
                            break
                        self._history.pop(i)
                        job = dict(hjob)
                        job["status"] = "queued"
                        job["error"] = None
                        job["retries"] = 0
                        job["transferred"] = 0
                        job["files_done"] = 0
                        job["current_file"] = None
                        job["started"] = None
                        job["finished"] = None
                        job["move_error"] = None
                        job["speed_bps"] = 0.0
                        job["verify"] = {"expected": None, "actual": None,
                                         "status": "pending"}
                        job.pop("src_identity", None)
                        job.pop("source_changed", None)
                        job.pop("manifest", None)      # re-enumerate fresh
                        job.pop("file_index", None)
                        self._jobs[job_id] = job
                        self._cancel_events[job_id] = threading.Event()
                        self._pause_flags[job_id] = threading.Event()
                        self._worker_done[job_id] = threading.Event()
                        break
        if job is None:
            return None
        if action == "pause":
            if job["status"] in ("running", "queued", "verifying", "hashing"):
                self._pause_flags[job_id].set()
                # drain the worker FIRST so a later resume can never race a
                # still-running writer on the same .part file
                done = self._worker_done.get(job_id)
                if done is not None:
                    done.wait(timeout=10)
                if self._jobs.get(job_id) is job and \
                        job["status"] in ("running", "verifying", "hashing"):
                    self._set_status(job, "paused")
        elif action == "resume":
            if job["status"] in ("paused", "interrupted", "failed"):
                job["error"] = None
                job["retries"] = 0
                self._pause_flags[job_id].clear()
                self._cancel_events[job_id].clear()
                self._worker_done[job_id] = threading.Event()
                self._set_status(job, "queued")
                self._wake.set()
        elif action == "cancel":
            self._cancel_events[job_id].set()
            self._pause_flags[job_id].clear()
            prep = job.get("prepare") or {}
            if prep.get("hash_id") and job.get("src_base") == "self":
                try:
                    self.hash_prepare.cancel(prep["hash_id"])
                except FsError:
                    pass
            elif prep.get("hash_id") and job.get("src_base") not in (None, "self"):
                try:
                    self._client.request(
                        "POST",
                        f"{job['src_base']}/api/fs/hash/prepare/"
                        f"{prep['hash_id']}/cancel",
                        body={}, timeout=HASH_PREPARE_HTTP_TIMEOUT)
                except Exception:
                    pass
            if job["status"] in ("queued", "paused", "interrupted", "hashing"):
                self._set_status(job, "cancelled", error="Cancelled by user")
            # running workers observe the event and finish the cancel
        elif action == "retry":
            if job["status"] in ("failed", "interrupted", "cancelled"):
                job["error"] = None
                job["retries"] = 0
                job["prepare"] = None
                if job.get("source_changed"):
                    # explicit user choice: discard the stale partial data
                    part = job["dest_path"] + ".part"
                    try:
                        if os.path.exists(part):
                            os.truncate(part, 0)
                    except OSError:
                        pass
                    job["src_identity"] = None
                    job["transferred"] = 0
                    job["source_changed"] = False
                    # the source legitimately changed — re-fetch its current
                    # hash instead of comparing against the stale expectation
                    job["verify"]["expected"] = None
                    job["verify"]["actual"] = None
                    job["verify"]["status"] = "pending"
                self._pause_flags[job_id].clear()
                self._cancel_events[job_id].clear()
                self._worker_done[job_id] = threading.Event()
                self._set_status(job, "queued")
                self._wake.set()
        self._persist()
        return job

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()
        self._persist()

    # ------------------------------------------------------------------
    # scheduler
    # ------------------------------------------------------------------
    def _scheduler_loop(self) -> None:
        max_parallel = max(1, int(self.config.get("max_concurrent_jobs") or 1))
        with ThreadPoolExecutor(max_workers=max_parallel + 2,
                                thread_name_prefix="pn-xfer") as pool:
            self._pool = pool
            while not self._stop.is_set():
                launched = 0
                with self._lock:
                    queued = [j for j in self._jobs.values()
                              if j["status"] == "queued"]
                    running = sum(1 for j in self._jobs.values()
                                  if j["status"] in ("running", "verifying",
                                                     "hashing"))
                    queued.sort(key=lambda j: j["created"])
                for job in queued:
                    if running + launched >= max_parallel:
                        break
                    self._set_status(job, "running")
                    done_ev = self._worker_done.get(job["id"])
                    if done_ev is not None:
                        done_ev.clear()
                    pool.submit(self._run_job, job)
                    launched += 1
                self._wake.wait(timeout=1.0)
                self._wake.clear()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        try:
            self.hash_prepare.stop_all()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # status plumbing
    # ------------------------------------------------------------------
    def _set_status(self, job: dict, status: str, error: Optional[str] = None):
        job["status"] = status
        if error is not None:
            job["error"] = error
        if status == "running" and job.get("started") is None:
            job["started"] = now_ms()
        if status in ("completed", "completed_partial", "failed", "cancelled"):
            job["finished"] = now_ms()
            with self._lock:
                self._history.append(dict(job))
                del self._history[:-HISTORY_CAP]
                self._jobs.pop(job["id"], None)
                self._cancel_events.pop(job["id"], None)
                self._pause_flags.pop(job["id"], None)
        self._persist()
        self._publish(job, status)
        self._emit_event(job, status)

    def _publish(self, job: dict, status: str) -> None:
        if self.sse:
            self.sse.publish("transfer", {
                "id": job["id"], "status": job["status"],
                "transferred": job["transferred"], "size": job["size"],
                "speed_bps": job["speed_bps"], "eta_s": job["eta_s"],
                "files_done": job["files_done"], "files_total": job["files_total"],
                "verify": job["verify"], "error": job["error"],
                "prepare": job.get("prepare"),
            })

    def _emit_event(self, job: dict, status: str) -> None:
        if not self.events:
            return
        mapping = {
            "running": "transfer_started", "completed": "transfer_completed",
            "completed_partial": "transfer_completed",
            "failed": "transfer_failed", "cancelled": "transfer_failed",
            "paused": "transfer_paused", "queued": "transfer_resumed",
        }
        etype = mapping.get(status)
        if not etype:
            return
        text = {
            "running": f"Transfer started: {job['src_path']} → {job['dest_path']}",
            "completed": f"Transfer completed: {job['dest_path']}",
            "completed_partial": f"Transfer finished with source retained: "
                                 f"{job['dest_path']} ({job.get('move_error') or ''})",
            "failed": f"Transfer failed: {job['dest_path']} ({job.get('error') or status})",
            "cancelled": f"Transfer cancelled: {job['dest_path']}",
            "paused": f"Transfer paused: {job['dest_path']}",
            "queued": f"Transfer queued/resumed: {job['dest_path']}",
        }.get(status, "")
        if etype == "transfer_completed" and job["verify"]["status"] == "ok":
            text += " (SHA-256 verified)"
        self.events.emit(etype, text,
                         severity="error" if status == "failed" else "info",
                         data={"job_id": job["id"], "src": job["src_path"],
                               "dest": job["dest_path"], "status": status})

    def _touch_progress(self, job: dict, force: bool = False) -> None:
        now = time.monotonic()
        last = job.get("_last_progress_t", 0.0)
        if force or now - last >= PROGRESS_EVERY_S:
            job["_last_progress_t"] = now
            self._publish(job, job["status"])

    # ------------------------------------------------------------------
    # source-side helpers (hash cache, verified delete, walk)
    # ------------------------------------------------------------------
    def file_hash(self, path: str) -> dict:
        """SHA-256 of a local file, cached by (size, mtime).  Used by the
        source side so each huge file is read at most once per change."""
        from .util import sha256_file
        real = self.sandbox.authorize(path, "r")
        st = os.stat(real)
        # mtime_ns: same-second rewrites with equal size must not hit a
        # stale cache entry
        key = f"{real}:{st.st_size}:{st.st_mtime_ns}"
        cached = self._hash_cache.get(real)
        if cached and cached[0] == key:
            return {"path": real, "size": st.st_size, "sha256": cached[1],
                    "mtime_ns": st.st_mtime_ns}
        cancel = threading.Event()
        digest = sha256_file(real, cancel=cancel)
        self._hash_cache[real] = (key, digest)
        return {"path": real, "size": st.st_size, "sha256": digest,
                "mtime_ns": st.st_mtime_ns}

    def verified_delete(self, path: str, sha256: str,
                        expected_mtime_ns: int = None,
                        expected_size: int = None) -> dict:
        """Delete a source file ONLY when current content + identity match.

        v1.2.0 policy (destructive operation): the checksum CACHE IS NEVER
        TRUSTED HERE.  The current bytes are re-hashed immediately before
        deletion, and the file identity (st_dev, st_ino, size, mtime_ns) is
        captured before hashing and re-checked right before unlink so a
        modification or replacement during/after verification refuses the
        deletion.  Symbolic links are never followed into deletion.  Any
        doubt → FsError → the move reports completed_partial with the source
        preserved.  The (size, mtime_ns)-keyed cache remains in use ONLY for
        non-destructive work (compare-conflict, sync previews).
        """
        real = self.sandbox.authorize(path, "r")
        # lstat the RAW path: authorize() realpaths, which would hide a
        # symlink — deleting through a link could remove the wrong data
        lst_raw = os.lstat(path)
        if stat.S_ISLNK(lst_raw.st_mode):
            raise FsError(409, "symlink_refused",
                          "Source is a symbolic link; deleting it could "
                          "remove the wrong data - source preserved.")
        lst = os.lstat(real)
        identity1 = (lst.st_dev, lst.st_ino, lst.st_size, lst.st_mtime_ns)
        if expected_size is not None and lst.st_size != expected_size:
            raise FsError(409, "source_changed",
                          "Source size changed since the copy was verified; "
                          "refusing to delete (original preserved)")
        # destructive step 1: hash the CURRENT content, bypassing the cache
        from .util import sha256_file
        cancel = threading.Event()
        current = sha256_file(real, cancel=cancel)
        if current != sha256:
            raise FsError(409, "verify_mismatch",
                          "Source checksum changed (stale or modified "
                          "source); refusing to delete (original preserved)")
        # destructive step 2: nothing changed while/after hashing
        lst2 = os.lstat(real)
        identity2 = (lst2.st_dev, lst2.st_ino, lst2.st_size, lst2.st_mtime_ns)
        if identity2 != identity1:
            raise FsError(409, "source_changed",
                          "Source changed during deletion verification; "
                          "refusing to delete (original preserved)")
        if expected_mtime_ns is not None and \
                lst2.st_mtime_ns != expected_mtime_ns:
            raise FsError(409, "source_changed",
                          "Source mtime changed since the copy was verified; "
                          "refusing to delete (original preserved)")
        real_w = self.sandbox.authorize(path, "w")
        try:
            os.remove(real_w)
        except (OSError, PermissionError) as exc:
            raise FsError(500, "source_delete_failed",
                          f"Copy verified but the source could not be removed: "
                          f"{exc}. Data is safe in both places.")
        self._hash_cache.pop(real, None)     # dropped: no longer valid
        return {"deleted": real_w, "rehashed": True}

    def walk_remote_manifest(self, path: str, skip: int = 0, limit: int = 5000,
                             max_files: int = None) -> dict:
        """Paginated flat recursive listing for directory transfers & sync.

        Returns a deterministic window [skip, skip+limit) of the tree plus
        totals and ``next_offset``.  Directory pulls fetch pages until
        ``next_offset`` is null — nothing is silently omitted.  Trees beyond
        ``max_dir_files`` (config, default 200 000) are REFUSED
        (``413 tree_too_large``) instead of being truncated.
        """
        real = self.sandbox.authorize(path, "r")
        hard_cap = int(self.config.get("max_dir_files") or 200000)
        limit = max(1, int(limit or 5000))
        skip = max(0, int(skip or 0))
        files, dirs = [], []
        total_files = 0
        total_bytes = 0
        for base, dnames, fnames in os.walk(real):
            dnames.sort(key=str.lower)
            fnames.sort(key=str.lower)
            for d in dnames:
                dirs.append(os.path.relpath(os.path.join(base, d), real)
                            .replace(os.sep, "/"))
            for f in fnames:
                if total_files >= hard_cap:
                    raise FsError(413, "tree_too_large",
                                  f"Directory tree exceeds the {hard_cap}-file "
                                  f"limit (max_dir_files). Transfer refused "
                                  f"instead of silently truncating.")
                full = os.path.join(base, f)
                try:
                    st = os.stat(full)
                except OSError:
                    # inaccessible files are COUNTED and reported, never
                    # silently omitted
                    if skip <= total_files < skip + limit:
                        files.append({"rel": os.path.relpath(full, real)
                                      .replace(os.sep, "/"), "path": full,
                                      "size": None, "mtime": 0,
                                      "inaccessible": True})
                    total_files += 1
                    continue
                if skip <= total_files < skip + limit:
                    files.append({
                        "rel": os.path.relpath(full, real).replace(os.sep, "/"),
                        "path": full, "size": st.st_size,
                        "mtime": int(st.st_mtime * 1000),
                    })
                total_files += 1
                total_bytes += st.st_size
        next_offset = skip + limit if skip + limit < total_files else None
        return {"root": real,
                "dirs": dirs if skip == 0 else [],
                "files": files,
                "total_files": total_files, "total_bytes": total_bytes,
                "skip": skip, "limit": limit,
                "next_offset": next_offset}

    # ------------------------------------------------------------------
    # job execution
    # ------------------------------------------------------------------
    def _run_job(self, job: dict) -> None:
        try:
            if job["kind"] == "pull":
                self._run_pull(job)
            else:
                self._run_pull_dir(job)
        except (_PauseExit,):
            return                                    # job stays paused
        except (_TransferAbort, InterruptedError):
            cancel = self._cancel_events.get(job["id"])
            if cancel is not None and cancel.is_set() and job["id"] in self._jobs:
                self._set_status(job, "cancelled", error="Cancelled by user")
            return
        except (PeerUnreachable, PeerHttpError, FsError, OSError) as exc:
            self._fail_or_retry(job, exc)
        except Exception as exc:                        # unexpected bug
            if self.log:
                self.log.exception("job %s crashed", job["id"])
            self._fail_or_retry(job, exc)
        finally:
            done_ev = self._worker_done.get(job["id"])
            if done_ev is not None:
                done_ev.set()
            self._touch_progress(job, force=True)

    def _fail_or_retry(self, job: dict, exc: Exception) -> None:
        job["retries"] = job.get("retries", 0) + 1
        recoverable = isinstance(exc, (PeerUnreachable, ConnectionError, OSError))
        if recoverable and job["retries"] <= AUTO_RETRY:
            if self.log:
                self.log.warning("job %s network error (%s), retry %d/%d",
                                 job["id"], exc, job["retries"], AUTO_RETRY)
            time.sleep(min(2 * job["retries"], 6))
            self._set_status(job, "queued")
            self._wake.set()
            return
        msg = str(exc) or exc.__class__.__name__
        # .part file stays on disk → resume is possible
        self._set_status(job, "failed" if not recoverable else "interrupted",
                         error=msg)

    # ------------------------------------------------------------------
    def _resolve_conflict(self, job: dict, dest_path: str, size: int,
                          src_sha: str) -> str:
        """Return 'go' | 'skip' | 'renamed:<path>' for an existing dest file."""
        conflict = job.get("conflict", "rename")
        if not os.path.exists(dest_path):
            return "go"
        if conflict == "skip":
            return "skip"
        if conflict == "replace":
            return "go"
        if conflict == "compare":
            try:
                dst_size = os.path.getsize(dest_path)
            except OSError:
                return "go"
            if dst_size != size:
                return "go"           # different content → transfer
            try:
                info = self.file_hash(dest_path)
            except FsError:
                return "go"
            if info["sha256"] == src_sha:
                return "skip"         # identical already
            return "go"
        # default: rename  →  "name (1).ext"
        base, ext = os.path.splitext(dest_path)
        for i in range(1, 1000):
            cand = f"{base} ({i}){ext}"
            if not os.path.exists(cand):
                job["dest_path"] = cand
                return "renamed"
        raise FsError(409, "conflict", "Could not find a free rename slot")

    # ------------------------------------------------------------------
    def _run_pull(self, job: dict) -> None:
        cancel = self._cancel_events[job["id"]]
        pause = self._pause_flags[job["id"]]
        src_base, src_path = job["src_base"], job["src_path"]

        if job["src_base"] == "self":
            real_src = self.sandbox.authorize(src_path, "r")
            meta = self._await_source_hash(job, local_path=src_path)
            size, src_sha = meta["size"], meta.get("sha256")
            src_mtime_ns = meta.get("mtime_ns")
        else:
            meta = self._await_source_hash(job, src_base=src_base,
                                          src_path=src_path)
            size, src_sha = meta["size"], meta.get("sha256")
            src_mtime_ns = meta.get("mtime_ns")
            real_src = None
        if not size and not src_sha:
            raise FsError(500, "hash_failed",
                          meta.get("error") or "Source checksum failed")
        job["size"] = size
        if job["verify"]["expected"] is None:
            job["verify"]["expected"] = src_sha
        # Leave hashing for transfer proper.
        if job.get("status") == "hashing":
            self._set_status(job, "running")
        # source identity binding: resume must never continue against a
        # source file that changed since the transfer started
        identity = {"size": size, "mtime_ns": src_mtime_ns}
        stored = job.get("src_identity")
        if stored is not None and job["transferred"] > 0 and (
                stored.get("size") != identity["size"] or (
                    identity["mtime_ns"] is not None and
                    stored.get("mtime_ns") not in (None, identity["mtime_ns"]))):
            job["source_changed"] = True
            self._set_status(job, "failed",
                             error="Source file changed since this transfer "
                                   "started (size/mtime mismatch). Partial "
                                   "data retained at the .part file — retry "
                                   "with conflict 'replace' to discard it or "
                                   "'skip' to keep it.")
            raise _TransferAbort()
        job["src_identity"] = identity

        parent_dir = os.path.dirname(job["dest_path"]) or "/"
        self.sandbox.authorize(parent_dir, "w")     # parent must be writable
        os.makedirs(parent_dir, exist_ok=True)
        dest_path = job["dest_path"]
        self._check_dest_name(dest_path)

        verdict = self._resolve_conflict(job, dest_path, size, src_sha or "")
        if verdict == "skip":
            job["transferred"] = size
            job["verify"].update({"status": "ok", "actual": src_sha})
            self._set_status(job, "completed")
            return
        if verdict == "renamed":
            dest_path = job["dest_path"]       # conflict resolver renamed it

        part_path = dest_path + ".part"
        expected_sha = job["verify"]["expected"]

        # resume support: reuse existing .part bytes — the space check must
        # account for what is already on disk, or resuming a half-written
        # large file would falsely fail on a disk it originally fit in
        offset = 0
        if os.path.exists(part_path):
            offset = os.path.getsize(part_path)
            if offset > size:
                offset = 0
                os.truncate(part_path, 0)
        job["transferred"] = offset
        self._check_space(dest_path, size - offset)

        fh = open(part_path, "ab")
        try:
            if job["src_base"] == "self":
                self._stream_local(real_src, fh, offset, size, job, cancel, pause)
            else:
                self._stream_remote(src_base, src_path, fh, offset, size,
                                    job, cancel, pause)
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fh.close()

        if cancel.is_set():
            self._set_status(job, "cancelled", error="Cancelled by user")
            raise _TransferAbort()

        # verification (never report complete without it)
        self._set_status(job, "verifying")
        actual = self.file_hash(part_path)["sha256"]
        job["verify"]["actual"] = actual
        if expected_sha and actual != expected_sha:
            job["verify"]["status"] = "failed"
            # preserve the corrupt .part for diagnosis; keep the original
            # source untouched; a retry re-transfers from the offset that
            # failed verification only if the user removes the .part, so we
            # move it aside instead:
            try:
                os.replace(part_path, part_path + ".corrupt")
            except OSError:
                pass
            self._set_status(job, "failed",
                             error="SHA-256 mismatch: received data corrupted; "
                                   "destination file NOT finalized; original preserved")
            if self.events:
                self.events.emit("verification_failed",
                                 f"Integrity verification failed: {dest_path}",
                                 severity="error", data={"job_id": job["id"]})
            raise _TransferAbort()

        job["verify"]["status"] = "ok"
        os.replace(part_path, dest_path)   # atomic finalize
        try:
            # match source mtime for sync friendliness
            if job["src_base"] == "self":
                st = os.stat(real_src)
            else:
                st = None
            if st:
                os.utime(dest_path, (st.st_atime, st.st_mtime))
        except OSError:
            pass

        # safe move: delete source only after verified completion
        if job.get("delete_source") and src_sha:
            try:
                if job["src_base"] == "self":
                    self.verified_delete(src_path, src_sha,
                                         expected_mtime_ns=src_mtime_ns,
                                         expected_size=size)
                else:
                    self._client.request(
                        "POST", f"{src_base}/api/fs/verified-delete",
                        body={"path": src_path, "sha256": src_sha,
                              "mtime_ns": src_mtime_ns, "size": size},
                        timeout=120)
            except Exception as exc:
                job["move_error"] = (
                    f"Copy verified and finalized; source could not be "
                    f"removed ({exc}). Data is safe in BOTH places — remove "
                    f"the source manually after checking.")
                self._set_status(job, "completed_partial")
                return
        self._set_status(job, "completed")

    # ------------------------------------------------------------------
    def _source_meta(self, src_base: str, src_path: str) -> dict:
        """Legacy one-shot hash (kept for callers). Prefer `_await_source_hash`."""
        return self._await_source_hash(
            {"id": "meta", "prepare": None,
             "status": "running"},
            src_base=src_base, src_path=src_path, publish=False)

    def _update_prepare(self, job: dict, snap: dict, publish: bool = True) -> None:
        prep = {
            "phase": "source_checksum",
            "hash_id": snap.get("hash_id"),
            "status": snap.get("status"),
            "path": snap.get("path") or job.get("src_path"),
            "size": snap.get("size") or 0,
            "processed": snap.get("processed") or 0,
            "speed_bps": snap.get("speed_bps") or 0.0,
            "eta_s": snap.get("eta_s"),
            "elapsed_s": snap.get("elapsed_s") or 0.0,
            "cached": bool(snap.get("cached")),
            "error": snap.get("error"),
        }
        job["prepare"] = prep
        if snap.get("size"):
            job["size"] = snap["size"]
        if not publish or not job.get("id") or job["id"] == "meta":
            return
        # Cached digests skip the visible hashing phase.
        if snap.get("cached") and snap.get("status") == "completed":
            self._persist()
            self._publish(job, job.get("status") or "running")
            return
        if job.get("status") not in ("hashing", "cancelled", "failed"):
            self._set_status(job, "hashing")
        else:
            self._persist()
            self._publish(job, job.get("status") or "hashing")

    def _await_source_hash(self, job: dict, *, local_path: str = None,
                           src_base: str = None, src_path: str = None,
                           publish: bool = True) -> dict:
        """Resolve source size/mtime/sha256 without a single long HTTP hold.

        Local paths use HashPrepareService directly. Remote paths start or
        join a prepare job on the source companion and poll with short
        timeouts so ordinary PeerClient defaults stay unchanged.
        """
        cancel = self._cancel_events.get(job.get("id")) or threading.Event()

        def _raise_if_cancelled():
            if cancel.is_set():
                raise InterruptedError()

        if local_path is not None:
            snap = self.hash_prepare.prepare(local_path)
            self._update_prepare(job, snap, publish=publish)
            hash_id = snap.get("hash_id")
            while snap.get("status") in ("queued", "running"):
                _raise_if_cancelled()
                pause = self._pause_flags.get(job.get("id"))
                if pause is not None:
                    self._wait_while_paused(job, pause)
                time.sleep(HASH_POLL_IDLE_S)
                if hash_id:
                    snap = self.hash_prepare.status(hash_id)
                else:
                    snap = self.hash_prepare.prepare(local_path)
                    hash_id = snap.get("hash_id")
                self._update_prepare(job, snap, publish=publish)
            if snap.get("status") != "completed" or not snap.get("sha256"):
                err = snap.get("error") or snap.get("status") or "hash failed"
                if snap.get("status") == "cancelled":
                    raise InterruptedError()
                if "changed" in (err or "").lower():
                    raise FsError(409, "source_changed", err)
                raise FsError(500, "hash_failed",
                              f"Source checksum failed: {err}")
            return {"size": snap["size"], "sha256": snap["sha256"],
                    "mtime_ns": snap.get("mtime_ns"), "path": snap.get("path")}

        # Remote source companion
        assert src_base and src_path
        start = self._client.request(
            "POST", f"{src_base}/api/fs/hash/prepare",
            body={"path": src_path}, timeout=HASH_PREPARE_HTTP_TIMEOUT)
        self._update_prepare(job, start, publish=publish)
        hash_id = start.get("hash_id")
        snap = start
        while snap.get("status") in ("queued", "running"):
            _raise_if_cancelled()
            pause = self._pause_flags.get(job.get("id"))
            if pause is not None:
                self._wait_while_paused(job, pause)
            if not hash_id:
                raise FsError(500, "hash_failed",
                              "Source checksum returned no job id")
            time.sleep(HASH_POLL_IDLE_S)
            snap = self._client.request(
                "GET",
                f"{src_base}/api/fs/hash/prepare/{_urlquote(hash_id)}",
                timeout=HASH_POLL_HTTP_TIMEOUT)
            self._update_prepare(job, snap, publish=publish)
        if snap.get("status") != "completed" or not snap.get("sha256"):
            err = snap.get("error") or snap.get("status") or "hash failed"
            if snap.get("status") == "cancelled":
                raise InterruptedError()
            if "changed" in (err or "").lower():
                raise FsError(409, "source_changed", err)
            # Accurate failure — never pretend size=0 transfer started.
            raise FsError(500, "hash_failed",
                          f"Source checksum failed: {err}")
        return {"size": snap["size"], "sha256": snap["sha256"],
                "mtime_ns": snap.get("mtime_ns"), "path": snap.get("path")}

    def _stream_local(self, real_src: str, fh, offset: int, size: int,
                      job: dict, cancel: threading.Event,
                      pause: threading.Event) -> None:
        import hashlib
        digest = hashlib.sha256()
        started = time.monotonic()
        with open(real_src, "rb") as src:
            src.seek(offset)
            remaining = size - offset
            chunk = int(self.config.get("chunk_size") or CHUNK)
            while remaining > 0:
                if cancel.is_set():
                    raise InterruptedError()
                self._wait_while_paused(job, pause)
                data = src.read(min(chunk, remaining))
                if not data:
                    raise PeerUnreachable("Source file shrank during read")
                fh.write(data)
                digest.update(data)
                remaining -= len(data)
                job["transferred"] += len(data)
                self.limiter.acquire(len(data))
                self._update_speed(job, started)
                self._touch_progress(job)
        job["verify"]["actual"] = digest.hexdigest()

    def _stream_remote(self, src_base: str, src_path: str, fh, offset: int,
                       size: int, job: dict, cancel: threading.Event,
                       pause: threading.Event) -> None:
        import hashlib
        digest = hashlib.sha256()
        started = time.monotonic()
        remaining = size - offset
        chunk = int(self.config.get("chunk_size") or CHUNK)
        url = (f"{src_base}/api/fs/download?path={_urlquote(src_path)}")
        while remaining > 0:
            if cancel.is_set():
                raise InterruptedError()
            self._wait_while_paused(job, pause)
            want = min(chunk, remaining)
            headers = {"Range": f"bytes={offset}-{offset + want - 1}"}
            data = self._client.request("GET", url, timeout=60, raw=True,
                                        headers=headers)
            if not data:
                raise PeerUnreachable("Empty chunk from source")
            fh.write(data)
            digest.update(data)
            offset += len(data)
            remaining -= len(data)
            job["transferred"] += len(data)
            self.limiter.acquire(len(data))
            self._update_speed(job, started)
            self._touch_progress(job)
        job["verify"]["actual"] = digest.hexdigest()

    @staticmethod
    def _wait_while_paused(job: dict, pause: threading.Event) -> None:
        if pause.is_set():
            raise _PauseExit()

    def _update_speed(self, job: dict, started: float) -> None:
        now = time.monotonic()
        last_t = job.get("_speed_t", 0.0)
        last_b = job.get("_speed_b", 0)
        dt = now - last_t if last_t else (now - started)
        if dt >= 0.8:
            db = job["transferred"] - last_b
            inst = db / dt if dt > 0 else 0.0
            # exponential moving average smooths LAN jitter
            job["speed_bps"] = inst if not last_t else \
                0.6 * job["speed_bps"] + 0.4 * inst
            elapsed = now - started
            job["avg_bps"] = job["transferred"] / elapsed if elapsed > 0 else 0.0
            remaining = max(0, job["size"] - job["transferred"])
            job["eta_s"] = remaining / job["avg_bps"] if job["avg_bps"] > 1 else None
            job["_speed_t"] = now
            job["_speed_b"] = job["transferred"]

    @staticmethod
    def _check_dest_name(dest_path: str) -> None:
        """Explicit early failure for names the destination OS cannot
        represent (Windows reserved/invalid).  Never silent rename/skip."""
        if os.name != "nt":
            return
        problem = windows_name_problem(os.path.basename(dest_path))
        if problem:
            raise FsError(400, "invalid_dest_name",
                          f"Destination name cannot exist on Windows: "
                          f"'{os.path.basename(dest_path)}' — {problem}. "
                          f"Transfer refused; source preserved.")

    def _check_space(self, dest_path: str, size: int) -> None:
        parent = os.path.dirname(os.path.abspath(dest_path)) or "/"
        while not os.path.exists(parent):
            nparent = os.path.dirname(parent)
            if nparent == parent:
                break
            parent = nparent
        try:
            usage = shutil.disk_usage(parent)
        except OSError:
            return
        need = size + int(self.config.get("min_free_space_bytes") or 0)
        if usage.free < need:
            raise FsError(507, "insufficient_space",
                          f"Insufficient disk space at {parent}: need {need} bytes, "
                          f"have {usage.free} free")

    # ------------------------------------------------------------------
    # directory pulls
    # ------------------------------------------------------------------
    def _run_pull_dir(self, job: dict) -> None:
        cancel = self._cancel_events[job["id"]]
        pause = self._pause_flags[job["id"]]

        if job.get("manifest") is None:
            # enumerate the source tree page by page until next_offset is
            # null — large trees are fully covered, never truncated
            manifest = {"root": None, "files": [], "dirs": [],
                        "total_files": 0, "total_bytes": 0}
            offset = 0
            while True:
                if cancel.is_set():
                    self._set_status(job, "cancelled", error="Cancelled by user")
                    raise _TransferAbort()
                self._wait_while_paused(job, pause)
                if job["src_base"] == "self":
                    page = self.walk_remote_manifest(job["src_path"], skip=offset)
                else:
                    page = self._client.request(
                        "GET",
                        f"{job['src_base']}/api/fs/walk?path={_urlquote(job['src_path'])}"
                        f"&skip={offset}&limit=5000", timeout=120)
                if manifest["root"] is None:
                    manifest["root"] = page["root"]
                    manifest["dirs"] = page.get("dirs", [])
                manifest["files"].extend(page["files"])
                manifest["total_files"] = page["total_files"]
                manifest["total_bytes"] = page["total_bytes"]
                job["files_total"] = page["total_files"]
                job["size"] = page["total_bytes"]
                job["enumerate_progress"] = len(manifest["files"])
                self._touch_progress(job, force=True)
                self._persist()
                offset = page.get("next_offset")
                if offset is None:
                    break
            job["manifest"] = manifest

        manifest = job["manifest"]
        root = manifest["root"]
        index = job.get("file_index", 0)
        os.makedirs(self.sandbox.authorize(job["dest_path"], "w"), exist_ok=True)
        for d in manifest.get("dirs", []):
            os.makedirs(os.path.join(job["dest_path"], *d.split("/")),
                        exist_ok=True)

        while index < len(manifest["files"]):
            if cancel.is_set():
                self._set_status(job, "cancelled", error="Cancelled by user")
                raise _TransferAbort()
            entry = manifest["files"][index]
            rel = entry["rel"]
            dest_file = os.path.join(job["dest_path"], *rel.split("/"))
            sub = {
                "id": job["id"],
                "kind": "pull",
                "src_base": job["src_base"],
                "src_path": os.path.join(root, *rel.split("/")),
                "dest_path": dest_file,
                "conflict": job.get("conflict", "rename"),
                "size": entry["size"],
                "src_device": job.get("src_device"),
                "dest_device": job.get("dest_device"),
            }
            # execute inline (sequential per directory job) but reuse _run_pull
            self._run_pull_inline(sub, job, cancel, pause)
            index += 1
            job["file_index"] = index
            job["files_done"] = index
            if index % 50 == 0:
                self._persist()          # throttled: state save is coarse
        self._persist()
        self._set_status(job, "completed")

    def _run_pull_inline(self, sub: dict, parent_job: dict,
                         cancel: threading.Event, pause: threading.Event) -> None:
        """Execute one file of a directory job without touching the queue."""
        parent_job["current_file"] = sub["dest_path"]
        src_base, src_path = sub["src_base"], sub["src_path"]
        # Reuse the parent job's cancel/pause events and prepare progress.
        if src_base == "self":
            real_src = self.sandbox.authorize(src_path, "r")
            meta = self._await_source_hash(parent_job, local_path=src_path)
            size, src_sha = meta["size"], meta.get("sha256")
        else:
            meta = self._await_source_hash(
                parent_job, src_base=src_base, src_path=src_path)
            size, src_sha = meta["size"], meta.get("sha256")
            real_src = None
        if parent_job.get("status") == "hashing":
            self._set_status(parent_job, "running")
        parent_job["verify"]["expected"] = None   # per-file; verified inline
        dest_path = sub["dest_path"]
        os.makedirs(os.path.dirname(dest_path) or "/", exist_ok=True)
        self._check_dest_name(dest_path)
        verdict = self._resolve_conflict(sub, dest_path, size, src_sha or "")
        if verdict == "skip":
            parent_job["transferred"] += size
            parent_job["verify"]["status"] = "ok"
            return
        part_path = dest_path + ".part"
        offset = 0
        if os.path.exists(part_path):
            offset = os.path.getsize(part_path)
            if offset > size:
                offset = 0
                os.truncate(part_path, 0)
        self._check_space(dest_path, size - offset)
        fh = open(part_path, "ab")
        try:
            if src_base == "self":
                self._stream_local(real_src, fh, offset, size, parent_job,
                                   cancel, pause)
            else:
                self._stream_remote(src_base, src_path, fh, offset, size,
                                    parent_job, cancel, pause)
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fh.close()
        if cancel.is_set():
            raise InterruptedError()
        actual = self.file_hash(part_path)["sha256"]
        if src_sha and actual != src_sha:
            try:
                os.replace(part_path, part_path + ".corrupt")
            except OSError:
                pass
            if self.events:
                self.events.emit("verification_failed",
                                 f"Integrity verification failed: {dest_path}",
                                 severity="error", data={"job_id": parent_job["id"]})
            raise FsError(409, "verify_mismatch",
                          f"SHA-256 mismatch for {dest_path}")
        os.replace(part_path, dest_path)

    # ------------------------------------------------------------------
    def stats(self) -> dict:
        with self._lock:
            running = [j for j in self._jobs.values()
                       if j["status"] in ("running", "verifying")]
            total_bps = sum(j.get("speed_bps") or 0 for j in running)
        return {
            "active": len(running),
            "aggregate_speed_bps": round(total_bps, 1),
            "bandwidth_limit_bps": self.config.get("bandwidth_limit_bps", 0),
            "history_count": len(self._history),
        }


class _TransferAbort(Exception):
    """Internal: job finished (cancel path) without further handling."""


class _PauseExit(Exception):
    """Internal: worker exits so pause/resume can never race writers."""


def _urlquote(path: str) -> str:
    import urllib.parse
    return urllib.parse.quote(path, safe="")
