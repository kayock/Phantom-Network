# LOCALFORGE_HANDOFF.md

Hand-off document for **LocalForge**, the local multi-agent development
platform that maintains PHANTOM NETWORK from **v1.2.1** onward.

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
  __init__.py         __version__ ("1.2.1") + ports (single source of version truth)
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
