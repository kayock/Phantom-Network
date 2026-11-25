# CHANGELOG.md

All notable changes to PHANTOM NETWORK.

## [1.3.2] — 2026-11-25

### Fixed
- **UDP discovery ignored every v1.3.1 beacon** because companions advertised
  `pn: 2` while receivers required `pn == 1`. Peers flapped offline after the
  12s TTL; GhostChat then failed with a red X ("No recipients online") even
  when the destination was reachable. Receivers now accept `pn >= 1`.
- **GhostChat delivery no longer requires a live "online" flag** when a peer
  has a known address — attempt delivery, then mark success/failure honestly.
- **GhostChat conversation sidebar 500** when history contained `mobile:…`
  keys for phones hosted on another companion (`client.name` on `None`).
- **Mobile chat attachments to a computer** now upload into that computer's
  authorized share instead of always landing on the phone's host.
- Prefer RFC1918 LAN addresses over CGNAT/Tailscale (`100.x`) when contacting
  multi-homed peers.
- Same-host mobile → host computer messages resolve locally (no peer hop).

### HTTPS
- Documented / prepared optional LAN HTTPS for iPhone microphone access.
  Certificates are generated on request; trust-store changes remain manual.
  HTTP on port 8415 stays the default until `tls_enabled` is set.

# Changelog

## [1.3.1] — Mobile Stabilization

### Security
- **Mobile identity protection**: re-attaching an existing client id now
  requires its secret (HTTP 401 without, 403 on mismatch); the secret is
  disclosed exactly once, only at new registration; `_authenticate` fails
  closed for empty/absent secrets on every mobile endpoint
  (heartbeat, inbox, send, ack, mutate, stream, prefs). Regression suite:
  `tests/unit/test_v131_identity.py` (10 tests) + HTTP-level takeover
  refusal in the e2e suite.

### Fixed
- Mobile interface boot failure: a syntax error (stray parenthesis) in
  v1.3.0's `web/js/mobile/app.js` prevented the entire mobile app from
  parsing — phones got a blank page. Found by v1.3.1 browser automation.
- Host→phone message pushes carried the sender-side copy (`outgoing: true`,
  un-rekeyed conversation): phones saw their host's messages as their own
  outgoing mail under the wrong conversation. Recipient-copy semantics now
  match the cross-host path.
- `peer_probe` probed the LOCAL api port on the peer's address first —
  same-host multi-companion setups falsely matched "this device" and
  custom-port peers never refreshed their identity (hosted mobile clients
  stayed invisible to deterministic discovery).
- Automation run history self-reported failure: `_record` logged a
  nonexistent key (`KeyError('last_result')` caught and recorded as the
  run error) — every v1.3.0 automation run failed internally.
- Receipt routing no longer depends on the cached online flag (stale TTL
  dropped delivery/read receipts on quiet networks); queued mobile
  messages retry on probes, not only on beacons.
- Same-host phone→phone conversations are rekeyed identically to the
  cross-host path; JS test runner bug (await in non-async) fixed;
  schema_version now visible immediately after first-run migration.

### Tests & verification
- 19/19 mobile e2e integration tests (all 13 comms flows, deterministic —
  no UDP dependence), 10 identity unit tests, installer regression split
  into an honest env-gated skip + a deterministic variant, 17-check JS
  suite, focused transfer-engine regression (2 known sandbox-timing
  failures, identical on pristine v1.2.2).
- Browser automation: 13 fresh v1.3.1 screenshots (desktop + mobile
  390×844 + offline shell); service worker update path verified live.

### Preserved
- Transfer engine byte-identical to the physically verified v1.2.2.
- No new authentication, pairing or access gates; QR registration flow
  unchanged and verified in-browser.

## [1.3.0] — PHANTOM MOBILE

### Independent mobile clients (Priority A)
- `companion/mobile.py` — browser clients register as independent logical
  participants (`kind: "mobile"`) hosted by a companion: persistent identity
  (`mob_*` id + browser-stored secret), name/type/icon, heartbeat presence
  with honest offline marking, per-client persistent inbox
  (`mobile_inbox_<id>.jsonl`), notification prefs. No phone runs a server.
- QR + manual registration: Settings → **Add Mobile Device** renders a QR
  (vendored MIT `qrcode-generator`, generated locally, no external service)
  pointing at the mobile interface `/m`. No pairing codes or tokens added;
  the QR carries no secrets. Duplicate-identity handling, rename, remove,
  recovery (fresh identity) are explicit UI actions.
- Dedicated mobile interface `web/mobile.html` + `web/css/mobile.css` +
  `web/js/mobile/app.js`: bottom navigation (Home / Chat / Devices / Files /
  More), large touch targets, onboarding, chat with attachments and voice
  notes, device fleet with presence, file send into authorized shares with
  chunked resumable-style uploads, camera capture hint, portrait+landscape.
- GhostChat 2.0: `to_kind: "mobile"` routing through hosts, delivery is
  honest (**delivered only after the phone acknowledges**), replies,
  reactions, message editing, deletion with tombstones, duplicate-message
  prevention (idempotent `/api/chat/incoming`), offline queueing incl.
  unknown-host queueing with prompt retry on fleet updates, read receipts
  computer↔phone and phone↔phone across hosts.
- Chat file store (`data_dir/chat_files`, capped by
  `mobile_max_attachment_mb`): chunked upload sessions, SHA-256, streamed
  download with Range support; voice messages via MediaRecorder (60 s cap).

### PWA (Priority A)
- `web/manifest.webmanifest`, service worker `web/sw.js` (shell cache
  versioned `pn-shell-v1.3.0-p1`, **`/api/**` never cached**, offline shell
  `offline.html`), original-brand PNG icons (192/512 + maskable) generated
  from the logo geometry, app shortcuts, update detection with reload toast.

### Discovery (Priority A/B)
- Discovery diagnostics ring buffer + `GET /api/discovery/diagnostics`
  (interfaces, beacon targets, rx/tx, last RX, manual peers, unknown
  senders, per-event log) with a Diagnostics panel in the UI.
- Peer exchange: companions share known-peer lists (`POST
  /api/peers/exchange`) — every forwarded record is validated by a real
  probe before it enters the registry.

### Fleet / monitoring (Priority B)
- Fleet Monitor "All Devices" overview; storage views `useful` (default,
  hides Snap/virtual) / `physical` / `all` with read-only + removable
  badges (`GET /api/storage`); Intel/AMD GPU identity via sysfs drm plus
  NVIDIA via nvidia-smi — **unavailable telemetry is reported, never
  fabricated**; physical vs logical CPU cores.
- Monitoring alerts engine (`companion/alerts.py`): 8 rule types,
  threshold + hold duration + cooldown + duplicate suppression, delivery
  via Event Center, 🔔 System Alerts chat conversation and optional sound
  cue. Nothing enabled by default.
- Network Radar: capability badges (shares/chat/audio/intercom/hosts
  phones/telemetry), mobile-client nodes with distinct rendering, search +
  computer/mobile filters, honest quick actions.

### Automation (Priority C)
- Watched folders + scheduled one-way copy jobs (`companion/automation.py`)
  executed through the **existing verified transfer engine**; additive only
  (never mirrors deletions, never prunes backups), preview, run-now,
  pause/resume, per-run history. Settings → Watched folders & scheduled jobs.

### Interface (Priority C/B)
- Command palette (Ctrl/Cmd+K): views, devices, conversations, commands.
- Media Party: fleet-wide scheduled playback via SSE with per-browser
  clock-offset scheduling (honest ±0.1–0.3 s, not sample-accurate), stop-all.
- Signal Lab 2.0: waterfall spectrogram (range/history/pause/markers/peaks),
  advanced Morse (WPM + Farnsworth per ARRL, timeline, practice mode),
  packetized acoustic modem (sync/seq/total/len/CRC-16 framing, three data
  rates, retry of missing packets, offline roundtrip tests), RMS/peak/
  duration analysis tools. Intercom input/output device selection. File
  Commander extension filter. Audio Command Media Party + stop.

### Platform
- Optional TLS (`tls_enabled`, `tls_cert`, `tls_key`) for secure-context
  features; off by default, never self-configured (see HTTPS_SETUP.md).
- Windows build CI workflow (`.github/workflows/build-windows.yml`) —
  delivered, awaiting execution on a real runner. Optional tray companion
  script `tools/tray_phantom.py` (pystray optional).
- Backward-compatible upgrade: versioned data migration with automatic
  backup to `migrations/`, `schema_version` marker, forward-compatible
  stores, protocol 2 identity payload with v1 peers still supported.

### Preserved
- The v1.2.2 transfer engine is **byte-identical** (`git diff` empty for
  `transfers.py` and `hashprep.py`): destination-pull, streaming, async
  checksum preparation, source identity checks, pause/resume, persistent
  state, SHA-256 verification, verified deletion, conflict resolution.

## v1.2.2
- Asynchronous checksum preparation (cold-cache 8 GiB qualification).
- See git history for v1.2.x.
