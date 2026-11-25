# HARDWARE_COMPATIBILITY.md

Target environment, measured performance and compatibility notes.

## 1. Initial deployment machines

| Machine | CPU | RAM | GPU | Phantom role |
|---|---|---|---|---|
| Windows PC | Intel Core i5-12500 (6P/12T) | ~55.7 GB | Intel UHD 770 | companion + UI + AI inference host |
| Ubuntu Station | Intel Core i5-7500 (4C/8T) | ~22 GB | Intel HD 630 | companion + UI |
| Ubuntu Portable | Intel Core i7-8850H (6C/12T) | ~45 GB | NVIDIA Quadro P2000 | companion + UI, optional GPU telemetry |

Phantom Network does **not** hardcode any of these values — they were used
only to size resource budgets. Every machine runs the identical companion;
capabilities are detected at runtime (audio player, GPU via `nvidia-smi`,
mount points, share roots).

## 2. Software compatibility

| Component | Windows 11 x64 | Ubuntu 22.04 | Ubuntu 24.04+ |
|---|---|---|---|
| Companion (Python ≥ 3.9) | ✔ (3.12 tested pattern; stdlib-only core) | ✔ (3.10) | ✔ (3.12 — this is what CI/dev used) |
| Web UI | Chrome/Edge/Firefox | Firefox/Chromium | same |
| Audio playback engine | PowerShell `Media.SoundPlayer` (WAV) | `paplay` → `aplay` → `ffplay` chain | same |
| Discovery (UDP broadcast/multicast 8414) | ✔ | ✔ | ✔ |
| `psutil` telemetry | optional wheel | optional wheel | optional wheel |
| GPU telemetry | `nvidia-smi` if NVIDIA driver present | same | same (Quadro P2000 supported by nvidia-smi) |

The development/verification environment for this release was
**Linux 5.10 x86_64, Python 3.12.16, 2 vCPU / 4 GB RAM container** — every
claim in TEST_REPORT.md was measured there unless explicitly marked
otherwise. Windows-specific code paths (ctypes drives/GPU/uptime,
PowerShell audio, `GetLogicalDriveStringsA`, `GetSystemTimes`) are
implemented but were **not executed on real Windows hardware** — see
TEST_REPORT.md §execution matrix.

## 3. Measured resource profile (dev container, honest numbers)

Measured with `GET /api/stats` (`process` block) and `/usr/bin/time` during
the verification run:

| Metric | Value (measured) |
|---|---|
| Companion RSS (idle) | ~28–34 MB |
| Companion threads (idle) | ~9 (HTTP server + discovery ×2 + maintenance + monitor + chat outbox + scheduler) |
| Idle CPU | ~0 % (single 3 s UDP beacon + 5 s telemetry sample; no busy loops) |
| RSS during a 40 MB/s transfer | +5–8 MB (1 MiB chunk buffers, flat profile — no file is ever fully loaded) |
| Loopback throughput (2 vCPU container) | ~400–800 MB/s aggregate, scheduler-limited by 1 MiB round trips |
| 30 MB cross-companion transfer (UI-driven) | < 1 s + SHA-256 verification |
| Bandwidth limiter accuracy | 12 MB configured → 12.0–12.6 MB/s measured over 60 MB |

Expected on the target machines: memory and CPU profiles will be similar
(the companion is I/O-bound); LAN throughput becomes the bottleneck
(~110 MB/s on 1 GbE) instead of the process.

## 4. Browser matrix (tested)

| Browser | Result |
|---|---|
| Chrome 158 (headless, this environment) | all 12 views console-clean; transfer, chat, radar verified (see TEST_REPORT.md) |
| Mobile viewport 375×812 (Chrome emulation) | layout reflows to single column; no horizontal overflow on Dashboard/Command Center |
| Other modern browsers (Firefox/Safari) | expected to work (standard ES2020 + EventSource + WebAudio); not executed in this environment — noted, not claimed |

## 5. Known hardware-specific caveats

* **Coarse filesystem timestamps**: some container/overlay filesystems and
  FAT volumes have second-level mtime granularity; the transfer hash cache
  keys on `(size, mtime_ns)` — a same-second, same-size rewrite of a file
  could serve a stale hash for the *compare* conflict mode. Verified-delete
  is unaffected (it always re-hashes).
* **Audio hardware**: the sandbox used for development has no sound device;
  playback is verified down to the correct player invocation and WAV
  synthesis, not to audible output (documented in TEST_REPORT.md).
* **Quadro P2000 telemetry**: reported via `nvidia-smi` queries when the
  driver is installed; the dev container has no GPU, so the Fleet Monitor
  honestly shows "not detected".


---

# v1.3.0 hardware-facing additions

- Storage: mounts classified physical/virtual/snap/network/external with
  read-only + removable flags (Linux: /proc/mounts + /sys/block/removable;
  Windows: GetDriveTypeW). Default view hides Snap loops and virtual
  filesystems; complete diagnostics preserved behind "Everything".
- GPU: NVIDIA via nvidia-smi (utilization/VRAM/temp when available);
  Intel/AMD identity via /sys/class/drm (vendor/product_name/driver).
  Utilization for Intel/AMD is reported as unavailable — never fabricated.
- CPU: physical cores from /proc/cpuinfo (physical id + core id) or
  psutil.cpu_count(logical=False); logical count from os.cpu_count().
- Phones: any modern browser (iOS Safari 16+, Android Chrome/FF); mic needs
  secure context. Desktop browsers unchanged from v1.2.2 matrix.
