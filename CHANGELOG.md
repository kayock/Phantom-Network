# Changelog

## [1.4.1] — Clipboard repair

### Root causes
- Desktop Clipboard UI called undefined `loadTargets()` (copy-paste from
  Broadcast), which threw on every open and broke send/target selection.
- Windows `os_copy` appended the payload as an extra PowerShell argv; 
  `$input | Set-Clipboard` never received stdin, so native capture/write
  appeared active but left the OS clipboard unchanged.
- Ubuntu companions on Wayland had no `wl-clipboard` helper and SSH-started
  processes lacked `WAYLAND_DISPLAY` / `XDG_RUNTIME_DIR`, so Station
  reported “no native helper”.
- Enabling auto-sync in Settings did not start the poller until restart.
- Incoming peer clipboard entries were stored in history but not written
  to the local OS clipboard, so paste-on-the-other-computer failed.

### Corrections
- Define `loadTargets()` in `clipboard.js`; add `/api/clipboard/os-paste`;
  fix “From OS clipboard” to read via helper instead of calling os-copy.
- Windows clipboard read/write via stdin-piped PowerShell; absolute
  `powershell.exe` discovery for schtasks contexts.
- Prefer Wayland helpers when the session is Wayland; resolve helpers from
  `~/.local/bin` and ship `scripts/install-clipboard-helper.sh` (user-local
  `.deb` extract, no sudo).
- Launcher imports graphical session environment from
  `systemctl --user show-environment`.
- `ensure_auto_sync()` starts the poller when auto-sync is enabled at
  runtime; incoming text is applied to the OS clipboard with fanout
  suppression to prevent sync loops / duplicates.
- Service worker cache bumped to `v1.4.1`.

## [1.4.0] — Phantom Signal (voice · broadcast · media · mesh · clipboard)

### Release hardening (post-zip)
- Signal Lab Waterfall: skip `drawImage` until the canvas has a non-zero
  layout size (was throwing `InvalidStateError` on first paint).
- Voice / Broadcast / Clipboard: reject non-object `targets` entries with
  HTTP 400 instead of a 500 AttributeError.
- `packaging/install-ubuntu.sh`: installer banner reports v1.4.0.
- Service worker cache bumped to `v1.4.0.1` so clients pick up the
  Waterfall fix without a manual cache clear.

### Phantom Voice (Priority A)
- New `companion/voice.py`: call registry + WebRTC signaling relay. Calls
  between computers, computers↔phones, phones↔phones; direct calls,
  group voice rooms, PTT preserved as the PCM intercom compatibility mode.
- Cross-companion calls: invites relay to the target companion, which
  keeps a MIRRORED call record; answer/signal/end actions forward to the
  origin companion (state owner). Verified by integration tests over real
  companion subprocesses.
- `web/js/lib/rtc.js`: LAN-first WebRTC (`iceServers: []`), explicit
  capture only, mute, device pickers, VAD level meter, jitter/loss
  quality, honest transport indicator (direct/stun/relay/FAILED).
- 45 s ring watchdog → `missed` + GhostChat system message + history.
- Desktop Voice view + mobile Voice tab with full-screen ring sheet.

### Phantom Broadcast (Priority B)
- `companion/broadcast.py`: announcements (text + tone + local TTS) to
  computers, phones, groups, everyone; seven built-in profiles + custom
  profiles; device groups; quiet hours (midnight-wrap, emergency override
  only via explicit emergency urgency); schedules (HH:MM, weekdays,
  DST-safe last-fired); per-target honest delivery states; acknowledgments;
  append-only history. Local voices only — no cloud TTS.

### Phantom Media (Priority B)
- `companion/media.py`: rooms, shared queues (PathSandbox-authorized
  paths only), play/pause/stop/seek/next/prev/repeat/shuffle, per-device
  volume with the 0.8 ceiling, clock-offset estimation, playback history.
- `/api/media/stream` — HTTP Range streaming with proper media MIME
  types from the OWNING companion (no duplication, no transcoding).
- Honest sync claim: ±50–150 ms on a LAN; the UI shows the measured
  offset. Desktop Media view + mobile Media tab.

### Phantom Mesh (Priority C)
- `companion/mesh.py`: exchange-introduced peers expire after 30 min
  without direct contact; known-peer recovery probes with exponential
  backoff (10 s → 60 s) and recorded last errors; duplicate-identity
  reconciliation (newest wins, audit events, manual peers never touched);
  optional pure-python mDNS/DNS-SD provider (off by default, probe-
  validated like every other source); EXPERIMENTAL mobile-host failover
  (claim + secret-hash + ledger merge, OFF by default, TTL-bound,
  newer claim wins, raw secrets never stored).
- Mesh diagnostics view: status, recovery errors, claims, audit trail.

### Phantom Clipboard (steer addition)
- `companion/clipboard.py`: shared clipboard with history (300 entries,
  retention), pins, search, clear; text (256 KiB) + image data-URL
  (2 MiB) caps; SHA-256 duplicate suppression; loop prevention
  (origin + hop, never re-broadcast, receive never sends); participation
  controls (auto-sync OFF by default, pause, private mode, per-device);
  native helpers (xclip/xsel/wl-clipboard/PowerShell/pbpaste) for
  dashboard-independent sync where the OS permits; phones sync manually.
- Desktop Clipboard view + mobile Clip tab.

### Advanced Signal Laboratory (Priority D)
- `web/js/lib/dsp.js` v2: Hamming(7,4) FEC (corrects 1 bit/word; frame
  CRC16 is the final arbiter), v2 modem frames (type 0x55) with FEC,
  ARQ NAK protocol + retransmit selection, deterministic simulation
  channels (seeded noise, bit corruption, packet drop, linear resample,
  clock drift), honest offline roundtrips.
- decodeModem accepts v1 (0x54) AND v2 (0x55) frames — backward
  compatible.
- Signal Lab UI: acoustic chat (v2) tab with ARQ, Morse practice tab
  (random sessions, accuracy scoring, Farnsworth), spectrum tools
  (markers, RMS/peak/dominant/sample-rate readouts, JSON export), and
  the previously-missing Waterfall and Modem tabs implemented (latent
  v1.3.1 defect found by browser testing: their tab buttons existed but
  the functions never did).
- Offline eval harness: `tests/unit/test_v140_dsp.py` runs the shipped
  JS under node — 18 tests across FEC vectors, SNR sweep, ARQ recovery,
  drift/rate-mismatch (must fail honestly), truncation, v1 compat.

### Optional screen sharing (Priority E)
- Browser-native getDisplayMedia inside a voice call, explicit
  participant action only, visible indicator, viewer card on the other
  side. No remote input control, no unattended capture.

### Mobile
- Voice/Media/Clip tabs + home quick actions; secure-context
  diagnostics in More; identity export + onboarding **Restore an
  existing identity** for HTTP↔HTTPS origin moves (secret-verified
  re-attach; bare-id takeover still refused). PWA shell, QR onboarding,
  offline cache, versioned SW (v1.4.0) preserved.

### HTTPS & deployment
- **Dual listeners**: `tls_dual: true` serves HTTP on the main port AND
  HTTPS on tls_port (+1) simultaneously; TLS failure never takes HTTP
  down. `/api/tls/status` (expiry, SANs, notes) and `/api/tls/cert`
  (PEM download for phone install). `scripts/gen-lan-cert.sh` adopted
  from v1.3.2.

### v1.3.2 corrections integrated (published baseline 78ea0f4)
- Discovery accepts `pn >= 1` (v1.3.1 advertised pn:2 but accepted only
  pn==1 → peers flapped offline after the 12 s TTL).
- Direct delivery whenever a peer has an address (stale presence never
  blocks a reachable peer); successful delivery refreshes presence.
- Mobile attachments upload into the DESTINATION computer's rw share.
- Conversations API is null-safe for remote-hosted phones (name from
  peer identity payloads).
- `prefer_lan_address`: RFC1918 preferred over CGNAT/Tailscale (backend
  + mobile + chat upload paths).
- Same-host local delivery for mobile clients talking to their host.

### GhostChat interface repairs (steer §3)
- Canonical phone conversation key `mobile:<client_id>` everywhere;
  legacy `direct:mob_*` rows merge at view level (stored messages
  untouched) — duplicate conversations fixed.
- Conversation titles resolve remote-hosted phone names; message-author
  name fallback; icon propagation; deterministic last-ts ordering.
- Desktop header repaints on every `current` change (fixes "Select a
  conversation" shown for an active conversation).
- Orphan (temporary test client) conversations hidden after 24 h
  without deleting history.
- Mobile conversation titles resolve mob_ authors from fleet payloads.

### Architecture hardening (steer §5)
- `PeerRegistry.verify_peer()` — probe + identity match before relaying
  calls/announcements/clipboard (60 s cache); an address alone is not an
  identity, stale presence is not unreachability.
- `PeerClient` accepts string OR callable token providers (a TypeError
  in the token path silently turned deliveries "unavailable" — found by
  the new integration tests, fixed).
- Voice invite routing falls back to local publish when a peer is
  unresolvable instead of dropping silently.

### Tests & tooling
- Unit: +44 tests (voice/broadcast/media/mesh/clipboard engines,
  conversation repairs, migration v2→v3 incl. clipboard flags). Suite:
  162 tests.
- Integration: `tests/integration/test_v140_signal_e2e.py` — 11 tests
  over real companion subprocesses (deterministic registration): voice
  mirror cycle, broadcast delivery + honest unavailable, clipboard
  delivery + suppression + private mode, v1.3.2 routing regressions,
  dual-TLS live listeners, identity-across-origins.
- Shipped-JS suite `tests/js/test_v140.mjs` (14 tests): DSP v2 vectors,
  all-JS syntax sweep, view/nav/SW/topic wiring.
- Browser qualification `scripts/browser_test_v140.py`: 26/26 surfaces
  (17 desktop views incl. all 8 signal-lab tabs and practice/self-test
  interactions; 9 mobile surfaces incl. restore form), 0 genuine console
  errors, 27 genuine v1.4.0 screenshots in docs/screenshots/.
- Migration: schema 3 (v1.3.1 → v1.4.0) adds Phantom Signal + Clipboard
  flags with backup; state files preserved.

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
