"""Fleet Monitor telemetry collector.

Priority order per metric: **psutil** (if installed — recommended, small
dependency) → platform APIs (Linux ``/proc``, Windows ``ctypes``) →
``None`` (reported honestly as unavailable, never fabricated).

GPU information comes from ``nvidia-smi`` when present (parsed CSV, no
large libraries).  A background sampler keeps a rolling history buffer
(default 240 points × 5 s = 20 minutes) for the Fleet Monitor sparklines.
Sampling is cheap (<1 ms with psutil, ~2 ms via /proc).
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import threading
import time
from typing import Dict, List, Optional

from .util import local_ipv4s, now_ms

try:
    import psutil  # optional; gracefully degraded without it
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

SAMPLE_INTERVAL_S = 5.0


class Monitor:
    def __init__(self, config, sandbox_shares_provider, log=None):
        self.config = config
        self._shares_provider = sandbox_shares_provider
        self.log = log
        self._history: List[dict] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_cpu_times = self._read_cpu_times()
        self._last_net = self._read_net_counters()
        self._last_net_ts = time.monotonic()
        self._last_cpu_ts = self._last_net_ts
        threading.Thread(target=self._sampler, name="pn-monitor",
                         daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    def _sampler(self) -> None:
        while not self._stop.wait(SAMPLE_INTERVAL_S):
            if not self.config.get("monitoring_enabled", True):
                continue
            try:
                snap = self.snapshot()
                with self._lock:
                    self._history.append({
                        "ts": snap["ts"],
                        "cpu": snap["cpu"]["percent"],
                        "mem": snap["memory"]["percent"],
                        "net_bps": snap["network"].get("rx_bps"),
                    })
                    cap = int(self.config.get("monitor_history_points") or 240)
                    del self._history[:-cap]
            except Exception:
                pass

    def history(self) -> List[dict]:
        with self._lock:
            return list(self._history)

    # ------------------------------------------------------------------
    # low-level readers
    # ------------------------------------------------------------------
    def _read_cpu_times(self) -> Optional[tuple]:
        if _HAS_PSUTIL:
            return psutil.cpu_times()
        if os.path.exists("/proc/stat"):
            try:
                with open("/proc/stat", "r", encoding="utf-8") as fh:
                    parts = fh.readline().split()
                if parts and parts[0] == "cpu":
                    vals = [float(v) for v in parts[1:8]]
                    return ("sum", sum(vals), vals[3] + vals[4])  # total, idle
            except (OSError, ValueError):
                pass
        if os.name == "nt":
            try:
                class FT(ctypes.Structure):
                    _fields_ = [("idle", ctypes.c_ulonglong * 2),
                                ("kernel", ctypes.c_ulonglong * 2),
                                ("user", ctypes.c_ulonglong * 2)]
                ft = FT()
                if ctypes.windll.kernel32.GetSystemTimes(
                        ctypes.byref(ft.idle), ctypes.byref(ft.kernel),
                        ctypes.byref(ft.user)):
                    idle = ft.idle[0] + (ft.idle[1] << 64)
                    busy = ft.user[0] + (ft.user[1] << 64) + \
                        ft.kernel[0] + (ft.kernel[1] << 64) - idle
                    return ("win", idle, busy)
            except Exception:
                pass
        return None

    def _read_net_counters(self) -> Optional[Dict[str, int]]:
        if _HAS_PSUTIL:
            io = psutil.net_io_counters()
            return {"rx": io.bytes_recv, "tx": io.bytes_sent}
        if os.path.exists("/proc/net/dev"):
            rx = tx = 0
            try:
                with open("/proc/net/dev", "r", encoding="utf-8") as fh:
                    lines = fh.readlines()[2:]
                for line in lines:
                    if ":" not in line:
                        continue
                    _iface, rest = line.split(":", 1)
                    if _iface.strip() == "lo":
                        continue
                    fields = rest.split()
                    rx += int(fields[0])
                    tx += int(fields[8])
                return {"rx": rx, "tx": tx}
            except (OSError, ValueError, IndexError):
                pass
        return None

    def _cpu_percent(self) -> Optional[float]:
        now = time.monotonic()
        cur = self._read_cpu_times()
        if cur is None or self._last_cpu_times is None:
            self._last_cpu_times = cur
            self._last_cpu_ts = now
            return None
        dt = now - self._last_cpu_ts
        if dt <= 0.2:
            return None
        try:
            if _HAS_PSUTIL:
                pct = psutil.cpu_percent(interval=None)
                self._last_cpu_times = cur
                self._last_cpu_ts = now
                return pct
            kind = cur[0]
            last_kind = self._last_cpu_times[0]
            if kind != last_kind:
                self._last_cpu_times = cur
                self._last_cpu_ts = now
                return None
            if kind == "sum":
                d_total = cur[1] - self._last_cpu_times[1]
                d_idle = cur[2] - self._last_cpu_times[2]
                if d_total <= 0:
                    return None
                pct = max(0.0, min(100.0, 100.0 * (1 - d_idle / d_total)))
            else:  # windows ("win", idle, busy)
                d_idle = cur[1] - self._last_cpu_times[1]
                d_busy = cur[2] - self._last_cpu_times[2]
                tot = d_idle + d_busy
                pct = max(0.0, min(100.0, 100.0 * d_busy / tot)) if tot > 0 else None
            self._last_cpu_times = cur
            self._last_cpu_ts = now
            return pct
        except (IndexError, TypeError):
            return None

    def _memory(self) -> dict:
        if _HAS_PSUTIL:
            vm = psutil.virtual_memory()
            sw = psutil.swap_memory()
            return {
                "total": vm.total, "available": vm.available,
                "used": vm.used, "percent": vm.percent,
                "swap_total": sw.total, "swap_used": sw.used,
                "swap_percent": sw.percent,
            }
        if os.path.exists("/proc/meminfo"):
            info = {}
            try:
                with open("/proc/meminfo", "r", encoding="utf-8") as fh:
                    for line in fh:
                        key, rest = line.split(":", 1)
                        info[key.strip()] = int(rest.strip().split()[0]) * 1024
            except (OSError, ValueError, IndexError):
                return {"total": None, "available": None, "used": None,
                        "percent": None, "swap_total": None,
                        "swap_used": None, "swap_percent": None}
            total = info.get("MemTotal", 0)
            avail = info.get("MemAvailable", info.get("MemFree", 0))
            used = max(0, total - avail)
            st, sf = info.get("SwapTotal", 0), info.get("SwapFree", 0)
            return {
                "total": total, "available": avail, "used": used,
                "percent": round(100.0 * used / total, 1) if total else None,
                "swap_total": st, "swap_used": st - sf,
                "swap_percent": round(100.0 * (st - sf) / st, 1) if st else 0.0,
            }
        return {"total": None, "available": None, "used": None, "percent": None,
                "swap_total": None, "swap_used": None, "swap_percent": None}

    def _uptime_s(self) -> Optional[float]:
        if _HAS_PSUTIL:
            try:
                return time.time() - psutil.boot_time()
            except Exception:
                pass
        if os.path.exists("/proc/uptime"):
            try:
                with open("/proc/uptime", "r", encoding="utf-8") as fh:
                    return float(fh.read().split()[0])
            except (OSError, ValueError, IndexError):
                pass
        if os.name == "nt":
            try:
                ms = ctypes.windll.kernel32.GetTickCount64()
                return ms / 1000.0
            except Exception:
                pass
        return None

    def _disks(self) -> List[dict]:
        out = []
        roots = []
        for share in self._shares_provider():
            roots.append(share["path"])
        if _HAS_PSUTIL:
            parts = psutil.disk_partitions(all=False)
            mounts = [p.mountpoint for p in parts if "cdrom" not in p.opts]
        elif os.path.exists("/proc/mounts"):
            mounts = []
            try:
                with open("/proc/mounts", "r", encoding="utf-8") as fh:
                    for line in fh:
                        parts_ = line.split()
                        if len(parts_) >= 3 and parts_[2] not in (
                                "proc", "sysfs", "tmpfs", "devtmpfs", "devpts",
                                "squashfs", "cgroup", "cgroup2", "overlay",
                                "mqueue", "hugetlbfs", "bpf", "tracefs"):
                            mounts.append(parts_[1])
            except OSError:
                mounts = []
        else:
            mounts = []
        for mount in mounts:
            if mount.startswith(("/sys", "/proc", "/dev", "/run", "/boot/efi")):
                continue
            try:
                if _HAS_PSUTIL:
                    usage = psutil.disk_usage(mount)
                    total, free = usage.total, usage.free
                else:
                    fr = os.statvfs(mount)
                    total, free = fr.f_frsize * fr.f_blocks, fr.f_frsize * fr.f_bavail
                out.append({"mount": mount, "total": total, "free": free,
                            "used": total - free,
                            "percent": round(100.0 * (total - free) / total, 1)
                            if total else None})
            except (OSError, PermissionError):
                continue
        for root in roots:
            if not any(d["mount"] == root or root.startswith(d["mount"])
                       for d in out):
                try:
                    fr = os.statvfs(root)
                    total, free = fr.f_frsize * fr.f_blocks, fr.f_frsize * fr.f_bavail
                    out.append({"mount": root, "total": total, "free": free,
                                "used": total - free,
                                "percent": round(100.0 * (total - free) / total, 1)
                                if total else None})
                except OSError:
                    pass
        return out

    # v1.3.0 mount classification ----------------------------------------
    VIRTUAL_FSTYPES = {"squashfs", "tmpfs", "devtmpfs", "proc", "sysfs",
                       "devpts", "cgroup", "cgroup2", "overlay", "mqueue",
                       "hugetlbfs", "bpf", "tracefs", "fusectl",
                       "fuse.gvfsd-fuse", "efivarfs", "binfmt_misc", "pstore",
                       "securityfs", "debugfs", "configfs", "autofs",
                       "fuse.snapfuse", "ramfs", "cramfs"}

    def _mount_meta(self, mount: str) -> dict:
        """(fstype, device, read_only) for one mountpoint, honestly."""
        if _HAS_PSUTIL:
            for p in psutil.disk_partitions(all=True):
                if p.mountpoint == mount:
                    ro = "ro" in (p.opts or "").split(",")
                    return {"fstype": p.fstype, "device": p.device,
                            "read_only": ro, "opts": p.opts or ""}
        if os.path.exists("/proc/mounts"):
            try:
                with open("/proc/mounts", "r", encoding="utf-8") as fh:
                    for line in fh:
                        parts_ = line.split()
                        if len(parts_) >= 4 and parts_[1] == mount:
                            ro = "ro" in parts_[3].split(",")
                            return {"fstype": parts_[2], "device": parts_[0],
                                    "read_only": ro, "opts": parts_[3]}
            except OSError:
                pass
        return {"fstype": "", "device": "", "read_only": False, "opts": ""}

    def _classify_mount(self, meta: dict, mount: str) -> dict:
        fstype = (meta.get("fstype") or "").lower()
        device = meta.get("device") or ""
        kind = "physical"
        if not device or device.startswith(("proc", "sysfs", "devtmpfs",
                                            "tmpfs", "cgroup", "overlay")) \
                or fstype in self.VIRTUAL_FSTYPES:
            kind = "virtual"
        elif device.startswith("/dev/loop") or fstype == "squashfs" \
                or mount.startswith("/snap"):
            kind = "snap"
        elif fstype.startswith("nfs") or fstype == "cifs" or fstype == "smb2":
            kind = "network"
        elif device.startswith("/dev/"):
            kind = "physical"
        else:
            kind = "virtual"
        removable = False
        if kind == "physical" and os.path.isdir("/sys/block"):
            base = device.rsplit("/", 1)[-1]
            base = base.rstrip("0123456789") if base[:2] in ("sd", "hd",
                                                             "vd") else \
                (base.rsplit("p", 1)[0] if "nvme" in base else base)
            for cand in ({base, base + "0", base.split("p")[0]} if
                         base.startswith("nvme") else {base}):
                try:
                    with open(f"/sys/block/{cand}/removable") as fh:
                        if fh.read().strip() == "1":
                            removable = True
                            break
                except OSError:
                    continue
        if os.name == "nt" and len(mount) >= 2 and mount[1] == ":":
            try:
                ct = 0
                import ctypes
                ct = ctypes.windll.kernel32.GetDriveTypeW(mount[:2] + "\\")
                if ct == 2:
                    kind, removable = "external", True
                elif ct == 4:
                    kind = "network"
                elif ct == 5:
                    kind = "optical"
            except Exception:
                pass
        return {"kind": kind, "removable": removable,
                "fstype": fstype, "device": device,
                "read_only": bool(meta.get("read_only"))}

    def mounts(self, view: str = "useful") -> List[dict]:
        """Classified storage view: physical | useful | all (v1.3.0).

        ``useful`` (default) hides Snap loops and virtual mounts; ``physical``
        keeps only real block devices; ``all`` preserves complete diagnostics.
        Nothing is deleted from the data — only the default display filters.
        """
        everything = self._disks()
        out = []
        hidden = [p for p in (self.config.get("storage_hidden_prefixes") or [])]
        for d in everything:
            meta = self._mount_meta(d["mount"])
            d.update(self._classify_mount(meta, d["mount"]))
            d["hidden_by_config"] = any(
                d["mount"].startswith(p) for p in hidden if p)
            out.append(d)
        if view == "all":
            return out
        if view == "physical":
            return [d for d in out if d["kind"] == "physical"]
        # useful (default): physical + external + network, minus snap/virtual
        return [d for d in out
                if d["kind"] in ("physical", "network") and
                not d["mount"].startswith("/snap") and
                not d["hidden_by_config"]]

    def gpu_info(self) -> dict:
        """GPU info across vendors — honest about what is unavailable.

        NVIDIA via nvidia-smi (values); Intel/AMD via sysfs drm identity
        (name/driver only — utilization is NOT fabricated without tools).
        """
        gpus = []
        nv = self._gpu()
        if nv and nv.get("gpus"):
            for g in nv["gpus"]:
                g["vendor"] = "NVIDIA"
                g["util_available"] = True
            gpus.extend(nv["gpus"])
        if os.path.isdir("/sys/class/drm"):
            for card in sorted(os.listdir("/sys/class/drm")):
                if not card.startswith("card") or "@" in card:
                    continue
                base = f"/sys/class/drm/{card}/device"
                try:
                    with open(f"{base}/vendor") as fh:
                        vendor_id = fh.read().strip()
                except OSError:
                    continue
                vendor = {"0x8086": "Intel", "0x1002": "AMD"}.get(vendor_id)
                if not vendor:
                    continue
                name = ""
                for cand in ("product_name", "product"):
                    try:
                        with open(f"{base}/{cand}", "r",
                                  encoding="utf-8", errors="replace") as fh:
                            name = fh.read().strip()
                            if name:
                                break
                    except OSError:
                        continue
                driver = ""
                try:
                    driver = os.path.basename(os.readlink(f"{base}/driver"))
                except OSError:
                    pass
                if not name:
                    name = f"{vendor} graphics ({card})"
                gpus.append({"index": len(gpus), "vendor": vendor,
                             "name": name, "driver": driver,
                             "util_percent": None, "mem_used_mb": None,
                             "mem_total_mb": None, "temp_c": None,
                             "util_available": False})
        return {"gpus": gpus}

    def cpu_topology(self) -> dict:
        """Physical vs logical cores where the OS exposes both."""
        logical = os.cpu_count()
        physical = None
        if _HAS_PSUTIL:
            try:
                physical = psutil.cpu_count(logical=False) or None
            except Exception:
                physical = None
        if physical is None and os.path.exists("/proc/cpuinfo"):
            cores = set()
            try:
                with open("/proc/cpuinfo", "r", encoding="utf-8") as fh:
                    cur = {}
                    for line in fh:
                        line = line.strip()
                        if not line:
                            if ("physical id" in cur) and ("core id" in cur):
                                cores.add((cur["physical id"], cur["core id"]))
                            cur = {}
                            continue
                        if ":" in line:
                            k, v = line.split(":", 1)
                            if k.strip() in ("physical id", "core id"):
                                cur[k.strip()] = v.strip()
                    if ("physical id" in cur) and ("core id" in cur):
                        cores.add((cur["physical id"], cur["core id"]))
                physical = len(cores) or None
            except OSError:
                physical = None
        if physical is None and os.name == "nt":
            physical = logical       # Windows: honest fallback, same count
        return {"logical": logical, "physical": physical}

    def _network(self) -> dict:
        now = time.monotonic()
        cur = self._read_net_counters()
        result: Dict[str, Optional[float]] = {"rx_total": None, "tx_total": None,
                                              "rx_bps": None, "tx_bps": None,
                                              "addresses": local_ipv4s()}
        if cur:
            result["rx_total"] = cur["rx"]
            result["tx_total"] = cur["tx"]
            dt = now - self._last_net_ts
            if dt > 0.3 and self._last_net:
                result["rx_bps"] = max(0.0, (cur["rx"] - self._last_net["rx"]) / dt)
                result["tx_bps"] = max(0.0, (cur["tx"] - self._last_net["tx"]) / dt)
        self._last_net = cur
        self._last_net_ts = now
        return result

    def _interfaces(self) -> List[dict]:
        if _HAS_PSUTIL:
            try:
                stats = psutil.net_if_addrs()
                out = []
                for iface, addrs in stats.items():
                    entry = {"name": iface, "ipv4": "", "mac": ""}
                    for a in addrs:
                        fam = getattr(a.family, "name", str(a.family))
                        if "AF_INET" in fam and a.address != "127.0.0.1":
                            entry["ipv4"] = a.address
                        if "AF_LINK" in fam or (os.name == "nt" and fam == "-1"):
                            entry["mac"] = a.address
                    if entry["ipv4"] or entry["mac"]:
                        out.append(entry)
                return out
            except Exception:
                pass
        return [{"name": "", "ipv4": a, "mac": ""} for a in local_ipv4s()]

    def _gpu(self) -> Optional[dict]:
        """NVIDIA GPUs via nvidia-smi (if present). No fabricated values."""
        smi = shutil.which("nvidia-smi")
        if not smi:
            return None
        try:
            out = subprocess.run(
                [smi, "--query-gpu=index,name,utilization.gpu,memory.used,"
                       "memory.total,temperature.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=4)
            if out.returncode != 0:
                return None
            gpus = []
            for line in out.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 6:
                    gpus.append({
                        "index": int(parts[0] or 0),
                        "name": parts[1],
                        "util_percent": float(parts[2]) if parts[2] else None,
                        "mem_used_mb": float(parts[3]) if parts[3] else None,
                        "mem_total_mb": float(parts[4]) if parts[4] else None,
                        "temp_c": float(parts[5]) if parts[5] else None,
                    })
            return {"source": "nvidia-smi", "gpus": gpus} if gpus else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None

    def _process(self) -> dict:
        """This companion's own resource usage (honest measurement)."""
        try:
            if _HAS_PSUTIL:
                proc = psutil.Process(os.getpid())
                mem = proc.memory_info()
                return {"rss_bytes": mem.rss, "threads": proc.num_threads(),
                        "cpu_percent": proc.cpu_percent(interval=None)}
            import resource
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
            return {"rss_bytes": rss, "threads": threading.active_count(),
                    "cpu_percent": None}
        except Exception:
            return {"rss_bytes": None, "threads": threading.active_count(),
                    "cpu_percent": None}

    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        mem = self._memory()
        cpu = self._cpu_percent()
        return {
            "ts": now_ms(),
            "host": platform.node(),
            "os": f"{platform.system()} {platform.release()}",
            "python": platform.python_version(),
            "psutil": _HAS_PSUTIL,
            "cpu": {
                "percent": cpu,
                "cores": (os.cpu_count() or None),
                "loadavg": (list(os.getloadavg()) if hasattr(os, "getloadavg")
                            and os.name != "nt" else None),
            },
            "memory": mem,
            "disks": self._disks(),
            "network": self._network(),
            "interfaces": self._interfaces(),
            "uptime_s": self._uptime_s(),
            "gpu": self._gpu(),
            "gpu_info": self.gpu_info(),
            "cpu_topology": self.cpu_topology(),
            "storage_view": (self.config.get("storage_view") or "useful"),
            "process": self._process(),
        }
