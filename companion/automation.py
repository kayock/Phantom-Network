"""Optional local automation: watched folders and scheduled copy jobs.

Jobs are ALWAYS explicitly configured by the operator (nothing is enabled
automatically) and always execute through the standard, verified transfer
engine — automation never bypasses filesystem permissions or authorized
directory boundaries, never mirrors deletions, and never destroys backup
data on its own.

Job kinds:

* ``watch``   — poll the source manifest every ``automation_poll_s``; when
                new or changed files appear (size+mtime differ), copy them.
* ``schedule`` — run a one-way copy/sync every ``interval_minutes``.

Every run records a history entry (capped); failures are reported honestly
and simply retried on the next poll.  ``sync`` mode copies differing files;
it does NOT delete files missing at the source (one-way, additive).
"""

from __future__ import annotations

import threading
import time
from typing import List, Optional

from .errors import FsError
from .util import new_id, now_ms

HISTORY_CAP = 20
MAX_FILES_PER_RUN = 2000


class AutomationEngine:
    def __init__(self, config, registry, log=None):
        self.config = config
        self.registry = registry
        self.log = log
        self._running: dict = {}        # job_id → ts of last run started
        self._lock = threading.RLock()
        self._stop = threading.Event()
        threading.Thread(target=self._loop, name="pn-automation",
                         daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # jobs CRUD
    # ------------------------------------------------------------------
    def list_jobs(self) -> List[dict]:
        jobs = []
        for j in (self.config.get("automation_jobs") or []):
            d = dict(j)
            d.pop("_snapshot", None)          # internal watch state
            d["running"] = d.get("id") in self._running
            jobs.append(d)
        return jobs

    def upsert_job(self, job: dict) -> dict:
        kind = job.get("kind")
        if kind not in ("watch", "schedule"):
            raise FsError(400, "bad_job", "kind must be watch|schedule")
        mode = job.get("mode", "copy")
        if mode not in ("copy", "sync"):
            raise FsError(400, "bad_job", "mode must be copy|sync")
        src_path = str(job.get("src_path") or "").strip()
        dst_path = str(job.get("dst_path") or "").strip()
        if not src_path or not dst_path:
            raise FsError(400, "bad_job", "src_path and dst_path are required")
        jobs = list(self.config.get("automation_jobs") or [])
        clean = {
            "id": job.get("id") or new_id("job"),
            "name": str(job.get("name") or f"{kind} {src_path}")[0:80],
            "kind": kind,
            "mode": mode,
            "enabled": bool(job.get("enabled", True)),
            "paused": bool(job.get("paused", False)),
            "src_device": str(job.get("src_device") or "")[:80],
            "dst_device": str(job.get("dst_device") or "")[:80],
            "src_path": src_path,
            "dst_path": dst_path,
            "compare": job.get("compare", "size"),
            "conflict": job.get("conflict", "replace"),
            "extensions": [e.lower().lstrip(".") for e in
                           (job.get("extensions") or [])][:24],
            "interval_minutes": max(1, _num(job.get("interval_minutes"), 60)),
            "created": job.get("created") or now_ms(),
            "last_run": job.get("last_run") or 0,
            "last_result": job.get("last_result") or "",
            "history": (job.get("history") or [])[-HISTORY_CAP:],
        }
        if clean["compare"] not in ("size", "mtime", "hash"):
            clean["compare"] = "size"
        for i, existing in enumerate(jobs):
            if existing.get("id") == clean["id"]:
                # preserve the internal watch snapshot across edits
                clean["_snapshot"] = existing.get("_snapshot")
                jobs[i] = clean
                break
        else:
            jobs.append(clean)
        self.config.set("automation_jobs", jobs)
        out = dict(clean)
        out.pop("_snapshot", None)
        return out

    def delete_job(self, job_id: str) -> dict:
        jobs = [j for j in (self.config.get("automation_jobs") or [])
                if j.get("id") != job_id]
        self.config.set("automation_jobs", jobs)
        return {"ok": True, "remaining": len(jobs)}

    def control(self, job_id: str, action: str, api) -> dict:
        """pause | resume | run | disable | enable."""
        job = self._get(job_id)
        if job is None:
            raise FsError(404, "no_job", "Unknown automation job")
        if action == "pause":
            job["paused"] = True
        elif action == "resume":
            job["paused"] = False
        elif action == "enable":
            job["enabled"] = True
        elif action == "disable":
            job["enabled"] = False
        elif action == "run":
            threading.Thread(target=self._run_job, args=(job, api),
                             name=f"pn-auto-{job_id[:8]}",
                             daemon=True).start()
            return {"ok": True, "started": True}
        else:
            raise FsError(400, "bad_action",
                          "action must be pause|resume|run|enable|disable")
        self._save_job(job)
        return {"ok": True, "job": {k: v for k, v in job.items()
                                    if k != "_snapshot"}}

    def preview(self, spec: dict, api) -> dict:
        """Reuse the interactive sync planner for an honest dry-run."""
        spec = dict(spec)
        spec.setdefault("compare", "size")
        # resolve configured device ids to bases exactly like the executor
        def base_for(device):
            if not device:
                return ""
            peer = self.registry.get(device)
            if peer and peer.status == "online" and peer.addresses:
                return f"http://{peer.addresses[0]}:{peer.api_port}"
            raise FsError(502, "peer_offline",
                          f"Device {device} is offline — cannot preview")
        if spec.get("src_device") and not spec.get("src_base"):
            spec["src_base"] = base_for(spec["src_device"])
        if spec.get("dst_device") and not spec.get("dst_base"):
            spec["dst_base"] = base_for(spec["dst_device"])
        return api._sync_plan(spec)

    # ------------------------------------------------------------------
    # scheduling loop
    # ------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.wait(max(5, _num(
                self.config.get("automation_poll_s"), 60))):
            api = self._api
            if api is None:
                continue
            for job in (self.config.get("automation_jobs") or []):
                if self._stop.is_set():
                    return
                if not job.get("enabled") or job.get("paused"):
                    continue
                if job.get("id") in self._running:
                    continue
                now = time.time()
                if job["kind"] == "schedule":
                    if now - (job.get("last_run") or 0) < \
                            job.get("interval_minutes", 60) * 60:
                        continue
                elif job["kind"] == "watch":
                    changed = self._watch_changes(job)
                    if not changed:
                        continue
                threading.Thread(target=self._run_job, args=(dict(job), api),
                                 name=f"pn-auto-{job['id'][:8]}",
                                 daemon=True).start()

    _api = None          # set by App after wiring (see app.py)

    def attach_api(self, api) -> None:
        self._api = api

    # ------------------------------------------------------------------
    def _watch_changes(self, job: dict) -> bool:
        """Compare the source manifest against the stored snapshot."""
        api = self._api
        if api is None:
            return False
        try:
            if job.get("src_device"):
                peer = self.registry.get(job["src_device"])
                if not (peer and peer.status == "online" and peer.addresses):
                    return False
            walk = api.transfers.walk_remote_manifest(job["src_path"])
            files = {(f["rel"], f["size"], f["mtime"])
                     for f in walk.get("files", [])}
        except Exception:
            return False
        prev = job.get("_snapshot") or []
        prev_set = {(r, s, m) for r, s, m in prev}
        changed = bool(files - prev_set) or bool(prev_set - files)
        if changed:
            job["_snapshot"] = [list(t) for t in sorted(files)][:MAX_FILES_PER_RUN]
            self._save_job(job)
        return changed

    def _run_job(self, job: dict, api) -> None:
        jid = job["id"]
        with self._lock:
            if jid in self._running:
                return
            self._running[jid] = now_ms()
        try:
            self._execute(job, api)
        except Exception as exc:
            self._record(job, ok=False, copied=0, bytes_=0, skipped=0,
                         error=str(exc))
        finally:
            with self._lock:
                self._running.pop(jid, None)

    def _execute(self, job: dict, api) -> None:
        def base_for(device: str) -> str:
            if not device:
                return ""
            peer = self.registry.get(device)
            if not (peer and peer.status == "online" and peer.addresses):
                raise FsError(502, "peer_offline",
                              f"Device {device} is offline for automation "
                              f"job '{job.get('name')}'")
            return f"http://{peer.addresses[0]}:{peer.api_port}"

        src_base = base_for(job.get("src_device"))
        dst_base = base_for(job.get("dst_device"))
        spec = {
            "src_base": src_base, "dst_base": dst_base,
            "src_path": job["src_path"], "dst_path": job["dst_path"],
            "compare": job.get("compare", "size"),
            "src_device": job.get("src_device") or None,
            "dst_device": job.get("dst_device") or None,
        }
        plan = api._sync_plan(spec)
        entries = plan.get("copy", [])
        if job.get("extensions"):
            exts = set(job["extensions"])
            entries = [e for e in entries
                       if e["rel"].rsplit(".", 1)[-1].lower() in exts]
        copied = skipped = 0
        total_bytes = 0
        errors = []
        for entry in entries[:MAX_FILES_PER_RUN]:
            job_spec = {
                "kind": "pull",
                "src_base": src_base or "self",
                "src_path": entry["src"],
                "dest_path": entry["dest"],
                "conflict": job.get("conflict", "replace"),
                "src_device": job.get("src_device") or "self",
                "dest_device": job.get("dst_device") or "self",
            }
            try:
                if dst_base:
                    # destination is a remote companion: the pull job is
                    # created THERE (destination-pull architecture preserved)
                    from .peers import PeerClient
                    client = PeerClient(self._token_provider)
                    client.request("POST", f"{dst_base}/api/jobs",
                                   body=job_spec, timeout=10)
                else:
                    api.transfers.create_job(job_spec)
                copied += 1
                total_bytes += entry.get("size") or 0
            except Exception as exc:
                errors.append(f"{entry['rel']}: {exc}")
        skipped = len(plan.get("skip", []))
        ok = not errors
        self._record(job, ok=ok, copied=copied, bytes_=total_bytes,
                     skipped=skipped,
                     error="; ".join(errors[:3]) if errors else "")

    def _token_provider(self) -> str:
        if self.config.get("require_token"):
            return self.config.get("auth_token", "")
        return ""

    def _record(self, job: dict, ok: bool, copied: int, bytes_: int,
                skipped: int, error: str) -> None:
        job = self._get(job["id"]) or job
        entry = {
            "ts": now_ms(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "ok": ok, "copied": copied, "bytes": bytes_, "skipped": skipped,
            "error": error[:300],
        }
        history = (job.get("history") or [])[-(HISTORY_CAP - 1):] + [entry]
        job["history"] = history
        job["last_run"] = entry["ts"]
        job["last_result"] = ("OK" if ok else "error") + \
            f" · copied {copied}, skipped {skipped}" + \
            (f" · {error[:120]}" if error else "")
        self._save_job(job)
        if self.log:
            self.log.info("automation job '%s' %s", job.get("name"),
                          job.get("last_result", entry["iso"]))

    def _get(self, job_id: str) -> Optional[dict]:
        for j in (self.config.get("automation_jobs") or []):
            if j.get("id") == job_id:
                return j
        return None

    def _save_job(self, job: dict) -> None:
        jobs = list(self.config.get("automation_jobs") or [])
        for i, j in enumerate(jobs):
            if j.get("id") == job["id"]:
                jobs[i] = job
                break
        self.config.set("automation_jobs", jobs)


def _num(value, default) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)
