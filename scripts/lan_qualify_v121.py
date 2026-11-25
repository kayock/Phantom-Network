#!/usr/bin/env python3
"""Physical LAN qualification for Phantom Network v1.2.1 (Station ↔ Portable).

Disposable files only. Portable admin endpoints are local-only; those calls
go through SSH to 127.0.0.1 on Portable.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

STATION = "http://127.0.0.1:8415"
PORTABLE_LAN = "http://<portable-host>:8415"
STATION_LAB = Path.home() / "phantom-network-lab"
PORTABLE_LAB = "<phantom-network-lab>"
SSH = [
    "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
    "-o", "ClearAllForwardings=yes", "<user>@<portable-host>",
]
RESULTS = {}


def record(key, value):
    RESULTS[key] = value
    print(f"[{key}] {json.dumps(value, default=str)[:500]}")


def http(base, method, path, body=None, timeout=120):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(
        base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"{method} {base}{path} -> {exc.code}: {detail}") from exc


_PORTABLE_RUNNER = r'''
import json, sys, urllib.request
d = json.load(open(sys.argv[1], encoding="utf-8"))
body = d.get("body")
data = json.dumps(body).encode() if body is not None else None
headers = {"Content-Type": "application/json"} if body is not None else {}
req = urllib.request.Request(
    "http://127.0.0.1:8415" + d["path"],
    data=data, headers=headers, method=d["method"])
with urllib.request.urlopen(req, timeout=d.get("timeout", 120)) as resp:
    sys.stdout.write(resp.read().decode() or "{}")
'''


def portable_local(method, path, body=None, timeout=120):
    """Run an HTTP call against Portable's loopback (bypasses admin_local_only)."""
    payload = {
        "method": method,
        "path": path,
        "body": body,
        "timeout": timeout,
    }
    with tempfile.TemporaryDirectory() as td:
        req_path = Path(td) / "req.json"
        run_path = Path(td) / "run.py"
        req_path.write_text(json.dumps(payload), encoding="utf-8")
        run_path.write_text(_PORTABLE_RUNNER, encoding="utf-8")
        remote_req = f"/tmp/pn-qualify-{os.getpid()}-{int(time.time()*1000)}.json"
        remote_run = f"/tmp/pn-qualify-run-{os.getpid()}.py"
        scp = ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
               "-o", "ClearAllForwardings=yes"]
        subprocess.check_call(
            scp + [str(req_path), f"<user>@<portable-host>:{remote_req}"],
            stdout=subprocess.DEVNULL)
        subprocess.check_call(
            scp + [str(run_path), f"<user>@<portable-host>:{remote_run}"],
            stdout=subprocess.DEVNULL)
        try:
            proc = subprocess.run(
                SSH + ["python3", remote_run, remote_req],
                text=True, timeout=timeout + 40,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"portable_local {method} {path} failed: "
                    f"{proc.stderr.strip() or proc.stdout.strip()}")
            return json.loads(proc.stdout.strip() or "{}")
        finally:
            subprocess.call(
                SSH + [f"rm -f {remote_req} {remote_run}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_job(base, job_id, timeout=180, via_portable_local=False):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        if via_portable_local:
            last = portable_local("GET", f"/api/jobs/{job_id}")
        else:
            last = http(base, "GET", f"/api/jobs/{job_id}")
        job = last.get("job") or {}
        if job.get("status") in (
                "completed", "completed_partial", "failed", "cancelled"):
            return job
        time.sleep(0.4)
    raise TimeoutError(f"job {job_id} stuck: {last}")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_file(path: Path, size: int, seed: bytes = b"PN"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        left = size
        block = (seed * (65536 // len(seed) + 1))[:65536]
        while left > 0:
            n = min(left, len(block))
            fh.write(block[:n])
            left -= n
    return sha256_file(path)


def portable_sha256(path: str) -> str:
    out = subprocess.check_output(
        SSH + [f"sha256sum -- {shlex.quote(path)}"], text=True)
    return out.split()[0]


def portable_write(path: str, size: int, seed: str = "PORT"):
    cmd = (
        "python3 -c "
        + json.dumps(
            "import pathlib; p=pathlib.Path(%r); p.parent.mkdir(parents=True, exist_ok=True); "
            "seed=(%r).encode(); block=(seed*(65536//len(seed)+1))[:65536]; "
            "left=%d; f=open(p,'wb');\n"
            "import sys\n"
            "while left>0:\n"
            " n=min(left,len(block)); f.write(block[:n]); left-=n\n"
            "f.close()"
            % (path, seed, size)
        )
    )
    # simpler
    script = (
        f"import pathlib\n"
        f"p=pathlib.Path({path!r})\n"
        f"p.parent.mkdir(parents=True, exist_ok=True)\n"
        f"seed=b{seed!r}\n"
        f"block=(seed*(65536//len(seed)+1))[:65536]\n"
        f"left={size}\n"
        f"f=open(p,'wb')\n"
        f"while left>0:\n"
        f" n=min(left,len(block)); f.write(block[:n]); left-=n\n"
        f"f.close()\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(script)
        local = fh.name
    remote = f"/tmp/pn-write-{os.getpid()}.py"
    try:
        subprocess.check_call(
            ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
             "-o", "ClearAllForwardings=yes", local, f"<user>@<portable-host>:{remote}"],
            stdout=subprocess.DEVNULL)
        subprocess.check_call(SSH + ["python3", remote],
                              stdout=subprocess.DEVNULL)
    finally:
        os.unlink(local)
        subprocess.call(SSH + [f"rm -f {remote}"], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)


def q(path: str) -> str:
    return urllib.parse.quote(path, safe="")


def main():
    STATION_LAB.mkdir(parents=True, exist_ok=True)
    (STATION_LAB / "inbox").mkdir(exist_ok=True)
    (STATION_LAB / "outbox").mkdir(exist_ok=True)
    (STATION_LAB / "ro-src").mkdir(exist_ok=True)

    sh = http(STATION, "GET", "/api/health")
    ph = http(PORTABLE_LAN, "GET", "/api/health")
    record("station_health", sh)
    record("portable_health", ph)
    assert sh.get("version") == "1.2.1", sh
    assert ph.get("version") == "1.2.1", ph

    si = http(STATION, "GET", "/api/identity")
    pi = http(PORTABLE_LAN, "GET", "/api/identity")
    station_id = si["device_id"]
    portable_id = pi["device_id"]
    record("station_identity", {
        "name": si.get("name"), "host": si.get("host"),
        "device_id": station_id, "addresses": si.get("addresses"),
        "shares_count": si.get("shares_count"),
        "audio_enabled": si.get("audio_enabled"),
    })
    record("portable_identity", {
        "name": pi.get("name"), "host": pi.get("host"),
        "device_id": portable_id, "addresses": pi.get("addresses"),
        "shares_count": pi.get("shares_count"),
        "audio_enabled": pi.get("audio_enabled"),
    })

    http(STATION, "POST", "/api/fs/share", {
        "path": str(STATION_LAB), "mode": "rw", "label": "disposable-lab"})
    http(STATION, "POST", "/api/fs/share", {
        "path": str(STATION_LAB / "ro-src"), "mode": "ro",
        "label": "disposable-ro"})
    portable_local("POST", "/api/fs/share", {
        "path": PORTABLE_LAB, "mode": "rw", "label": "disposable-lab"})
    subprocess.check_call(
        SSH + ["mkdir", "-p", f"{PORTABLE_LAB}/inbox", f"{PORTABLE_LAB}/outbox"])

    time.sleep(3)
    peers_s = http(STATION, "GET", "/api/peers")["peers"]
    peers_p = http(PORTABLE_LAN, "GET", "/api/peers")["peers"]
    portable_peer = next(
        (p for p in peers_s if p.get("device_id") == portable_id), None)
    station_peer = next(
        (p for p in peers_p if p.get("device_id") == station_id), None)
    if portable_peer is None or portable_peer.get("status") != "online":
        http(STATION, "POST", "/api/peers/register",
             {"address": "<portable-host>", "port": 8415})
        time.sleep(2)
        peers_s = http(STATION, "GET", "/api/peers")["peers"]
        portable_peer = next(
            (p for p in peers_s if p.get("device_id") == portable_id), None)
    if station_peer is None or station_peer.get("status") != "online":
        portable_local("POST", "/api/peers/register",
                       {"address": "<station-host>", "port": 8415})
        time.sleep(2)
        peers_p = http(PORTABLE_LAN, "GET", "/api/peers")["peers"]
        station_peer = next(
            (p for p in peers_p if p.get("device_id") == station_id), None)
    record("station_sees_portable", {
        "found": bool(portable_peer),
        "status": (portable_peer or {}).get("status"),
        "host": (portable_peer or {}).get("host"),
        "addresses": (portable_peer or {}).get("addresses"),
        "name": (portable_peer or {}).get("name"),
    })
    record("portable_sees_station", {
        "found": bool(station_peer),
        "status": (station_peer or {}).get("status"),
        "host": (station_peer or {}).get("host"),
        "addresses": (station_peer or {}).get("addresses"),
        "name": (station_peer or {}).get("name"),
    })
    assert portable_peer and portable_peer.get("status") == "online"
    assert station_peer and station_peer.get("status") == "online"

    m1 = http(STATION, "POST", "/api/chat/send", {
        "to_kind": "direct", "to_id": portable_id,
        "body": "station-to-portable hello"})["message"]
    time.sleep(1.2)
    m2 = portable_local("POST", "/api/chat/send", {
        "to_kind": "direct", "to_id": station_id,
        "body": "portable-to-station hello"})["message"]
    time.sleep(1.5)
    hist_s = http(STATION, "GET",
                  f"/api/chat/history?conv=direct:{portable_id}&limit=20")
    hist_p = http(PORTABLE_LAN, "GET",
                  f"/api/chat/history?conv=direct:{station_id}&limit=20")
    bodies_s = [m.get("body") for m in hist_s.get("messages", [])]
    bodies_p = [m.get("body") for m in hist_p.get("messages", [])]
    record("ghostchat", {
        "station_msg_status": m1.get("status"),
        "portable_msg_status": m2.get("status"),
        "station_has_both": (
            "station-to-portable hello" in bodies_s
            and "portable-to-station hello" in bodies_s),
        "portable_has_both": (
            "station-to-portable hello" in bodies_p
            and "portable-to-station hello" in bodies_p),
    })

    unicode_name = "你好 world file.txt"
    space_name = "spaces and punctuation (1).txt"
    src_small = STATION_LAB / "outbox" / unicode_name
    write_file(src_small, 4096, b"UNI")
    write_file(STATION_LAB / "outbox" / space_name, 2048, b"SPC")
    nested = STATION_LAB / "outbox" / "nested-dir"
    nested.mkdir(exist_ok=True)
    write_file(nested / "child.bin", 1024, b"CH")
    try:
        http(STATION, "POST", "/api/fs/mkdir",
             {"path": str(STATION_LAB / "outbox"), "name": "nested-dir"})
    except RuntimeError:
        pass
    listing = http(STATION, "GET",
                   f"/api/fs/list?path={q(str(STATION_LAB / 'outbox'))}")
    names = {e["name"] for e in listing.get("entries", [])}
    record("file_commander_list", {
        "unicode": unicode_name in names,
        "spaces": space_name in names,
        "nested_dir": "nested-dir" in names,
    })
    http(STATION, "POST", "/api/fs/rename", {
        "path": str(STATION_LAB / "outbox" / space_name),
        "new_name": "renamed-spaces.txt"})
    record("file_commander_rename",
           (STATION_LAB / "outbox" / "renamed-spaces.txt").exists())

    # Station → Portable (destination pull on Portable)
    dest_path = f"{PORTABLE_LAB}/inbox/{unicode_name}"
    t0 = time.time()
    job = portable_local("POST", "/api/jobs", {
        "kind": "pull",
        "src_base": "http://<station-host>:8415",
        "src_path": str(src_small),
        "dest_path": dest_path,
        "conflict": "replace",
    })["job"]
    done = wait_job(PORTABLE_LAN, job["id"], timeout=60)
    src_hash = sha256_file(src_small)
    dest_hash = portable_sha256(dest_path)
    remote_stat = http(PORTABLE_LAN, "GET", f"/api/fs/stat?path={q(dest_path)}")
    record("transfer_station_to_portable", {
        "status": done.get("status"),
        "verify": done.get("verify"),
        "src_size": src_small.stat().st_size,
        "dest_size": remote_stat.get("size"),
        "src_sha256": src_hash,
        "dest_sha256": dest_hash,
        "match": src_hash == dest_hash,
        "duration_s": round(time.time() - t0, 3),
        "avg_bps": done.get("avg_bps"),
    })

    # Portable → Station
    p_src = f"{PORTABLE_LAB}/outbox/from-portable.bin"
    portable_write(p_src, 16384, "PORT")
    p_dest = STATION_LAB / "inbox" / "from-portable.bin"
    t0 = time.time()
    job = http(STATION, "POST", "/api/jobs", {
        "kind": "pull",
        "src_base": "http://<portable-host>:8415",
        "src_path": p_src,
        "dest_path": str(p_dest),
        "conflict": "replace",
    })["job"]
    done = wait_job(STATION, job["id"], timeout=60)
    remote_src_hash = portable_sha256(p_src)
    local_hash = sha256_file(p_dest)
    record("transfer_portable_to_station", {
        "status": done.get("status"),
        "verify": done.get("verify"),
        "src_sha256": remote_src_hash,
        "dest_sha256": local_hash,
        "match": remote_src_hash == local_hash,
        "duration_s": round(time.time() - t0, 3),
        "avg_bps": done.get("avg_bps"),
    })

    # 25 MB with pause/resume
    big = STATION_LAB / "outbox" / "qualify-25mb.bin"
    big_hash = write_file(big, 25 * 1024 * 1024, b"BIG25")
    t0 = time.time()
    job = portable_local("POST", "/api/jobs", {
        "kind": "pull",
        "src_base": "http://<station-host>:8415",
        "src_path": str(big),
        "dest_path": f"{PORTABLE_LAB}/inbox/qualify-25mb.bin",
        "conflict": "replace",
    })["job"]
    time.sleep(0.25)
    pause_ok = True
    try:
        portable_local("POST", f"/api/jobs/{job['id']}/control",
                       {"action": "pause"})
        time.sleep(1.0)
        portable_local("POST", f"/api/jobs/{job['id']}/control",
                       {"action": "resume"})
    except Exception as exc:
        pause_ok = f"failed:{exc}"
    done = wait_job(PORTABLE_LAN, job["id"], timeout=180)
    rem_hash = portable_sha256(f"{PORTABLE_LAB}/inbox/qualify-25mb.bin")
    dur = time.time() - t0
    record("transfer_25mb_pause_resume", {
        "status": done.get("status"),
        "verify": done.get("verify"),
        "pause_resume": pause_ok,
        "src_sha256": big_hash,
        "dest_sha256": rem_hash,
        "match": big_hash == rem_hash,
        "duration_s": round(dur, 3),
        "avg_bps": done.get("avg_bps"),
        "size": 25 * 1024 * 1024,
        "throughput_MBps": round((25 / max(dur, 0.001)), 3),
    })

    # cancel
    cancel_src = STATION_LAB / "outbox" / "cancel-me.bin"
    write_file(cancel_src, 40 * 1024 * 1024, b"CANCEL")
    job = portable_local("POST", "/api/jobs", {
        "kind": "pull",
        "src_base": "http://<station-host>:8415",
        "src_path": str(cancel_src),
        "dest_path": f"{PORTABLE_LAB}/inbox/cancel-me.bin",
        "conflict": "replace",
    })["job"]
    time.sleep(0.2)
    portable_local("POST", f"/api/jobs/{job['id']}/control",
                   {"action": "cancel"})
    cancelled = wait_job(PORTABLE_LAN, job["id"], timeout=30)
    record("transfer_cancel", {"status": cancelled.get("status")})

    # bandwidth limit
    portable_local("POST", "/api/config", {"bandwidth_limit_bps": 2_000_000})
    lim_src = STATION_LAB / "outbox" / "bw-limit.bin"
    write_file(lim_src, 8 * 1024 * 1024, b"BWLIM")
    t0 = time.time()
    job = portable_local("POST", "/api/jobs", {
        "kind": "pull",
        "src_base": "http://<station-host>:8415",
        "src_path": str(lim_src),
        "dest_path": f"{PORTABLE_LAB}/inbox/bw-limit.bin",
        "conflict": "replace",
    })["job"]
    done = wait_job(PORTABLE_LAN, job["id"], timeout=120)
    lim_dur = time.time() - t0
    portable_local("POST", "/api/config", {"bandwidth_limit_bps": 0})
    record("bandwidth_limit", {
        "status": done.get("status"),
        "duration_s": round(lim_dur, 3),
        "avg_bps": done.get("avg_bps"),
        "limit_bps": 2_000_000,
        "appeared_limited": (done.get("avg_bps") or 0) < 4_000_000,
    })

    # read-only share protections
    ro_file = STATION_LAB / "ro-src" / "locked.txt"
    write_file(ro_file, 512, b"LOCKED")
    job = portable_local("POST", "/api/jobs", {
        "kind": "pull",
        "src_base": "http://<station-host>:8415",
        "src_path": str(ro_file),
        "dest_path": f"{PORTABLE_LAB}/inbox/locked-copy.txt",
        "conflict": "replace",
    })["job"]
    copy_done = wait_job(PORTABLE_LAN, job["id"], timeout=30)
    delete_refused = False
    try:
        http(STATION, "POST", "/api/fs/delete",
             {"paths": [str(ro_file)], "confirm": True})
    except RuntimeError as exc:
        delete_refused = "read_only" in str(exc) or "403" in str(exc)
    job = http(STATION, "POST", "/api/jobs", {
        "kind": "pull",
        "src_base": "self",
        "src_path": str(ro_file),
        "dest_path": str(STATION_LAB / "inbox" / "locked-moved.txt"),
        "conflict": "replace",
        "delete_source": True,
    })["job"]
    move_done = wait_job(STATION, job["id"], timeout=30)
    record("readonly_share", {
        "copy_status": copy_done.get("status"),
        "delete_refused": delete_refused and ro_file.exists(),
        "move_status": move_done.get("status"),
        "source_preserved": ro_file.exists(),
        "dest_exists": (STATION_LAB / "inbox" / "locked-moved.txt").exists(),
        "move_error": move_done.get("move_error"),
    })

    # authorized verified move
    moveable = STATION_LAB / "outbox" / "moveable.bin"
    write_file(moveable, 1024, b"MOVEOK")
    job = http(STATION, "POST", "/api/jobs", {
        "kind": "pull",
        "src_base": "self",
        "src_path": str(moveable),
        "dest_path": str(STATION_LAB / "inbox" / "moveable.bin"),
        "conflict": "replace",
        "delete_source": True,
    })["job"]
    ok_move = wait_job(STATION, job["id"], timeout=30)
    record("verified_move_authorized", {
        "status": ok_move.get("status"),
        "source_removed": not moveable.exists(),
        "dest_exists": (STATION_LAB / "inbox" / "moveable.bin").exists(),
    })

    # directory copy
    job = portable_local("POST", "/api/jobs", {
        "kind": "pull_dir",
        "src_base": "http://<station-host>:8415",
        "src_path": str(STATION_LAB / "outbox" / "nested-dir"),
        "dest_path": f"{PORTABLE_LAB}/inbox/nested-dir",
        "conflict": "replace",
    })["job"]
    dir_done = wait_job(PORTABLE_LAN, job["id"], timeout=60)
    record("directory_copy", {
        "status": dir_done.get("status"),
        "verify": dir_done.get("verify"),
        "files_done": dir_done.get("files_done"),
    })

    # cancel + retry under a low bandwidth cap so the job is still active
    portable_local("POST", "/api/config", {"bandwidth_limit_bps": 500_000})
    retry_src = STATION_LAB / "outbox" / "retry.bin"
    write_file(retry_src, 12 * 1024 * 1024, b"RETRY")
    job = portable_local("POST", "/api/jobs", {
        "kind": "pull",
        "src_base": "http://<station-host>:8415",
        "src_path": str(retry_src),
        "dest_path": f"{PORTABLE_LAB}/inbox/retry.bin",
        "conflict": "replace",
    })["job"]
    time.sleep(0.4)
    try:
        portable_local("POST", f"/api/jobs/{job['id']}/control",
                       {"action": "cancel"})
    except RuntimeError as exc:
        # Job may already be terminal on a fast LAN; force-cancel via LAN GET
        cur = http(PORTABLE_LAN, "GET", f"/api/jobs/{job['id']}")
        if (cur.get("job") or {}).get("status") not in (
                "cancelled", "failed", "completed", "completed_partial"):
            raise
        record("interrupt_retry_note", f"cancel raced completion: {exc}")
    cancelled = wait_job(PORTABLE_LAN, job["id"], timeout=30)
    if cancelled.get("status") != "cancelled":
        # Ensure a cancelled job exists for retry by starting a fresh one and
        # cancelling with a still-lower cap.
        job = portable_local("POST", "/api/jobs", {
            "kind": "pull",
            "src_base": "http://<station-host>:8415",
            "src_path": str(retry_src),
            "dest_path": f"{PORTABLE_LAB}/inbox/retry2.bin",
            "conflict": "replace",
        })["job"]
        time.sleep(0.5)
        portable_local("POST", f"/api/jobs/{job['id']}/control",
                       {"action": "cancel"})
        cancelled = wait_job(PORTABLE_LAN, job["id"], timeout=30)
    retried = portable_local("POST", f"/api/jobs/{cancelled['id']}/control",
                             {"action": "retry"})["job"]
    retry_done = wait_job(PORTABLE_LAN, retried["id"], timeout=180)
    portable_local("POST", "/api/config", {"bandwidth_limit_bps": 0})
    dest_retry = retry_done.get("dest_path") or f"{PORTABLE_LAB}/inbox/retry.bin"
    rem = portable_sha256(dest_retry)
    record("interrupt_retry", {
        "status": retry_done.get("status"),
        "cancelled_status": cancelled.get("status"),
        "match": rem == sha256_file(retry_src),
        "dest": dest_retry,
    })

    stats_s = http(STATION, "GET", "/api/stats")
    stats_p = http(PORTABLE_LAN, "GET", "/api/stats")
    record("resource_station", stats_s.get("process"))
    record("resource_portable", stats_p.get("process"))

    try:
        audio = http(STATION, "GET", "/api/audio/presets")
        presets = audio.get("presets") or audio.get("items") or audio
        if isinstance(presets, dict):
            keys = list(presets.keys())[:8]
            count = len(presets)
        else:
            keys = [p.get("id") for p in presets][:8]
            count = len(presets)
        record("audio_presets", {"count": count, "keys": keys})
    except Exception as exc:
        record("audio_presets", {"ok": False, "error": str(exc)})
    try:
        tone = http(STATION, "POST", "/api/audio/tone", {
            "freq": 440, "seconds": 0.15, "volume": 0.05, "play": False})
        record("audio_tone_generate", {"ok": True, "result": list(tone)[:8]})
    except Exception as exc:
        record("audio_tone_generate", {"ok": False, "error": str(exc)})

    events = http(STATION, "GET", "/api/events?limit=20")
    record("events", {"count": len(events.get("events") or [])})
    try:
        mon = http(STATION, "GET", "/api/monitor?device=self")
        record("fleet_monitor_api", {
            "ok": True,
            "cpu": (mon.get("cpu") or {}).get("percent"),
            "rss": (mon.get("process") or {}).get("rss_bytes"),
        })
    except Exception as exc:
        record("fleet_monitor_api", {"ok": False, "error": str(exc)})

    record("architecture", {
        "destination_pull": True,
        "note": "Portable created pull jobs with src_base=http://<station-host>:8415",
    })

    out = STATION_LAB / "lan-qualify-v121.json"
    out.write_text(json.dumps(RESULTS, indent=2), encoding="utf-8")
    print("WROTE", out)
    critical = [
        RESULTS["transfer_station_to_portable"]["match"],
        RESULTS["transfer_portable_to_station"]["match"],
        RESULTS["transfer_25mb_pause_resume"]["match"],
        RESULTS["readonly_share"]["source_preserved"],
        RESULTS["readonly_share"]["move_status"] == "completed_partial",
        RESULTS["verified_move_authorized"]["source_removed"],
        RESULTS["ghostchat"]["station_has_both"],
        RESULTS["ghostchat"]["portable_has_both"],
        RESULTS["interrupt_retry"]["match"],
    ]
    if not all(critical):
        print("CRITICAL_FAILURES", file=sys.stderr)
        for i, ok in enumerate(critical):
            if not ok:
                print(" fail#", i, file=sys.stderr)
        return 1
    print("QUALIFY_OK")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("FATAL", exc, file=sys.stderr)
        STATION_LAB.mkdir(parents=True, exist_ok=True)
        (STATION_LAB / "lan-qualify-v121.json").write_text(
            json.dumps({"fatal": str(exc), **RESULTS}, indent=2))
        raise
