# DEVELOPMENT_BACKLOG.md

A practical, prioritized backlog for the follow-up multi-agent development
platform. Items are grouped by theme with rough effort (S/M/L) and the
modules they touch, so separate agents can work in parallel without
stepping on each other.

## P1 — reliability & scale (next release)

| # | Item | Effort | Modules | Notes |
|---|---|---|---|---|
| 1 | WebSocket transport alongside SSE for lower-latency chat/intercom | M | httpd, api.js | SSE stays as fallback; feature-detect |
| 2 | Content-hash-based sync (streamed SHA-256 diff instead of size compare) | M | transfers, UI sync dialog | reuse `/api/fs/hash` + cache |
| 3 | Audited delete-sync mode (two-step: preview → explicit per-file confirm → apply) | M | transfers, UI | `delete_confirmed` flag already reserved |
| 4 | Browser upload resume (persist upload sessions server-side, Range append) | S | fsops.UploadSessions | |
| 5 | Transfer scheduler priorities (pause-all, per-job priority) | S | transfers | |
| 6 | Watched-folder auto-sync (inotify/ReadDirectoryChangesW) | L | new watcher.py | opt-in per share pair |
| 7 | Multi-source directory pulls (parallel files, configurable) | M | transfers._run_pull_dir | keep per-job concurrency cap |

## P2 — communications

| # | Item | Effort | Modules |
|---|---|---|---|
| 8 | End-to-end encryption for chat (per-pair X25519 keys exchanged via QR/manual pinning) | L | chat.py, crypto.py (new) |
| 9 | WebRTC upgrade for intercom (LAN-only SDP exchange, no STUN) | L | intercom.py, UI |
| 10 | Voice-activity + level meters in the UI; inline audio messages in chat | M | intercom, chat, UI |
| 11 | Message reactions / edit / delete (with tombstones) | M | chat.py, UI |
| 12 | Notification relay (companion emits OS notifications for incoming chat) | S | new notifier.py (Win toast / Linux notify-send) |

## P3 — radar & monitoring

| # | Item | Effort | Modules |
|---|---|---|---|
| 13 | ARP-based discovery (via `ip neigh` / GetIpNetTable) for cross-subnet hints | M | discovery.py |
| 14 | mDNS/DNS-SD advertisement (`phantom._tcp`) | M | discovery.py |
| 15 | Topology view: switch-level layout from latency clustering | M | radar.js |
| 16 | AMD/Intel GPU telemetry (rocm-smi, intel_gpu_top polling) | S | monitor.py |
| 17 | Alert rules (CPU > 90 % for N min → chat broadcast + event) | M | monitor.py, events.py |

## P4 — UI/UX

| # | Item | Effort | Modules |
|---|---|---|---|
| 18 | Command palette (Ctrl+K) with fuzzy nav + device actions | M | app.js |
| 19 | Dark/light theme toggle (dark is default) | S | theme.css |
| 20 | i18n scaffold (en first) | M | new i18n.js |
| 21 | Keyboard navigation & ARIA audit for all views | M | views/* |
| 22 | File Commander: dual-pane sync scrolling, split-resize, column presets | M | commander.js |
| 23 | Waterfall/spectrogram view in Signal Lab | M | signal.js, dsp.js |
| 24 | PWA manifest + offline shell for phone dashboards | S | index.html, sw.js |

## P5 — packaging & ops

| # | Item | Effort | Modules |
|---|---|---|---|
| 25 | Windows CI job building `companion.exe` via PyInstaller | M | packaging |
| 26 | Auto-update channel (signed manifest, opt-in) | L | new updater.py |
| 27 | System tray companion (pystray) with quick enable/disable toggles | M | new tray.py |
| 28 | Structured JSON logs + log shipping toggle (off by default) | S | logging_setup.py |
| 29 | Benchmark suite as a pytest module (throughput report → BUILD_LOG) | S | tests |

## Engineering notes for implementers

* Every new endpoint: handler in `app.py` + route in `routes.py` + API_REFERENCE.md entry + integration test.
* Keep the pure-stdlib guarantee: any new dependency needs a graceful fallback and a KNOWN_LIMITATIONS.md entry.
* Transfer/protocol changes must update FILE_TRANSFER_PROTOCOL.md and pass the corrupting-proxy test pattern.
* UI additions: `render(container) → cleanup` contract; every visible control must do something real (no decorative controls).


---

# v1.3.0 outcome + next backlog

Delivered in v1.3.0: mobile identity/QR/relay, GhostChat 2.0, PWA, mobile
UI, mobile file sharing, discovery diagnostics + peer exchange, migration,
fleet overview + storage views + GPU honesty + capability badges, alerts,
watched folders/schedules, palette, Media Party, waterfall + advanced
morse + acoustic modem, intercom device pickers, optional TLS, Windows CI
workflow (unexecuted), tray script (optional).

Deferred / next (in order):
1. WebRTC intercom for low-latency phone voice (signaling via companion
   SSE; PCM/SSE stays as fallback). Physical mic verification first.
2. iOS Safari microphone + PWA push on real devices (needs trusted TLS).
3. Windows exe via the delivered CI workflow; MSI/NSIS installer.
4. mDNS/DNS-SD discovery as an optional mechanism.
5. Group avatars + typing indicators; per-conv mute server-side.
6. Encrypted transport research (documented proposal only — no custom crypto).
7. Mobile UI: per-conversation notification prefs mirrored to host.
8. Command palette actions registry (pluggable commands).
