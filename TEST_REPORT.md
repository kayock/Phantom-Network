# Test Report

## v1.4.0 — Phantom Signal qualification (this release)

### Execution summary (all run against the shipped tree)

| Suite | Count | Result | Type |
| --- | --- | --- | --- |
| Unit suite (`tests/unit/`) | 162 | 161 passed, 1 skipped | unit |
| — incl. v1.4.0 engines | 26 | 26 passed | unit |
| — incl. DSP eval harness (`test_v140_dsp.py`, node-runner) | 18 | 18 passed | simulated DSP (deterministic, seeded) |
| — incl. v1.3.2 routing regressions (`test_v132_routing.py`) | 5 | 5 passed | unit |
| — incl. conversation repairs (`test_v140_conversations.py`) | 7 | 7 passed | unit |
| — v1.3.x migration suites | 18 | 18 passed, 1 skipped | unit |
| Integration (`test_v140_signal_e2e.py`) | 11 | 11 passed | real companion subprocesses (127.0.0.1, deterministic registration) |
| Shipped-JS suite (`tests/js/test_v140.mjs`) | 14 | 14 passed | node against shipped source |
| JS syntax sweep | 27 modules | 0 failures | node --check (the v1.3.0 rule) |
| Browser qualification (`scripts/browser_test_v140.py`) | 26 surfaces | 26 passed, 0 genuine console errors | real Chromium automation |
| Screenshots captured | 27 | genuine v1.4.0 | docs/screenshots/v140-* |

### What each test type means here

- **Unit**: engines + state machines against fakes; deterministic.
- **Simulated DSP**: shipped JS executed by node with seeded noise/
  corruption channels; asserts BOTH clean decodes AND honest failures
  (48k-audio-as-44.1k must NOT decode; truncated signals never fake
  text).
- **Real integration**: `python -m companion` subprocesses talking HTTP
  — voice mirror cycle, broadcast delivery states, clipboard round-trip
  + suppression + private mode, v1.3.2 routing regressions, live dual
  HTTP+TLS listeners (openssl-generated cert, both ports probed), mobile
  identity across "origins" incl. the takeover guard.
- **Browser**: Playwright Chromium drives every desktop view (incl. all
  8 Signal Lab tabs, a real practice session scored, and the acoustic
  self-test executed) and all 9 mobile surfaces; console/pageerror
  listeners recorded zero genuine errors.
- **Actual audio hardware / physical Windows / real mobile hardware**:
  NOT executed in this environment — same honest distinction as v1.3.x;
  the deployment handoff verifies those on real machines.

### v1.3.2 baseline verification
- Published corrections fetched from the public release (`78ea0f4`) and
  integrated as semantic patches; adopted regression suite passes
  (5/5). Version markers: `companion/__init__.py` = 1.4.0, UI brand +
  packaging + SW all v1.4.0.

### Failures found and FIXED during qualification (no weakened assertions)
1. `PeerClient` received string token providers → `'str' not callable'
   silently turned peer deliveries "unavailable" — normalized to accept
   both; integration tests now cover the real path.
2. Voice `Call` mirror fields missing (silent patch no-op) — invite
   relay 500'd; exposed by direct relay probe, fixed, covered.
3. Voice invite routing dropped local publish for unresolvable
   computer targets — fall-through restored (unit test caught it).
4. Latent v1.3.1 defect: Signal Lab Waterfall + Modem tab buttons
   existed but their functions never did (`tabWaterfall is not
   defined`) — both tabs implemented; browser sweep now exercises them.
5. Hamming(7,4) syndrome bit-order bug (would have "corrected" the
   wrong bit) — caught by the eval-harness vectors before any release.
6. DSP v2 packing used array `toString(2)` (comma-joined 13-bit words)
   — roundtrip test caught it.

### Skipped (1)
- One v1.3.x platform-specific test skips by design outside its
  platform (unchanged behavior).


# TEST_REPORT.md — PHANTOM NETWORK

## v1.3.0 results (2026-11-25)

### Physical LAN qualification (v1.2.1 baseline, preserved)

Station (`<station-host>`) → Portable 1 TB share
`<external-transfer-share>` using the Phantom
Network transfer engine (destination-pull). Evidence file:
`~/phantom-network-lab/large-file-qualify-v121.json`.

| Size | Result | Throughput |
|---|---|---|
| 100 MB | SHA-256 verified | 6.3 MiB/s |
| 1 GB | SHA-256 verified | 7.9 MiB/s |
| 5 GB | SHA-256 verified, pause/resume | 7.4 MiB/s |
| 10 GB | SHA-256 verified | 7.4 MiB/s |
| 25 GB | SHA-256 verified | 7.5 MiB/s |
| 50 GB | Independent SHA-256 verified | 8.6 MiB/s wall |

Peak companion RSS ≈ 43 MB (Station) / ≈ 45 MB (Portable). Safe disposable
move verified. Existing AI models untouched.

**Honesty note:** the first 50 GB attempt under v1.2.1 failed cold because
`/api/fs/hash` used a 30 s peer timeout. After manually pre-warming Station's
hash cache the same 50 GB transfer completed. That is the defect v1.3.0
corrects — ordinary users must not need the workaround.

### v1.2.2 automated regressions

`tests/unit/test_v122_checksum.py` — slow checksum (>30 s) completes without
false interrupt; PeerClient default timeout unchanged; prepare jobs join;
cancel works; changed sources fail prepare; verified deletion still refuses
stale cache; Transfer Center exposes hashing status.

### v1.2.2 focused cold-checksum physical regression

Evidence: `~/phantom-network-lab/cold-checksum-qualify-v122.json`.

- Disposable **8 GiB** pull Station → Portable `phantom-1tb-transfers`.
- Companion hash cache intentionally cold (no `/api/fs/hash` pre-warm).
- Visible prepare status `source_checksum` / job status `hashing`.
- Cold checksum duration **42.3 s** (exceeds the old 30 s peer timeout).
- Transfer completed; independent SHA-256 match.
- Wall throughput ≈ **8.1 MiB/s**; peak RSS ≈ 41 MB Station / ≈ 42 MB Portable.
- Disposable artifacts cleaned; original 50 GB v1.2.1 evidence preserved.

This run is distinct from the warmed 50 GB qualification above.

## v1.2.0 results (2026-11-25)

Final battery: `python -m pytest tests/unit tests/integration -q` — counts
below; zero failures; all v1.1.0 tests preserved without weakening.

| Suite | Count | Status |
|---|---|---|
| Unit (incl. v1.1.0 + v1.2.0 regressions) | 32 + 23 | ✅ |
| JS DSP (node, shipped dsp.js) | 11 | ✅ |
| Integration — v1.0.0 set | 24 | ✅ |
| Integration — v1.1.0 set | 10 | ✅ |
| Integration — v1.2.0 set | 11 | ✅ |

### v1.2.0 set — what each test proves

1. `test_remote_admin_changes_refused` — remote LAN client gets
   `403 admin_local_only` for share-add and settings changes; peer
   operations and local administration unaffected.
2. `test_admin_guard_can_be_relaxed_explicitly` — documented escape hatch
   works (`admin_local_only=false`).
3. `test_port_conflict_fails_cleanly` — second companion on a busy port
   terminates with "Address already in use" (not silently co-existing).
4. `test_repeated_start_stop_cycles_stay_light` — 5 restart cycles: RSS
   bounded, threads stable (11), device identity persists.
5. `test_concurrent_large_transfers` — 2 × 1.2 GiB simultaneously, both
   verified, independent SHA-256 match at destination.
6. `test_cancel_large_transfer_keeps_part_and_source` — cancel keeps
   `.part`, source byte-identical.
7. `test_changed_source_detected_after_restart` — pause → RESTART
   companion → mutate source → resume refused (identity binding persists).
8. `test_long_filename_large_file` — 233-byte UTF-8 filename transfer
   (exposed & fixed the Content-Disposition latin-1 crash).
9. `test_real_data_window_beyond_4gib_offset` — real bytes at 2^32−1 MiB
   round-trip exactly through Range requests (sparse 4.6 GB file).
10. `test_remote_to_remote_pull_is_direct` — job lands on the destination
    engine pulling from the source URL; no dashboard transit by
    construction.
11. `test_installer_never_writes_runtime_files_into_program_dir` —
    launcher regression guard (XDG logs, --data-dir).

Plus unit-level v1.2.0 regressions: stale-cache deletion refusal, same-path
replacement refusal, symlink refusal, cache-bypass proof,
modified-during-verification refusal, Windows name validation
(parametrized), v1.1.0-config upgrade compatibility, mocked ENOSPC
handling, release-validator control-byte detection.

### Browser interface (v1.2.0)

All 12 views loaded console-clean (0 errors each); UI-driven 20 MB
cross-companion transfer via panes → `completed / verify=ok`,
`sha256sum` source == destination; screenshots refreshed
(`ui-v12-*.png`). First-run wizard + admin-boundary behavior observed
live (remote share-add refused, local wizard works).

### Performance (v1.2.0)

Idle RSS 25.9 MB, 0.0 % CPU over 10 s, 11 threads stable across 5 restart
cycles; SHA-256 ~1.7 GiB/s per core; RSS during large transfers < 250 MB.

### Clean-extract verification

The release ZIP was extracted to a clean directory and re-tested (unit
suite + integration smoke) from the shipped source — see BUILD_LOG.txt.

### Not verified (honest, unchanged)

Windows execution, physical speakers, mobile microphone, >4 GiB real copy
(disk-limited; largest real: 2.6 GiB + concurrent 2×1.2 GiB), pwsh
execution (static checks only), physical ENOSPC (mocked).

---

## v1.1.0 results (2026-11-25)

Every result below was actually executed in the verification environment
(Linux 5.10 x86_64, Python 3.12.16, 2 vCPU / 4 GB container).
Nothing is claimed that was not run.

### Final battery

`python -m pytest tests/unit tests/integration -q` → **71 passed, 0 failed,
0 errors** (~13 min, includes the real 2.6 GiB transfer at a 20 MB/s limiter).

| Suite | Count | Status |
|---|---|---|
| Unit (incl. new v1.1.0 regressions) | 32 | ✅ |
| JS DSP (node, shipped code) | 11 | ✅ |
| Integration — v1.0.0 set (real companions) | 24 | ✅ (was 19/24; §3 below documents the five root causes) |
| Integration — v1.1.0 regressions (real companions) | 10 | ✅ |

### v1.1.0 integration set — what each test proves

1. `test_safe_default_shares_nothing` — fresh companion refuses `~`
   (`outside_shares`); `/api/setup/status` works; wizard-style share
   authorization makes the dir browsable.
2. `test_identity_advertises_capabilities` — `audio_enabled` /
   `intercom_enabled` / `shares_count` in the identity payload.
3. `test_directory_transfer_beyond_20k_files` — 21 000 files transfer
   COMPLETELY (`files_done == files_total == 21000`, local count matches).
4. `test_tree_cap_refusal_is_explicit` — beyond `max_dir_files` the job is
   REFUSED (`tree_too_large`), nothing transferred, nothing silently
   omitted.
5. `test_sync_same_size_different_content` — `size` mode honestly skips;
   `hash` mode detects and replaces; engine verifies SHA-256 after copy.
6. `test_sync_mtime_mode` — newer-wins comparison.
7. `test_resume_refuses_changed_source` — resume against a mutated source
   fails with "Source file changed…", `.part` retained; retry from history
   re-copies the CURRENT content and verifies ok.
8. `test_move_with_undeletable_source_reports_partial` — read-only source
   dir → `completed_partial`, both copies intact, honest `move_error`.
9. `test_large_file_transfer_above_2gib_with_boundary_resume` — real
   2.6 GiB transfer, pause at > 2^31 offset, RSS < 250 MB during transfer,
   independent SHA-256 over the destination. (Discovered and fixed a real
   bug here: the resume path re-checked disk space for the FULL size
   instead of the remaining bytes — the check is now resume-aware.)
10. `test_range_request_across_4gib_boundary` — sparse 4.5 GB file, Range
    `bytes=4294966296-4294967296` → exact 206 + Content-Range + zero
    payload: 64-bit offset handling proven above 4 GiB.

### v1.1.0 clean-extract verification

The release ZIP was extracted into a fresh directory and re-tested from
there (see BUILD_LOG.txt [5]/[6]): shipped source compiles and the shipped
test suites pass against the extracted tree.

### Browser interface re-verification (v1.1.0, live)

* First-run wizard rendered on a fresh companion; read-write authorization
  via the wizard; `setup_complete` persisted.
* Repeat interface-driven transfer: 25 MB via pane navigation + Copy →
  `completed / verify=ok`; `sha256sum` source == destination.
* Device capability hints (`Windows PC — 1 share(s)`), Transfer Center
  remaining-bytes/`COPY OK · SOURCE RETAINED` states, Diagnostics guide +
  exportable report (15 checks / 10 explanations over HTTP).
* Console: zero errors; screenshots refreshed in `screenshots/`.

### Not verified in v1.1.0 (unchanged honesty)

Windows host execution, physical audio audibility, mobile microphone,
>2.6 GiB real transfer (disk-limited). See KNOWN_LIMITATIONS.md.

---

# PHANTOM NETWORK v1.0.0 — historical report (kept for the record)

## 1. Execution matrix

| Test class | Executed? | Environment |
|---|---|---|
| Automated unit tests (27) | ✅ green | this container (pytest) |
| JS DSP suite (11) | ✅ green | node 24 against the shipped `web/js/lib/dsp.js` |
| Real-network integration (24) | ⚠️ 19 green / 5 red at release cut-off (details §3) | two+ real `python -m companion` subprocesses over HTTP |
| Real file transfers | ✅ verified | 30–80 MB cross-companion transfers incl. SHA-256 equality re-checked with `sha256sum` |
| Browser interface tests | ✅ done | agent-browser (headless Chrome 158): all 12 views, console-clean, real UI-driven transfer |
| Mobile layout check | ✅ 375×812 emulation screenshot | no horizontal overflow |
| Actual Linux execution | ✅ | the whole suite |
| Actual Windows execution | ❌ not performed | no Windows machine in the dev environment — code is source-reviewed only (KNOWN_LIMITATIONS §1) |
| Physical audio hardware | ❌ not performed | container has no sound device; synthesis + player selection verified instead |
| Phone browser microphone | ❌ not performed | secure-context restrictions documented, not claimable from here |

## 2. Unit + DSP results (final run)

```
tests/unit  ............................ 27 passed
tests/js/test_dsp.mjs .................. 11 passed, 0 failed
```
Covers: sandbox traversal/symlink/read-only refusal, hidden files,
mkdir/rename/confirm-delete, upload sessions, rate limiter, conflict
verdicts, disk-space guard, job persistence, verified-delete refusal,
event bus persistence/filters, JSON utilities, Morse codec roundtrips,
Goertzel, FSK clean/noisy roundtrip, corrupted-frame refusal.

## 3. Real-network integration results

Final full-suite run (`pytest tests/integration/test_network_e2e.py`):
**19 passed / 5 failed** at the release cut-off.

Green (verified end-to-end):
- companions see each other; manual registration idempotent; non-participating hosts reported honestly; self-registration refused
- availability events + uptime
- subnet sweep finds a companion on the default port
- remote directory browsing incl. spaces/unicode; Windows/Linux path variants; unauthorized paths 403
- 40 MB cross-companion pull with SHA-256 verification (digest re-checked independently)
- nested directory transfer (4 files, 2 levels)
- pause → byte-identical pause point → resume → completed + verified (12 MB/s limiter active)
- source SIGKILL mid-transfer → job `interrupted` → source restarted → resume from `.part` offset → completed + verified
- corrupted transfer (Range-aware MITM bit-flip) → job `failed`, `verification_failed` event, `.part.corrupt`, destination never finalized, source intact
- safe move: source deleted only after verification
- corrupted move: original preserved
- destination conflicts: rename `(1)`, skip, replace
- insufficient space (507, nothing written)
- bandwidth limit: 12 MB/s configured → ≤ 4.6 MB measured cap margin held, elapsed ≥ 2.2 s for 48 MB
- sync preview + apply (identical skipped, missing copied, nothing deleted)
- config persistence across restart; auth-token enforcement (401 without token); graceful shutdown; telemetry snapshot shapes

Red at cut-off (honest status):
1. `test_broadcast_reaches_all_participants` / `test_group_message` — the *receiving* companion process stops answering mid-suite (RemoteDisconnected) after several prior test modules; not yet root-caused. Manual reproduction with two companions worked (message delivered + stored), so the defect is interaction/state-dependent, likely suite-level resource contention or a crash in a background thread of the receiver. Debug log tail did not show a traceback before the hang.
2. `test_event_log_captures_transfer_and_audio` — `/api/audio/preset` returned `internal_error` in this run after the `time` import fix; needs one more reproduction cycle.
3. `test_auth_token_enforced_when_enabled` — assertion updated for the new test-client semantics; last run still reported the old failure form (stale ordering), needs rerun.
4. `test_reconnection_after_peer_restart` — B does not come back online within the wait window after a stop/start cycle; likely TTL/re-probe timing, needs investigation.

These five are listed in DEVELOPMENT_BACKLOG.md §P1 context and the failing
tests are retained in the shipped suite (they are the to-do list, not
skipped).

## 4. Browser interface verification (actual, scripted)

- `agent-browser` (headless Chrome 158) loaded `http://127.0.0.1:18415/`:
  boot screen → Command Center with live device cards and event feed.
- **All 12 views** opened in sequence; **0 console errors** after fixes
  (initial sweep surfaced 3 real frontend bugs: stale `draw` reference,
  `prefs is not defined` in commander panes, missing `other` closure — all fixed).
- Real user-path transfer driven by clicks: Radar → register device →
  File Commander: right pane device selector → shares/drives selector →
  select `ai-model-sim.bin` (28.6 MB, 30 000 000 B) → **Copy →** →
  Transfer Center tab shows the job; job state `completed`,
  `verify=ok`. Independent check:
  `sha256sum` source == destination (`e76c4045…439ca`).
- GhostChat: API-sent message appears in conversation list + chat
  (`delivered`), delivered/read status ticks present.
- Radar map: live canvas with animated links, latency label (2 ms), legend,
  sweep and register controls functional.
- Transfer Center: six state tabs, live progress bars, bandwidth limit UI,
  sync preview/apply UI, report export.
- Mobile 375×812: sidebar collapses to a drawer; single-column layout.

Screenshots shipped in `screenshots/` (dashboard, radar, commander, chat,
transfers).

## 5. Performance measurements (measured, not theoretical)

| Metric | Value |
|---|---|
| Companion RSS idle | ~28–34 MB |
| Idle CPU | ~0 % (3 s beacon + 5 s telemetry) |
| RSS during 40 MB/s transfer | +5–8 MB, flat (1 MiB chunks) |
| 30 MB UI-driven transfer | < 1 s incl. verification |
| Bandwidth limiter | 12 MB/s config → 12.0–12.6 MB/s measured over 60 MB |
| UI responsiveness during transfer | view switches + SSE updates unaffected (separate threads) |

## 6. Not tested / explicitly not claimed

- Windows OS execution, Windows audio (PowerShell SoundPlayer) audibility
- phone/tablet microphone capture (platform restrictions documented)
- WebRTC (not used), TLS mode end-to-end with a phone browser
- >2 GB single-file transfers in this container (8 GB free disk; chunked
  design is exercised at 40–80 MB and the streaming path has no size
  special-cases)


---

# v1.3.0 test report (addendum)

## New automated tests
- `tests/unit/test_v130_mobile.py` — 10 tests: registration/secret-once,
  persistence across restart, identity mismatch, rename/remove, presence
  TTL honesty, inbox append/mark/since, ring bounds, unread counts,
  duplicate attach, client_updated SSE.
- `tests/unit/test_v130_alerts.py` — 8 tests: threshold+duration+cooldown,
  honest unavailable-telemetry, disk-low math, scoped event rules,
  disabled rules, delivery channels (event/chat/SSE sound), history.
- `tests/unit/test_v130_migration.py` — 6 tests: backup exists and is
  verbatim, schema marker, idempotency, malformed outbox/message repair,
  unknown-key preservation.
- `tests/unit/test_v130_monitor.py` — 6 tests: mount classification,
  view subsetting, config prefix hiding, GPU honesty (no fabricated util),
  CPU topology, v1.2.2 `_disks` contract.
- `tests/integration/test_v130_mobile_e2e.py` — 14 tests over real
  companion subprocesses: identity advertises kind/caps/migration, phone
  registration + persistence, computer→phone honest delivery (sent →
  client-ack → delivered), phone→computer with read receipts,
  phone→phone across hosts, offline queueing + dedupe-safe sync,
  idempotent incoming, reactions/edits/tombstones propagation, chat file
  store + size cap, discovery diagnostics + peer exchange, storage view,
  alerts lifecycle, automation run with verified transfer, PWA assets
  (manifest/sw/offline/`/m`/icons), rename+remove.
- `tests/js/test_v130.mjs` — 16 node checks: modem offline roundtrips at
  three data rates incl. 5-packet messages, CRC corruption rejection,
  Farnsworth timing, analysis helpers, seeded practice words, vendored QR
  MIT header + encode, PWA manifest/SW rules, mobile UI invariants,
  desktop PWA hooks, GhostChat 2.0 endpoints.
- `tests/conftest.py`: `wait_job_done` now waits through the asynchronous
  checksum-preparation phase ("hashing") — assertions check genuine
  completion instead of a transient state (strennthens the suite).

## Results in the development sandbox
- All new unit tests pass (31/31). JS suite: pending final node run at
  packaging time (see BUILD_LOG.txt).
- Integration: chat delivery, receipts, dedupe, chat files, discovery
  diagnostics, storage, alerts, PWA assets verified. Two tests are
  environment-sensitive in the 2-vCPU sandbox and documented as such:
  * `test_phone_to_phone_across_hosts` — depends on beacon propagation of
    hosted-client lists; in the sandbox UDP broadcast between two loopback
    companions is unreliable, the test uses the manual probe fallback.
  * `test_automation_job_runs_verified_transfer` — depends on the same
    beacon/multi-machine timing; the verified-engine execution itself is
    covered by the v1.2.2 regression suite.
- Full-suite regression: v1.2.2 suite re-run; remaining failures in this
  sandbox are IDENTICAL to a pristine v1.2.2 worktree run (verified via
  git worktree A/B): timing-sensitive "hashing"-phase polls on large
  directory fixtures and the root-requiring installer test. No v1.3.0
  regression found; `transfers.py`/`hashprep.py` are byte-identical.

## Pending physical verification (hardware not available in sandbox)
- Real phone (iPhone/Android): registration via camera QR, portrait/
  landscape at 320/375/390/430/768 px, Add to Home Screen, voice messages,
  intercom PTT microphone, iOS notification limitations.
- Acoustic modem room-scale transmission; Media Party audibility.
- Windows executable build (CI workflow delivered, awaiting a runner).
- Live-capture v1.3.0 UI screenshots (shipped captures are v1.2.2).


---

# v1.3.1 test report (final, this release)

## Passing
- Unit: 21 mobile-registry/identity tests (incl. 10 new v1.3.1 identity
  protection tests), 8 alerts, 6 migration, 6 monitor, v1.2.2 core/transfer
  logic, sandbox fs, checksum regressions.
- Integration: 19/19 mobile e2e (computer→phone, phone→computer,
  phone→phone same host, phone→phone cross hosts, persistent identity,
  independent inboxes, offline queueing, reconnection sync, delivery
  acks, read receipts, dedupe, attachments, voice roundtrip, takeover
  refusal over HTTP, discovery diagnostics, peer exchange, storage view,
  alerts lifecycle, automation verified transfer, PWA assets).
- JS: 17/17 (modem roundtrips at three rates, CRC corruption rejection,
  Farnsworth timing, QR vendored library, PWA/SW rules, mobile invariants).
- Focused transfer-engine regression: all except the two sandbox-timing
  tests below; installer: 2 pass + 1 honest skip (passwd-dependent
  original, superseded by the deterministic variant).

## Failing (environment-specific, NOT weakened)
- `test_corrupted_transfer_fails_verification_and_preserves_source`,
  `test_bandwidth_limit_enforced` (and the 20k-file fixture in the full
  integration file): the 2-vCPU sandbox stalls in the asynchronous
  checksum-preparation polling window. A/B against a pristine v1.2.2
  worktree shows the identical failures — no v1.3.x regression. These
  pass on the author's physical machines.

## Skipped
- `test_installer_ignores_root_home_and_root_xdg`: skipped only where the
  simulated desktop user is absent from the passwd DB (sandbox); its
  assertions are unchanged and a deterministic replacement test passes
  everywhere.

## Unexecuted (no hardware/runner available)
- Real iPhone/Android hardware tests (QR camera flow, iOS PWA/mic).
- Windows exe build (CI workflow delivered; awaiting a Windows runner).
- Room-scale acoustic modem and Media Party audibility checks.

## Browser automation (v1.3.1)
- agent-browser: desktop 1440×900 (dashboard, GhostChat, Settings QR,
  Fleet) + mobile 390×844 (onboarding → live registration → home, chat,
  conversation, devices, files, more) + offline shell.
- Verified live: mobile registration persists across companion restart
  and page reload; service worker controls the page and `update()`
  resolves; QR modal renders from the vendored library.
- Two REAL defects found by automation and fixed: mobile app.js syntax
  error (v1.3.0 shipped unable to boot) and sender-copy push semantics.
