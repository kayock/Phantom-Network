# ARCHITECTURE.md

PHANTOM NETWORK v1.0.0 — architecture, modules and protocol decisions.

## 1. Big picture

Phantom Network is a **peer-oriented companion architecture**, not a
client/server dashboard:

```
┌────────────────────────────── browser (any device) ──────────────────────────┐
│  SPA: js/app.js → views/* → api.js (REST + SSE)  ·  lib/dsp.js · lib/sfx.js  │
└───────────────┬──────────────────────────────────────────────────┬──────────┘
                │ HTTP/1.1 + SSE (same origin)                     │ HTTP (CORS)
┌───────────────▼──────────────┐                        ┌─────────▼─────────┐
│        companion (py)        │                        │  companion (py)   │
│  httpd.py ── routes.py       │                        │  same modules     │
│     ├── fsops.py (sandbox)   │      direct pull       │        │          │
│     ├── transfers.py ────────┼────── Range GETs ─────►│  fsops/transfers  │
│     ├── chat.py ─────────────┼──── POST /incoming ───►│  chat/intercom    │
│     ├── intercom.py ─────────┼──── POST /relay ──────►│  audio/monitor    │
│     ├── audio.py · monitor.py│                        │                   │
│  discovery.py (UDP 8414) ◄───┼─────── beacons ───────►│ discovery.py      │
│  config.py · events.py       │                        │ config · events   │
└──────────────────────────────┘                        └───────────────────┘
```

Key properties:

* **The companion is the product.** It runs headless, independently of any
  browser. The UI is a static SPA it serves — open the UI from *any*
  participant (or a phone) and you control the fleet through the same APIs.
* **No central bottleneck.** Large files move directly between the two
  companions involved (destination-pull). The machine showing the dashboard
  only orchestrates.
* **One port per machine** (default 8415/tcp) carries UI, API, SSE and
  transfer streams; 8414/udp carries discovery.
* **Push = SSE, not WebSocket.** One long-lived `GET /api/stream` per
  browser tab delivers chat, transfer progress, presence and intercom PCM.
  Browsers reconnect automatically; no socket library is needed.

## 2. Module map (companion/)

| Module | Responsibility | Key classes/functions |
|---|---|---|
| `app.py` | wiring, CLI (`python -m companion`), request handlers, static files, lifecycle | `App`, `main` |
| `routes.py` | route table: every `(METHOD, regex, handler)` | `register_routes` |
| `httpd.py` | threaded HTTP server, router, request/response types, SSE writer, FsError→JSON | `PhantomServer`, `Router`, `SSEHub` |
| `config.py` | persistent `config.json`, portable mode, change listeners | `Config`, `resolve_data_dir` |
| `events.py` | event bus + JSONL persistence + live subscribers | `EventBus` |
| `discovery.py` | UDP beacons, registry feeding, manual registration, subnet sweep, latency probes | `DiscoveryService` |
| `peers.py` | persistent peer registry (`known_peers.json`), HTTP client to peers | `PeerRegistry`, `PeerClient` |
| `fsops.py` | **security sandbox**, listing, ops, preview, zip, upload sessions | `PathSandbox`, `UploadSessions` |
| `transfers.py` | job queue, pull/pull_dir engine, SHA-256, identity-bound resume, move safety (completed_partial), rate limit, paginated manifests | `TransferEngine`, `RateLimiter` |
| `chat.py` | message store, relay to peers, groups, broadcast, outbox, read receipts | `ChatEngine` |
| `audio.py` | WAV synthesis, original presets, platform player, safety limits | `AudioService`, `render_notes` |
| `monitor.py` | telemetry (psutil → /proc → ctypes → None), history ring, nvidia-smi | `Monitor` |
| `intercom.py` | PCM relay sessions (talk → relay → SSE) | `IntercomService` |
| `errors.py` | shared `FsError(status, code, message)` | — |
| `util.py` | hashing, atomic JSON, ids, identity payload | — |

Frontend (`web/`):

| File | Responsibility |
|---|---|
| `index.html` | shell: sidebar, topbar, boot screen, modal/toast layers |
| `css/theme.css` | obsidian/graphite/violet theme, responsive to 375 px |
| `js/app.js` | boot, hash router, SSE wiring, badges |
| `js/api.js` | typed API client for *any* device + SSE hub |
| `js/state.js` | client store: self/peers/config/favorites/prefs |
| `js/ui.js` | DOM builder, formatting, toasts, modals, context menus |
| `js/lib/dsp.js` | Morse codec, Goertzel, FSK acoustic codec (no DOM deps → unit-testable in node) |
| `js/lib/sfx.js` | WebAudio preset/tone synthesis for local playback |
| `js/views/*.js` | one module per nav view (12 views), each exports `render(container) → cleanup` |

## 3. Data flows

### 3.1 Discovery
Every 3 s each companion broadcasts a JSON beacon on UDP 8414 (limited
broadcast + `239.255.80.80` multicast + configured directed-broadcast
addresses). Listeners dedupe by `device_id`, keep addresses ordered by
recency, and mark peers offline after 12 s of silence. A user-triggered
**sweep** TCP-probes the API port across the local /24 only, and manual
registrations are re-probed every 30 s. Transitions emit
`device_discovered / device_connected / device_lost` events.

### 3.2 File Commander
The browser calls `/api/fs/*` on *whichever device it wants to browse*
(CORS is allowed on `/api/*`). Every path resolves through
`PathSandbox.authorize()` (realpath containment inside a configured share;
write additionally requires `rw` mode). The UI never needs raw path typing:
device selector → shares/drives selector → breadcrumbs → favorites.

### 3.3 Transfers (see FILE_TRANSFER_PROTOCOL.md)
The UI creates a **pull job on the destination companion**. The destination
worker asks the source for size + cached SHA-256 (`/api/fs/hash`), streams
1 MiB `Range` chunks, hashes **while receiving**, fsyncs a `.part` file and
atomically renames only after checksum equality. Resume reuses the `.part`
byte offset. Moves delete the source only after a *verified-delete* round
trip. Progress is published on the `transfer` SSE topic every second.

### 3.4 GhostChat
`POST /api/chat/send` → sender companion relays `POST /api/chat/incoming`
to each target companion → each stores + fans out over SSE. Delivery =
recipient-companion ack; read receipts travel back via `/api/chat/ack-read`.
Offline recipients queue messages in `outbox.json` and retry every 20 s.
Groups are config-defined member lists; broadcast goes to every online
participating device.

### 3.5 Intercom
Browser captures 16 kHz mono Int16 PCM **only while PTT is held**, posts
base64 chunks (~60 ms) to `/api/intercom/talk`; the talker's companion
relays to each member's `/api/intercom/relay`; each receiving companion
publishes `event: intercom` over SSE; browsers play via a small jitter
buffer. Nothing is stored. Secure-context requirements are documented in
KNOWN_LIMITATIONS.md.

### 3.6 Audio Command
`/api/audio/tone|preset|file` renders PCM (stdlib `wave`) and plays via the
first available platform player: PowerShell `Media.SoundPlayer` (Windows),
`paplay` → `aplay` → `ffplay` (Linux). One playback at a time, hard caps:
amplitude ≤ 0.8, duration ≤ 30 s, 20 Hz–20 kHz, per-device enable switch.

## 4. Design decisions & rejected alternatives

| Decision | Why |
|---|---|
| Pure-stdlib Python companion | zero-install on Win+Ubuntu, no build step, no compiled deps; psutil optional |
| Threaded HTTP server | LAN scale (≤ ~20 clients) makes threads-per-connection simple, robust, ~0 % idle CPU; asyncio's complexity wasn't warranted |
| SSE over WebSocket | one-way push is all the UI needs; EventSource auto-reconnects; no dependency, trivially testable |
| Destination-pull transfers | one code path for local↔remote and remote↔remote; destination owns disk-space/conflict decisions; resume state lives with the `.part` file |
| Browser UI, no Electron | instant cross-platform reach incl. phones; companion startup scripts + .desktop/.lnk launchers give desktop convenience |
| JSONL stores (events, messages) | append-mostly workloads with cheap trimming; no DB dependency |
| Optional shared-token auth | LAN tool first; PSK adds protection on shared networks without an identity service |

## 5. Extension points (for the follow-up multi-agent platform)

* New API endpoint → add handler in `app.py`, register in `routes.py`.
* New job kind → implement `_run_<kind>` in `transfers.py`, extend
  `create_job` validation; job dicts are the wire format.
* New discovery mechanism → implement a `start/stop/status` service in
  `discovery.py` and call `registry.observe_beacon(identity, address)`.
* New UI view → drop `web/js/views/<name>.js` exporting `render(container)`,
  add one nav row in `index.html` + route in `app.js`.
* Message schema versions: beacons carry `pn` (protocol) and `version`;
  peer REST payloads are plain dicts documented in API_REFERENCE.md.

## 6. Failure & recovery model

* Companion crash → jobs persisted in `jobs_state.json`; on restart they
  become `interrupted` and resume from `.part` offsets.
* Peer crash → beacons stop → peer marked offline → events + UI badge;
  chat to it queues in the outbox.
* Network blips mid-transfer → 3 automatic retries with backoff, then
  `interrupted` (resumable via UI/API).
* Corrupted stream → SHA-256 mismatch → `.part` moved to `.part.corrupt`,
  job `failed`, destination never finalized, source untouched; event
  `verification_failed` recorded.
