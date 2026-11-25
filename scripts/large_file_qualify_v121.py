#!/usr/bin/env python3
"""Large-file LAN qualification into Portable's 1 TB Phantom transfer share."""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

STATION = "http://127.0.0.1:8415"
PORTABLE = "http://10.0.0.45:8415"
STATION_LAB = Path.home() / "phantom-network-lab" / "large-src"
PORTABLE_XFER = "/run/media/kayock/KAYOCK-1TB/Phantom-Network-Transfers"
SSH = [
    "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
    "-o", "ClearAllForwardings=yes", "kayock@10.0.0.45",
]
RESULTS = []
SUMMARY = {}
# Keep headroom on Station root and the 1 TB volume.
MIN_FREE_STATION = 25 * 1024 ** 3
MIN_FREE_1TB = 40 * 1024 ** 3
SIZES = [
    ("100MB", 100 * 1024 ** 2),
    ("1GB", 1 * 1024 ** 3),
    ("5GB", 5 * 1024 ** 3),
    ("10GB", 10 * 1024 ** 3),
    ("25GB", 25 * 1024 ** 3),
    ("50GB", 50 * 1024 ** 3),
]


def http(base, method, path, body=None, timeout=300):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        base + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "{}")


_PORTABLE_RUNNER = r'''
import json, sys, urllib.request
d = json.load(open(sys.argv[1], encoding="utf-8"))
body = d.get("body")
data = json.dumps(body).encode() if body is not None else None
headers = {"Content-Type": "application/json"} if body is not None else {}
req = urllib.request.Request(
    "http://127.0.0.1:8415" + d["path"],
    data=data, headers=headers, method=d["method"])
with urllib.request.urlopen(req, timeout=d.get("timeout", 300)) as resp:
    sys.stdout.write(resp.read().decode() or "{}")
'''


def portable_local(method, path, body=None, timeout=300):
    payload = {"method": method, "path": path, "body": body, "timeout": timeout}
    with tempfile.TemporaryDirectory() as td:
        req_path = Path(td) / "req.json"
        run_path = Path(td) / "run.py"
        req_path.write_text(json.dumps(payload), encoding="utf-8")
        run_path.write_text(_PORTABLE_RUNNER, encoding="utf-8")
        rid = f"{os.getpid()}-{int(time.time()*1000)}"
        remote_req = f"/tmp/pn-lf-{rid}.json"
        remote_run = f"/tmp/pn-lf-run-{rid}.py"
        scp = ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
               "-o", "ClearAllForwardings=yes"]
        subprocess.check_call(
            scp + [str(req_path), f"kayock@10.0.0.45:{remote_req}"],
            stdout=subprocess.DEVNULL)
        subprocess.check_call(
            scp + [str(run_path), f"kayock@10.0.0.45:{remote_run}"],
            stdout=subprocess.DEVNULL)
        try:
            proc = subprocess.run(
                SSH + ["python3", remote_run, remote_req],
                text=True, timeout=timeout + 60,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
            return json.loads(proc.stdout.strip() or "{}")
        finally:
            subprocess.call(
                SSH + [f"rm -f {remote_req} {remote_run}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def free_bytes(path_on_host: str, host="station") -> int:
    import shutil
    if host == "station":
        return shutil.disk_usage(path_on_host).free
    # Avoid nested-quote breakage over SSH: ask df for available KiB.
    out = subprocess.check_output(
        SSH + ["df", "-Pk", path_on_host], text=True)
    # Filesystem 1024-blocks Used Available Capacity Mounted on
    line = out.strip().splitlines()[-1]
    avail_kib = int(line.split()[3])
    return avail_kib * 1024


def write_and_hash(path: Path, size: int, seed: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    block = (seed * (1024 * 1024 // max(len(seed), 1) + 1))[:1024 * 1024]
    left = size
    with open(path, "wb") as fh:
        while left > 0:
            n = min(left, len(block))
            chunk = block[:n]
            fh.write(chunk)
            h.update(chunk)
            left -= n
    return h.hexdigest()


def portable_sha256(path: str) -> str:
    out = subprocess.check_output(
        SSH + [f"sha256sum -- {shlex.quote(path)}"], text=True)
    return out.split()[0]


def wait_job(job_id: str, timeout: int, poll_mem=True):
    deadline = time.time() + timeout
    peak_s = peak_p = 0
    last = None
    while time.time() < deadline:
        last = http(PORTABLE, "GET", f"/api/jobs/{job_id}")
        job = last.get("job") or {}
        if poll_mem:
            try:
                peak_s = max(peak_s, (http(STATION, "GET", "/api/stats")
                                      .get("process") or {}).get("rss_bytes") or 0)
                peak_p = max(peak_p, (http(PORTABLE, "GET", "/api/stats")
                                      .get("process") or {}).get("rss_bytes") or 0)
            except Exception:
                pass
        if job.get("status") in (
                "completed", "completed_partial", "failed", "cancelled"):
            return job, peak_s, peak_p
        time.sleep(1.0)
    raise TimeoutError(f"job {job_id} stuck: {last}")


def record(entry: dict):
    RESULTS.append(entry)
    print(json.dumps(entry, default=str), flush=True)


def transfer_one(label: str, size: int, resume: bool = False):
    free_s = free_bytes(str(Path.home()), "station")
    free_p = free_bytes(PORTABLE_XFER, "portable")
    need = size + 2 * 1024 ** 3  # generation + transfer headroom
    if free_s < need + MIN_FREE_STATION:
        return {"label": label, "status": "skipped",
                "reason": f"station free {free_s} insufficient for {size}"}
    if free_p < size + MIN_FREE_1TB:
        return {"label": label, "status": "skipped",
                "reason": f"1TB free {free_p} insufficient for {size}"}

    src = STATION_LAB / f"pn-lf-{label}.bin"
    dest = f"{PORTABLE_XFER}/pn-lf-{label}.bin"
    print(f"=== generating {label} ({size} bytes) ===", flush=True)
    if src.exists():
        src.unlink()
    src_sha = write_and_hash(src, size, f"PN-{label}-".encode())
    assert src.stat().st_size == size

    print(f"=== transferring {label} Station → Portable 1TB ===", flush=True)
    t0 = time.time()
    job = portable_local("POST", "/api/jobs", {
        "kind": "pull",
        "src_base": "http://10.0.0.202:8415",
        "src_path": str(src),
        "dest_path": dest,
        "conflict": "replace",
    }, timeout=60)["job"]

    paused = False
    if resume:
        # Wait until some bytes move, then pause/resume.
        for _ in range(120):
            cur = http(PORTABLE, "GET", f"/api/jobs/{job['id']}")["job"]
            if (cur.get("transferred") or 0) > max(8 * 1024 * 1024, size // 50):
                portable_local("POST", f"/api/jobs/{job['id']}/control",
                               {"action": "pause"})
                paused = True
                time.sleep(3)
                portable_local("POST", f"/api/jobs/{job['id']}/control",
                               {"action": "resume"})
                break
            if cur.get("status") in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.5)

    timeout = max(600, int(size / (2 * 1024 * 1024)) + 300)
    done, peak_s, peak_p = wait_job(job["id"], timeout=timeout)
    dur = time.time() - t0
    dest_sha = portable_sha256(dest) if done.get("status") == "completed" else None
    entry = {
        "label": label,
        "direction": "Station→Portable-1TB",
        "src": str(src),
        "dest": dest,
        "size": size,
        "src_sha256": src_sha,
        "dest_sha256": dest_sha,
        "match": dest_sha == src_sha,
        "status": done.get("status"),
        "verify": done.get("verify"),
        "duration_s": round(dur, 3),
        "avg_bps": done.get("avg_bps"),
        "throughput_MBps": round((size / (1024 ** 2)) / max(dur, 0.001), 3),
        "peak_rss_station": peak_s,
        "peak_rss_portable": peak_p,
        "pause_resume": paused if resume else None,
        "free_station_before": free_s,
        "free_1tb_before": free_p,
    }
    # Delete source after success to free Station space for next size.
    if entry["match"] and entry["status"] == "completed":
        src.unlink(missing_ok=True)
    return entry


def main():
    STATION_LAB.mkdir(parents=True, exist_ok=True)
    SUMMARY["health_station"] = http(STATION, "GET", "/api/health")
    SUMMARY["health_portable"] = http(PORTABLE, "GET", "/api/health")
    assert SUMMARY["health_station"]["version"] == "1.2.1"
    assert SUMMARY["health_portable"]["version"] == "1.2.1"

    # Browse / authorization checks
    listing = http(PORTABLE, "GET",
                   f"/api/fs/list?path={urllib.parse.quote(PORTABLE_XFER, safe='')}")
    SUMMARY["browse_xfer_dir"] = {
        "ok": listing.get("ok"),
        "entries": len(listing.get("entries") or []),
    }
    outside_ok = False
    try:
        http(PORTABLE, "GET",
             f"/api/fs/list?path={urllib.parse.quote('/run/media/kayock/KAYOCK-1TB', safe='')}")
        outside_ok = True
    except Exception as exc:
        SUMMARY["outside_share_blocked"] = str(exc)
    else:
        SUMMARY["outside_share_blocked"] = "UNEXPECTED_ALLOWED" if outside_ok else "blocked"

    # Small round-trip sanity into 1TB share before big files
    tiny = STATION_LAB / "pn-lf-sanity.txt"
    tiny.write_text("1tb-share-ok\n", encoding="utf-8")
    dest_tiny = f"{PORTABLE_XFER}/pn-lf-sanity.txt"
    job = portable_local("POST", "/api/jobs", {
        "kind": "pull", "src_base": "http://10.0.0.202:8415",
        "src_path": str(tiny), "dest_path": dest_tiny, "conflict": "replace",
    })["job"]
    done, _, _ = wait_job(job["id"], timeout=60, poll_mem=False)
    SUMMARY["sanity_copy"] = {
        "status": done.get("status"),
        "verify": done.get("verify"),
        "dest_sha": portable_sha256(dest_tiny),
        "src_sha": hashlib.sha256(tiny.read_bytes()).hexdigest(),
    }
    tiny.unlink(missing_ok=True)

    max_ok = 0
    resume_done = False
    for label, size in SIZES:
        # Interrupt/resume on the 5GB transfer (large enough to pause mid-flight)
        do_resume = (label == "5GB")
        try:
            entry = transfer_one(label, size, resume=do_resume)
        except Exception as exc:
            entry = {"label": label, "size": size, "status": "error",
                     "error": str(exc)}
        record(entry)
        if entry.get("match") and entry.get("status") == "completed":
            max_ok = size
            if do_resume:
                resume_done = bool(entry.get("pause_resume"))
        else:
            # Stop escalating if a size fails or is skipped for space
            if entry.get("status") in ("failed", "error"):
                break
            if entry.get("status") == "skipped":
                break

    # Safe disposable move on Station lab (verified delete after copy)
    move_src = Path.home() / "phantom-network-lab" / "outbox" / "safe-move-lf.bin"
    move_src.parent.mkdir(parents=True, exist_ok=True)
    move_sha = write_and_hash(move_src, 8 * 1024 * 1024, b"SAFEMOVE")
    move_dest = Path.home() / "phantom-network-lab" / "inbox" / "safe-move-lf.bin"
    job = http(STATION, "POST", "/api/jobs", {
        "kind": "pull", "src_base": "self", "src_path": str(move_src),
        "dest_path": str(move_dest), "conflict": "replace",
        "delete_source": True,
    })["job"]
    # wait on station
    deadline = time.time() + 120
    move_done = None
    while time.time() < deadline:
        move_done = http(STATION, "GET", f"/api/jobs/{job['id']}")["job"]
        if move_done.get("status") in (
                "completed", "completed_partial", "failed", "cancelled"):
            break
        time.sleep(0.5)
    SUMMARY["safe_move"] = {
        "status": (move_done or {}).get("status"),
        "source_removed": not move_src.exists(),
        "dest_exists": move_dest.exists(),
        "dest_sha_match": (
            move_dest.exists() and hashlib.sha256(move_dest.read_bytes()).hexdigest() == move_sha),
        "verify": (move_done or {}).get("verify"),
    }
    if move_dest.exists():
        move_dest.unlink()

    # Cleanup disposable large artifacts on Portable 1TB (keep directory)
    print("=== cleaning disposable large-file artifacts on 1TB ===", flush=True)
    subprocess.check_call(
        SSH + [f"rm -f {shlex.quote(PORTABLE_XFER)}/pn-lf-*.bin "
               f"{shlex.quote(PORTABLE_XFER)}/pn-lf-sanity.txt"])
    # Cleanup any leftover station sources
    for p in STATION_LAB.glob("pn-lf-*.bin"):
        p.unlink(missing_ok=True)

    SUMMARY["transfers"] = RESULTS
    SUMMARY["max_verified_bytes"] = max_ok
    SUMMARY["interrupt_resume_tested"] = resume_done
    SUMMARY["free_station_after"] = free_bytes(str(Path.home()), "station")
    SUMMARY["free_1tb_after"] = free_bytes(PORTABLE_XFER, "portable")

    out = Path.home() / "phantom-network-lab" / "large-file-qualify-v121.json"
    out.write_text(json.dumps(SUMMARY, indent=2), encoding="utf-8")
    print("WROTE", out, flush=True)
    if max_ok < 100 * 1024 ** 2:
        return 1
    print("LARGE_QUALIFY_OK", f"max={max_ok}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("FATAL", exc, file=sys.stderr)
        Path.home().joinpath(
            "phantom-network-lab/large-file-qualify-v121.json").write_text(
            json.dumps({"fatal": str(exc), "results": RESULTS, **SUMMARY}, indent=2))
        raise
