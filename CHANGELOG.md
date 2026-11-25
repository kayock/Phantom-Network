# CHANGELOG.md

All notable changes to PHANTOM NETWORK.

## [1.2.2] — 2026-11-25

Large-file checksum reliability after the physical 50 GB LAN qualification.

### Fixed

* **Cold source SHA-256 timeout.** Destination pulls called
  `/api/fs/hash` with a single 30-second peer timeout. Cold hashing of a
  50 GB file exceeded that window, so after three retries the job became
  `interrupted` with `size=0`. Source checksum preparation is now a
  joinable background job (`POST/GET /api/fs/hash/prepare`) polled with
  short HTTP timeouts. Ordinary PeerClient defaults remain 10 seconds.
  Transfer Center shows **Calculating source checksum** with measured
  progress when available. Failed preparation reports `hash_failed`
  instead of a misleading zero-byte transfer. Verified deletion still
  re-hashes and never trusts the cache for destructive removal.

### Physical evidence preserved

* v1.2.1 LAN qualification through 50 GB (after manual hash-cache warm)
  remains documented in `TEST_REPORT.md` and
  `~/phantom-network-lab/large-file-qualify-v121.json`.

## [1.2.1] — 2026-11-25

Physical-deployment corrections after the Z.ai v1.2.0 archive. Ubuntu
Station and Ubuntu Portable are the qualified platforms for this release.
Windows remains unverified.

### Fixed

* **Ubuntu installer runtime ownership.** `install-ubuntu.sh` resolved
  `$HOME` and `XDG_*` before identifying the desktop user, so a sudo
  install baked `/root` paths into the launcher. The installer now
  resolves the invoking user (`SUDO_USER`, `PKEXEC_UID`, then `logname`)
  and that user's home first, ignores XDG values that are not inside that
  home, and writes a launcher whose config and log paths expand when the
  desktop user starts the companion. The Debian package launcher no longer
  writes `companion.out` into `/opt`.
* **Read-only shares inside a writable ancestor.** Share authorization
  used the first matching root. A read-write parent therefore authorized
  deletion of a file inside a more specific read-only share, including
  verified deletion. Authorization now uses the longest matching share
  and prefers the read-only mode when two roots are the same length.
  A verified move whose source cannot be removed stays `completed_partial`
  and keeps the source. Fresh-checksum verification is unchanged.
* **Fleet Monitor view.** The view passed an unresolved history request
  to `appendChild` and never attached its panels, so the page reported
  "View failed to load". History is awaited, and the device selector,
  note and panels are attached to the view.

## [1.2.0] — 2026-11-25

Final engineering hardening & deployment readiness.

### Fixed — confirmed defects from the independent v1.1.0 inspection

* **Windows installer path corruption**: `install-windows.ps1` contained
  embedded vertical-tab (0x0b) and carriage-return (0x0d) bytes from a
  Python escape accident in the v1.1.0 edit (`"\v"` → VT, `"\r"` → CR).
  All three Windows scripts rewritten with `Join-Path` construction; a new
  byte-level validator (`packaging/validate_release.py`) now blocks control
  characters, non-UTF-8 and missing deliverables from ever shipping again.
* **Windows uninstaller logic was inverted**: `-KeepData` DELETED the data
  and default kept it. New semantics: default keeps runtime data
  (`%APPDATA%\PhantomNetwork`), `-RemoveData` deletes it explicitly.
* **Ubuntu launcher wrote logs into the root-owned program dir**
  (`/opt/phantom-network/companion.out`): ordinary users could not launch
  Phantom Network. Logs now go to `~/.local/state/phantom-network/`
  (XDG), state to `~/.config/phantom-network/`; the launcher writes
  NOTHING into the program dir (verified against a read-only program dir).
* **Remote LAN clients could change local configuration** (shares,
  read/write modes, remote-audio toggle, settings) undermining the
  first-run wizard's selections. Administrative endpoints (`/api/config`,
  `/api/fs/share`, `/api/fs/unshare`, `/api/audio/enabled`) now accept
  changes only from the local machine (`admin_local_only`, default ON,
  documented escape hatch). Peer operations are unaffected. Implemented
  with the existing architecture; **no new mandatory authentication**.
* **Verified deletion could serve a cached checksum** (data-preservation
  risk): destructive deletion now ALWAYS re-hashes the current content
  (cache bypassed), captures file identity `(st_dev, st_ino, size,
  mtime_ns)` before hashing and re-checks it immediately before unlink,
  refuses symbolic links, and drops the cache entry afterwards. Regression
  tests prove a stale cache + preserved size/mtime cannot authorize
  deleting modified or replaced data.

### Added

* Windows destination-name validation: reserved device names (CON, AUX,
  COM1-9…), invalid characters, trailing dots/spaces, 255-char component
  limit → explicit `invalid_dest_name` failure, never silent rename/skip.
* `CURSOR_DEPLOYMENT_HANDOFF.md`: staged A–F physical deployment procedure
  (Ubuntu Station, Ubuntu Portable, Windows 11, audio/mobile, large models,
  GitHub prep).
* Release validation integrated into `packaging/make-dist.sh`.
* Companion lifecycle + large-transfer qualification suites (concurrent
  2×1.2 GiB transfers, cancellation with `.part` retention, long-filename
  transfers, restart-then-resume source-change detection, real-data window
  beyond 2^32, port-conflict behaviour, 5× start/stop thread/RSS stability).
* Debian package build verified with `dpkg-deb` (shipped under `dist/`).

## [1.1.0] — 2026-11-25

Reliability, File Commander & cross-platform refinement release.

### Fixed — the five v1.0.0 integration failures (root causes)

1. **Broadcast/group chat tests** — test-side defect: unencoded spaces in
   history query URLs (`http.client.InvalidURL`); queries now URL-encoded
   and the test client is hardened. No server defect.
2. **Event export test** — test-side defect: `.decode()` on an
   already-parsed dict after the client gained JSON parsing.
3. **Auth token test** — app-side defect: `POST /api/config` enabled
   `require_token` without generating a token → every request (including
   the token-holder's) was refused. `config_set` now generates a token.
4. **Reconnection test** — app-side defect: offline-TTL sweep ran only
   every 30 s, so a fast re-register was treated as "refresh" and produced
   no `device_connected` event. Sweep now runs every 5 s (latency probes
   stay at 30 s).
5. **"Chat receiver hang"** — was a cascade of the above test defects plus
   the v1.0.0 pause/resume worker race (fixed earlier); with corrected
   tests and per-companion stderr capture the full suite is green and the
   receiver stays responsive.

### Fixed — independently identified defects

* **A. Default filesystem exposure**: fresh installs share NOTHING
  (`share_home_rw` now False, `setup_complete` flag). First-run wizard in
  the UI authorizes directories/drives explicitly with read-only /
  read-write choices. Existing configured installs are unaffected.
* **C. Installer pip isolation**: Ubuntu installer builds an app-owned
  venv for optional psutil (PEP 668-compatible); pure-stdlib fallback.
* **D. Unsafe ownership changes**: removed the recursive `chown` of
  `~/.local`; only files the installer itself creates are adjusted.
* **E. Autostart unit**: corrected `ExecStart`/`WorkingDirectory`
  construction, proper venv python path, `PYTHONUNBUFFERED`, on-failure
  restart; still strictly opt-in.
* **F. Missing packaging files**: added `packaging/companion.spec`
  (Windows PyInstaller) and `packaging/make-deb.sh` (Debian/Ubuntu .deb).
* **G. Directory truncation**: `/api/fs/walk` is now paginated
  (`skip`/`limit`/`next_offset`); directory pulls fetch every page; trees
  beyond `max_dir_files` (default 200 000) are REFUSED (`tree_too_large`)
  instead of silently truncated.
* **H. Sync by size only**: comparison strategies `size` / `mtime` /
  `hash` (verified SHA-256 preview with a 400-file cap; hash failures copy
  + verify rather than skip).

### Added — reliability

* Source-identity binding: resume refuses changed sources
  (size/mtime_ns mismatch → clear error, `.part` retained, retry path).
* Retry works from history: terminal jobs can be re-enqueued cleanly
  (previously `retry` on a failed job silently did nothing).
* Transfer-state persistence is throttled and excludes in-memory walk
  manifests (directory jobs no longer quadratic; re-enumeration on restart
  is deterministic).
* `completed_partial` transfer state: verified copy + retained source
  reported honestly with both copies' locations (never plain "completed").
* `verified_delete` confirms the source is unchanged via the (size,
  mtime_ns) identity before deleting — no blind re-read of huge files.
* Diagnostics Center: expanded checks with fix hints, common-problem guide,
  exportable local diagnostic report (`/api/diagnostics/report`).
* Peer capability flags (audio/intercom/shares count) advertised in the
  identity payload and shown in the UI (radar hints, audio gating).
* Per-companion stderr capture in the test harness for diagnosability.

### Performance (measured)

* 2.6 GiB real cross-companion transfer with pause at >2^31 offset, resume,
  SHA-256 equality re-verified independently; companion RSS stayed
  < 250 MB during the transfer (bounded, size-independent).
* >4 GiB Range boundary probe on a sparse file (206 + exact Content-Range).

## [1.0.0] — 2026-11-25

First public release.

### Added — companion (backend)

* Threaded HTTP/1.1 companion service with JSON API, SSE event stream and
  permissive CORS for cross-device browser use (`httpd.py`, `routes.py`).
* UDP discovery: 3 s broadcast/multicast beacons (port 8414), persistent
  peer registry with custom names/icons/groups, offline TTL detection,
  latency + uptime probes, manual registration (with honest
  non-participating reporting and self-probe refusal), opt-in /24 TCP sweep
  on the API port only (`discovery.py`, `peers.py`).
* Filesystem service with a hard security sandbox: shares (rw/ro),
  realpath containment, traversal + symlink-escape refusal, drive/mount
  listing, breadcrumbs-friendly listing, hidden-file toggle, bounded
  search, text/image/binary preview, zip download, chunked upload sessions
  with conflict handling (`fsops.py`).
* Transfer engine: destination-pull jobs, 1 MiB streaming with live SHA-256,
  `.part` resume, pause/resume/cancel/retry, automatic retry with backoff,
  token-bucket bandwidth limiting, disk-space pre-check, four conflict
  modes, directory pulls with per-file checkpoints, verified moves
  (copy → verify → source-side re-check → delete), job persistence across
  restarts (`transfers.py`).
* GhostChat: direct/group/broadcast messaging between companions, delivery
  + read receipts, offline outbox with auto-redelivery, searchable
  JSONL history, attachment references backed by the transfer engine
  (`chat.py`).
* Audio Command: original synthesized presets (8), tone generator with
  safety caps, platform player chain (PowerShell SoundPlayer / paplay /
  aplay / ffplay), visible enable/disable per device, stop control
  (`audio.py`).
* Phantom Intercom: opt-in PTT PCM relay (16 kHz Int16, ~60 ms chunks)
  companion-to-companion with SSE delivery; no recording, no covert capture
  (`intercom.py`).
* Fleet Monitor: CPU/RAM/swap/disks/network/interfaces/uptime telemetry via
  psutil → /proc → Windows ctypes → honest `null`s, 20-minute history
  buffer, nvidia-smi GPU probe, companion self-usage reporting
  (`monitor.py`).
* Event Center: typed event bus with JSONL persistence (5 000-entry ring),
  live SSE fanout, filtering/search/export (`events.py`).
* Configuration: platform data dir + portable marker mode, change
  listeners, public view without secrets, optional shared-token auth
  (`config.py`).
* Diagnostics: quick + full self-tests (config, HTTP, loopback call,
  discovery socket, audio player, hashing, shares, transfer engine, chat
  store, telemetry).

### Added — command center (frontend)

* Obsidian/graphite/violet/cyan themed SPA (no build step) served by every
  companion; responsive to 375 px viewports.
* Twelve views: Command Center, Network Radar (animated canvas map +
  device actions), File Commander (dual-pane, cross-device, drag & drop,
  favorites, roots/shares selector, search), Transfer Center (six-state
  tabs, live progress, bandwidth control, sync preview/apply, report
  export), GhostChat (conversations, receipts, attachments, search),
  Audio Command (target selection, tone gen, presets, file playback),
  Intercom (session picker, hold-to-talk PTT, secure-context guidance),
  Signal Lab (tone generator, spectrum analyzer with mic/file input,
  Morse lab with mic decoding, FSK acoustic messaging with CRC + confidence,
  offline roundtrip demo), Fleet Monitor, Events, Settings, Diagnostics.
* Browser-side DSP library (`lib/dsp.js`): Morse codec, Goertzel, FSK
  acoustic codec — verified by a node test suite.
* Browser-side preset synth (`lib/sfx.js`).
* Original branding: hexagonal node-network logo (SVG), boot animation.

### Added — packaging & docs

* Installers (Ubuntu `.sh`, Windows `.ps1`), uninstallers, portable
  distribution scripts, desktop launchers, `make-dist.sh` release pipeline
  with clean-extract re-verification, optional Windows PyInstaller spec,
  optional `.deb` builder.
* Full documentation set (README, ARCHITECTURE, API_REFERENCE,
  DEVELOPER_GUIDE, DEPLOYMENT, FILE_TRANSFER_PROTOCOL,
  HARDWARE_COMPATIBILITY, BUILD, CHANGELOG, TEST_REPORT, BUILD_LOG,
  KNOWN_LIMITATIONS, DEVELOPMENT_BACKLOG, LICENSE).
* Test suite: 27 unit tests, 11 node DSP tests, 24 real-network integration
  tests (real companion subprocesses incl. interruption/resume/corruption
  scenarios).

### Fixed during hardening (found by the test suite)

* Same-second hash-cache staleness (`mtime` → `mtime_ns` keying).
* Token-bucket over-issuing budget across sleeps.
* Unnamed regex groups breaking `/{id}` routes (jobs/peers/uploads/groups).
* Rename-conflict finalizing at the original path instead of `(1)` copies.
* `PeerClient` receiving a token string instead of a callable.
* Manual registration probing the default port only + self-registration.
* Missing `uptime_s` in `/api/identity`.
* Missing auth-token generation when `require_token` is enabled at runtime.
