# LOCALFORGE_HANDOFF.md

Hand-off document for **LocalForge**, the local multi-agent development
platform that maintains PHANTOM NETWORK from **v1.3.0** onward.

Purpose: let a small coding model work on **one component** with only this
document plus the component's source file — without loading the whole
project. Every suggested assignment below names its files, its contract,
its test command and its acceptance criteria.

---

## 1. Architecture in one paragraph

Phantom Network is a peer-oriented companion application: a pure-Python
stdlib service (`companion/`) runs on every participating machine and
serves a no-build vanilla-JS single-page UI (`web/`). Companions find each
other via UDP beacons, talk over HTTP/1.1 + SSE, move files directly
between each other (destination-pull), and never require a central server.
State lives in JSON files under a per-machine data directory.

## 2. Source directory map

```
companion/            Python package — the ENTIRE backend (run: python -m companion)
  __init__.py         __version__ ("1.3.0") + ports (single source of version truth)
  __main__.py         entry point
  app.py              App wiring: one class, one method per REST handler
  routes.py           route table (METHOD, regex) — read this to find endpoints
  httpd.py            threaded HTTP server, router, SSE writer, FsError → JSON
  config.py           config.json store, safe defaults, portable mode
  events.py           event bus + events.jsonl persistence
  discovery.py        UDP beacons, manual registration, /24 sweep, latency
  peers.py            peer registry (known_peers.json) + PeerClient (HTTP to peers)
  fsops.py            SECURITY SANDBOX + listing/ops/preview/zip/upload sessions
  transfers.py        job engine: pull/pull_dir, SHA-256, resume, moves, sync
  chat.py             message store, relay, outbox, receipts
  audio.py            WAV synthesis + platform players + safety caps
  monitor.py          telemetry (psutil → /proc → ctypes → None)
  intercom.py         PTT PCM relay
  errors.py           FsError(status, code, message) — the only domain error
  util.py             hashing, atomic JSON, identity payload
web/                  browser UI (ES modules, no build step)
  index.html          shell (sidebar + 12 view slots)
  css/theme.css       visual identity — DO NOT REDESIGN, only extend
  js/app.js           boot, hash router, SSE wiring, first-run wizard
  js/api.js           REST client for ANY device + SSE hub
  js/state.js         client store (self/peers/config/favorites)
  js/ui.js            DOM builder, formatters, toasts, modals, context menus
  js/lib/dsp.js       Morse + Goertzel + FSK codec (NO DOM deps → node-testable)
  js/lib/sfx.js       WebAudio synth for local playback
  js/views/*.js       12 views, each exports render(container) → cleanup|null
tests/
  conftest.py         Companion subprocess launcher + wait_job_done helper
  unit/               33 fast tests (no network)
  integration/        24 + 7 real-network tests (companion subprocesses)
  js/test_dsp.mjs     11 node tests against the shipped dsp.js
packaging/            installers, uninstallers, spec, make-deb, make-dist
scripts/              gen_sounds.py (deterministic WAV presets)
sounds/               8 original preset WAVs (generated, byte-stable)
```

## 3. Component responsibilities & ownership boundaries

| Component | Owner files | Other files it may touch | Never touches |
|---|---|---|---|
| HTTP layer | httpd.py, routes.py | errors.py | any service module |
| Config | config.py | — | httpd.py |
| Discovery | discovery.py, peers.py | util.py | transfers.py |
| Sandbox/FS | fsops.py, errors.py | config.py | chat.py |
| Transfers | transfers.py | fsops.py, peers.py, errors.py | httpd.py internals |
| Chat | chat.py | peers.py, config.py | fsops.py |
| Audio | audio.py | errors.py | everything network |
| Monitor | monitor.py | util.py | transfers.py |
| Intercom | intercom.py | peers.py | fsops.py |
| UI view X | web/js/views/x.js | ui.js helpers only | companion/*.py |
| DSP | web/js/lib/dsp.js | — (keep DOM-free!) | — |

**Rule:** if your change crosses two owner boundaries, split it into two
commits/PRs — one per component.

## 4. Important interfaces (stable contracts)

### 4.1 Handler contract (backend)
Every route handler is `def handler(req: Request) -> Response` in `app.py`.
Raise `FsError(status, code, message)` for expected failures — httpd.py
converts it to `{"ok": false, "error": code, "message": ...}` with that
status. Never return raw exceptions. Error codes are listed in
`DIAGNOSTIC_EXPLANATIONS` (app.py) — new codes need an entry there.

### 4.2 Job schema (transfers.py)
```python
job = {id, kind: pull|pull_dir, src_base ("self"|URL), src_path, dest_path,
       conflict: rename|skip|replace|compare, delete_source: bool,
       size, transferred, status, verify:{expected,actual,status},
       speed_bps, avg_bps, eta_s, error, files_done, files_total,
       current_file, created, started, finished, retries,
       src_identity:{size,mtime_ns}, source_changed: bool, move_error}
status ∈ queued|running|verifying|paused|interrupted|
         completed|completed_partial|failed|cancelled
```
Terminal states: completed, completed_partial, failed, cancelled (moved to
history). `completed_partial` = destination finalized & verified, source
intentionally retained (see §5 invariants).

### 4.3 SSE topics
`event` (Event Center record) · `chat` (full message) · `transfer`
(progress ~1 Hz) · `intercom` (base64 Int16LE 16 kHz PCM chunks).
A payload change must keep old fields present (append, don't rename).

### 4.4 View contract (frontend)
```js
export async function render(container) { …; return cleanupFnOrNull; }
```
Views get a fresh empty `container` on every navigation; cleanup must stop
timers/streams. Use `ui.js` helpers; never `innerHTML` with unescaped user
data; never add new CSS outside theme.css conventions.

## 5. File-transfer invariants (data-preservation law)

Also high-risk: `companion/hashprep.py` and `_await_source_hash` in `transfers.py` (long-running source checksum prepare/poll).

1. Nothing is ever loaded fully into memory: 1 MiB chunk boundaries.
2. Destination is written to `<dest>.part` and atomically renamed ONLY
   after `size matches` AND `SHA-256 matches`.
3. `verify.status == "ok"` is required before any job reports completion.
4. On checksum failure the `.part` is preserved as `.part.corrupt`; the
   destination is NOT created; the source is untouched.
5. Resume continues from `.part` byte length — but ONLY if the source
   identity `(size, mtime_ns)` still matches `job.src_identity`. Mismatch →
   fail with `Source file changed…`, keep the `.part`. The disk-space
   pre-flight on resume checks only the REMAINING bytes (size − offset).
6. **Verified deletion (v1.2.0 law)**: `verified_delete` NEVER reads the
   checksum cache. It (a) refuses symbolic links, (b) captures the file
   identity `(st_dev, st_ino, size, mtime_ns)`, (c) re-hashes the CURRENT
   content, (d) re-checks the identity immediately before unlink. Any
   mismatch → FsError → the move reports `completed_partial` with the
   source preserved. Cache use is allowed ONLY for non-destructive work
   (compare-conflict, sync previews).
7. Moves: source deletion happens ONLY via `verified_delete`. Delete
   failure → `completed_partial` (never plain `completed`).
8. Directory enumeration is paginated (`skip/limit/next_offset`); trees
   beyond `max_dir_files` are REFUSED (`tree_too_large`).
9. Windows destinations: names are pre-validated
   (`fsops.windows_name_problem`) — reserved/invalid names fail explicitly
   (`invalid_dest_name`); never rename, skip or overwrite silently.
10. Sync never deletes; previews compare per `size`/`mtime`/`hash`; every
    applied copy is hash-verified regardless.

Any code change that can violate 1–10 must be rejected. Changes to
`verified_delete` are HIGH-RISK destructive-operation code: they require a
reviewer-visible diff of that function plus green
`tests/unit/test_v12_regressions.py::test_stale_cache_*` before merge.

## 6. Data-preservation requirements (outside transfers)

* Config lives in `<data_dir>/config.json` (XDG: `~/.config/phantom-network`);
  write ONLY via `atomic_write_json`. Logs/state for installers:
  `~/.local/state/phantom-network` — launchers NEVER write into the
  program directory (`/opt/phantom-network`, `%LOCALAPPDATA%\PhantomNetwork`).
* Administrative endpoints (`/api/config`, `/api/fs/share|unshare`,
  `/api/audio/enabled`) are local-machine-only via `App._admin_guard`
  (`admin_local_only`, default true). New admin-ish endpoints MUST call
  `_admin_guard(req)` first — peer operations must not gain silent
  configuration privileges.
* `events.jsonl` / `messages.jsonl` are append-only logs; trimming must
  preserve the newest 5000/20000 entries.
* Installers/uninstallers must never `chown`/`rm` anything outside their
  own installed paths (see packaging/install-ubuntu.sh comments) and must
  never delete shared-directory contents.

## 7. Test commands (predictable)

```bash
python -m pytest tests/unit -q                  # ~10 s — run on EVERY change
node tests/js/test_dsp.mjs                      # ~1 s — DSP changes only
python -m pytest tests/integration -q           # ~8 min — real companions
python -m pytest tests/integration/test_v11_regressions.py::test_sync_same_size_different_content -q   # focused
python -m pytest                                # everything
```
The large-file test auto-skips when disk < 6 GB. Integration tests need no
docker/network; they bind 127.0.0.1 random ports.

## 8. Known limitations (see KNOWN_LIMITATIONS.md for the full list)

* Windows paths/telemetry/audio are source-reviewed, not hardware-verified.
* Same-host UDP discovery is unreliable (SO_REUSEPORT) — use manual
  registration; tests do this via `conftest.Companion.register`.
* Intercom needs a secure context for the microphone (HTTP LAN = blocked by
  browsers; documented workarounds only).
* The hash cache keys on `(size, mtime_ns)`; coarse-mtime filesystems can
  theoretically serve a stale hash in the `compare` conflict mode.
* No full 50 GB transfer was executed in CI (disk-limited); 2.6 GiB real
  transfer + >4 GiB Range boundary probe cover the offset logic.

## 9. Suggested small assignments (each ~one focused session)

Format per assignment: **Goal** · **Files** (owner + allowed-to-change) ·
**Required tests** · **Acceptance criteria** · **Reviewer evidence**.

| # | Goal | Files (owner / allowed) | Required tests | Acceptance criteria | Reviewer evidence |
|---|---|---|---|---|---|
| A1 | `GET /api/fs/recent` — newest N modified files in a share | owner: app.py, routes.py / + API_REFERENCE.md | new unit test | handler ≤ 30 lines; newest-first; docs updated | diff + test output |
| A2 | Surface `enumerate_progress` in Transfer Center | owner: transfers.js / + ui.js helpers | manual + console-clean check | cell shows `enumerated N/total` while manifest incomplete | screenshot + console log |
| A3 | History clear with `before_ts` filter | owner: app.py, routes.py / + API_REFERENCE.md | new unit test | older removed, newer kept | diff + test output |
| A4 | Radar capability badges (audio/intercom/shares) | owner: radar.js | manual | muted badge when `audio_enabled === false`; no layout shift on null flags | screenshot |
| A5 | Farnsworth spacing in the Morse lab | owner: signal.js, dsp.js (keep DOM-free) | node tests/js/test_dsp.mjs | existing tests unchanged-green; timeline reflects spacing | test output |
| A6 | Expose `limiter_tokens` in `/api/stats` | owner: transfers.py, app.py | new unit test | field ≥ 0; monotonic refill | test output |
| A7 | Chat dedupe by message id (idempotent relay) | owner: chat.py | new unit + existing integration | duplicate receive → single stored message | test output |
| A8 | Diagnostics "beacon TX increasing" check | owner: app.py (diagnostics_run) | manual | passes when discovery on, hint when off | screenshot |
| A9 | Keyboard focus outlines (.nav-item, .fc-list tr) | owner: theme.css | manual | 2px violet outline on Tab; no other visual change | screenshot |
| A10 | Auto-generate endpoint table from routes.py | owner: scripts/gen_api_docs.py (new) / + BUILD.md | self-check | generated table == routes.py exactly | script output |
| A11 | Wire `_admin_guard` into any NEW config-changing endpoint | owner: app.py, routes.py / + API_REFERENCE.md | extend tests/integration/test_v12_integration.py | remote POST → 403 admin_local_only; local POST → ok | diff + test output |
| A12 | Extend `windows_name_problem` (e.g. ADS names, reserved-with-ADS) | owner: fsops.py / + tests/unit/test_v12_regressions.py | parametrized unit tests | new rules covered; existing cases unchanged | test output |

**HIGH-RISK rule:** anything touching `verified_delete`, `_run_pull`
finalization, `PathSandbox.authorize`, or installers/uninstallers is NOT a
small assignment — it needs the owner's explicit sign-off, the full
integration battery, and a written rationale referencing invariants §5/§6.

## 10. Workflow for a LocalForge agent

1. Read this file + the owner file(s) of your assignment only.
2. Run the focused test command BEFORE your change (baseline green).
3. Make the smallest change that satisfies the acceptance criteria.
4. Re-run the focused test + `tests/unit`.
5. Update the docs listed in the assignment (API_REFERENCE.md or similar).
6. Never bump `__version__` (release engineering owns it), never edit
   `theme.css` identity tokens (`--violet`, `--bg0`…), never touch
   `packaging/` installers without the safety checklist in §6.


## Development source (Station)

- Development tree: `<phantom-network-source>`
- Installed application (do not edit for LocalForge work): `<phantom-network-install>`
- Runtime config: `~/.config/phantom-network`
- Runtime logs: `~/.local/state/phantom-network`

### Test commands

```bash
cd <phantom-network-source>
python3 -m venv .venv && .venv/bin/pip install -q pytest pytest-timeout
.venv/bin/python -m pytest tests/unit -q
node tests/js/test_dsp.mjs
.venv/bin/python -m pytest tests/integration -q
.venv/bin/python -m pytest -q
```

Future LocalForge changes must pass these against the development tree before
any copy into the installed application directory.



---

# v1.3.0 ownership map (for LocalForge agents)

| Component | Files | Contract notes |
| --- | --- | --- |
| Mobile registry | companion/mobile.py | clients.json; secret ONLY in registration response; inbox ring per client |
| Mobile HTTP | app.py::mobile_* + routes.py | auth=False + client_secret in body; admin rename/remove |
| Chat 2.0 | companion/chat.py | mobile ack → ack-delivery routing; mutations.jsonl replay; ChatFileStore cap |
| Fleet identity | app.py::identity + peers.py | kind/mobile_clients/capabilities; protocol 2, v1 accepted |
| Discovery diag | companion/discovery.py | ring buffer ≤240; peer exchange probes before trusting |
| Alerts | companion/alerts.py | rules in config; cooldown/duration; delivery via events+chat+sse |
| Automation | companion/automation.py | jobs in config; EXECUTES ONLY via TransferEngine; no deletes |
| Storage views | companion/monitor.py | mounts(view), gpu_info (no fabricated values), cpu_topology |
| PWA | web/sw.js, manifest, offline.html | never cache /api/**; version-stamped caches |
| Mobile UI | web/mobile.html + js/mobile/app.js | localStorage identity; 5 tabs; chunked uploads |
| Palette | web/js/app.js::wirePalette | Ctrl/Cmd+K |
| Signal Lab | web/js/views/signal.js + lib/dsp.js | modem framing stable: [sync2,type,seq,total,len,payload,crc2] |
| Migration | companion/migration.py | SCHEMA_VERSION=2; backup before touch; idempotent |

Small maintenance assignments (suitable for small models):
1. Add an alert rule type `gpu_hot` reading `monitor.gpu_info()`.
2. Mobile UI: add a "recents" list to the Files tab (localStorage only).
3. Extend `MODEM_RATES` with a `slow` entry (150 ms) + offline test.
4. Radar: persist the last selected filter per device (uiPrefs).
5. Docs: keep API_REFERENCE.md in sync with any route change.
Acceptance for all: tests pass (`python -m pytest`, `node tests/js/*.mjs`),
no new dependencies, transfer engine untouched.

---

# v1.4.0 maintenance tasks (Phantom Signal)

Each task names allowed files, required tests, acceptance criteria and
reviewer evidence. High-risk areas (auth, destructive file operations,
installation) remain OUT of scope for LocalForge — see the exclusions
at the end.

## Task LF-14 · Voice session lifecycle
- **Allowed files**: `companion/voice.py`, `tests/unit/test_v140_backends.py`
  (voice section), `tests/integration/test_v140_signal_e2e.py`.
- **Contract**: `Call` states ringing → active → ended/missed/rejected;
  mirrored calls (origin_host set) MUST forward answer/signal/end to the
  origin and never mutate state locally beyond the mirror; ring watchdog
  expires at 45 s with a GhostChat record.
- **Tests**: `pytest tests/unit/test_v140_backends.py -k voice` and
  `pytest tests/integration/test_v140_signal_e2e.py -k voice`.
- **Acceptance**: all voice tests pass; no call left in `ringing` after
  60 s; the mirror forwards produce origin-side state changes.
- **Evidence**: test output + a diff limited to the allowed files.

## Task LF-15 · Broadcast profiles + schedules
- **Allowed files**: `companion/broadcast.py`, related unit tests.
- **Contract**: quiet hours parse "HH:MM-HH:MM" with midnight wrap;
  schedule times are validated HH:MM; emergency urgency overrides quiet
  hours ONLY for the sender; participation is never overridden.
- **Tests**: unit tests `-k broadcast`.
- **Acceptance**: `in_quiet_hours("22:00-07:00", 23:00) is True`,
  `(02:00) is True`, `(12:00) is False`; schedule save rejects "25:99".
- **Evidence**: test output.

## Task LF-16 · Media queue state machine
- **Allowed files**: `companion/media.py`, related unit tests.
- **Contract**: repeat-all wraps next AND prev past the ends; volume
  clamps to 0.8; `reorder` accepts only permutations; unauthorized
  paths raise 403 via the sandbox guard.
- **Tests**: unit tests `-k media`.
- **Acceptance**: all media unit tests green; no state persisted for
  unauthorized enqueues.
- **Evidence**: test output.

## Task LF-17 · Mesh diagnostics + recovery
- **Allowed files**: `companion/mesh.py`, `web/js/views/mesh.js`.
- **Contract**: exchange-introduced peers expire after 30 min without
  direct contact; recovery backoff doubles 10→60 s and records last
  errors; reconciliation deactivates the STALE duplicate (never manual
  peers). The failover claim path is FLAG-GATED — do not loosen the
  gate or store raw secrets.
- **Tests**: unit tests `-k mesh`.
- **Acceptance**: green mesh tests; `mobile_failover_enabled` defaults
  false in `config.py` and `migration.py`.
- **Evidence**: test output + grep evidence that the flag gate is
  intact.

## Task LF-18 · Mobile UI improvements
- **Allowed files**: `web/js/mobile/app.js`, `web/mobile.html`,
  `web/css/mobile.css`.
- **Contract**: every tab renderer exists in the `VIEWS` map; new SSE
  event types follow the `handleMobileEvent` dispatch; localStorage
  identity schema unchanged (client_id/client_secret/name/...).
- **Tests**: `node --check web/js/mobile/app.js` + the browser sweep
  (`scripts/browser_test_v140.py` mobile section).
- **Acceptance**: all 9 mobile surfaces load with zero pageerrors.
- **Evidence**: sweep output + screenshot diffs.

## Task LF-19 · DSP regression testing
- **Allowed files**: `tests/unit/test_v140_dsp.py`, `tests/js/`.
- **Contract**: FEC vectors, ARQ recovery, SNR sweep, honest-failure
  sims (48k-as-44.1k MUST fail) stay asserted; any dsp.js change
  requires updating BOTH the mjs suite and the Python harness.
- **Tests**: `node tests/js/test_v140.mjs` + `pytest
  tests/unit/test_v140_dsp.py`.
- **Acceptance**: 100% pass; no assertion weakened to make a failing
  scenario "pass".
- **Evidence**: both suite outputs.

## Exclusions (unchanged)
Authentication/identity (`mobile.py` secret rules, chat delivery ack
semantics), destructive file operations (`fsops.py` delete/move),
installation scripts, and the transfer engine's core loop are OUTSIDE
LocalForge scope. Route-level work touching `auth=False` endpoints
requires human review.
