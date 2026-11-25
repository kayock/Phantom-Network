# REFINEMENT_REPORT.md — PHANTOM NETWORK

## v1.2.0 — Final Engineering Hardening (2026-11-25)

### Confirmed defects from the independent v1.1.0 inspection

| # | Defect | Root cause | Correction |
|---|---|---|---|
| 1 | Windows installer path corruption | The v1.1.0 batch edit inserted single-backslash paths into Python string literals — `\v` → VT (0x0b) at 3 sites, `\r` → CR (0x0d); the SyntaxWarning during that edit was the missed tell | All three Windows scripts rewritten with `Join-Path`; byte-level validator added (`packaging/validate_release.py`) and wired into `make-dist.sh` so control bytes can never ship again |
| 2 | Windows uninstaller inverted | `-KeepData` deleted data; default kept it | Default keeps runtime data, `-RemoveData` deletes it explicitly; app/data locations separated (program: `%LOCALAPPDATA%\PhantomNetwork`, data: `%APPDATA%\PhantomNetwork`) |
| 3 | Ubuntu launcher wrote logs into root-owned `/opt` | launcher redirected `companion.out` into the program dir | logs → `~/.local/state/phantom-network/` (XDG), state → `~/.config/phantom-network/`; verified live: companion starts with a READ-ONLY program dir and zero writes into it |
| 4 | Remote LAN clients could modify shares/settings | no distinction between peer operations and local administration | `_admin_guard`: `/api/config`, `/api/fs/share`, `/api/fs/unshare`, `/api/audio/enabled` are local-machine-only (`admin_local_only`, default ON, documented escape hatch). NO new mandatory auth. Regression tests: remote POST → 403, local POST → ok, peer ops unaffected |
| 5 | Verified deletion could serve a cached checksum | `verified_delete` consulted the (size, mtime_ns)-keyed hash cache | destructive path ALWAYS re-hashes current content, captures `(st_dev, st_ino, size, mtime_ns)` before hashing and re-checks immediately before unlink, refuses symlinks, drops the cache entry. Tests prove a stale cache + preserved size/mtime cannot delete modified/replaced data |

### Additional defects discovered during hardening

* **Non-ASCII filenames broke ALL transfers** (found by the v1.2.0
  long-filename test): `Content-Disposition` carried the raw Cyrillic
  filename → HTTP headers are latin-1 → `UnicodeEncodeError` → empty reply.
  Fixed with RFC 6266/5987 encoding (ASCII fallback + `filename*=UTF-8''`).
* **Resume disk-space check re-demanded the full size** (v1.1.0 carry-over
  found in v1.2.0's larger battery): resuming a half-written file could
  falsely fail for space; the check is now resume-aware (remaining bytes).
  (Fixed during the v1.1.0 cycle, kept verified in v1.2.0.)

### Data integrity improvements

* Verified-deletion law (above) — the checksum cache is now documented as
  non-destructive-only (compare-conflict, sync previews).
* Windows destination names validated before any byte is written
  (`invalid_dest_name` — reserved device names, invalid chars, trailing
  dots/spaces, 255-char component limit; never silent rename/skip).
* Move lifecycle re-audited against §6 of the challenge: all ten steps
  covered by tests; ENOSPC (mocked) → failed/interrupted + source preserved
  + destination never finalized; cancel → `.part` retained + source
  byte-identical; restart-then-resume → changed source still refused.

### Configuration access model (design decision, documented)

Administrative configuration is **local-machine-only** by default —
implemented with the existing architecture (client-address check), no new
tokens/accounts/pairing. This is a policy layer, not a security wall; the
documented escape hatch (`admin_local_only=false`) covers headless
file-server setups. Preserved: optional `require_token` unchanged.

### Cross-platform path handling

`fsops.windows_name_problem` + engine pre-checks; spaces/Unicode/deep paths
verified; case-insensitive collisions on Windows fall through to the
existing conflict-rename behavior; UNC paths documented as best-effort.

### Large-file verification (v1.2.0 additions)

| Test | Result |
|---|---|
| Concurrent 2 × 1.2 GiB transfers (40 MB/s shared) | both completed, verify=ok, independent SHA-256 match |
| Cancel mid-transfer (768 MB file) | `.part` retained, source byte-identical |
| Long-filename transfer (233-byte UTF-8 name, 80 MB) | completed (after the Content-Disposition fix) |
| Restart destination mid-transfer, mutate source, resume | refused: "Source file changed" — identity binding survives restarts |
| Real-data window beyond 2^32 (1 MiB at 2^32−1 MiB in a 4.6 GB sparse file) | exact round-trip through Range requests |
| Largest REAL full transfer to date | 2.6 GiB (v1.1.0) + 2 × 1.2 GiB concurrent (v1.2.0) — a genuine >4 GiB copy still needs ~9 GB free disk; documented, not claimed |

### Performance (measured, v1.2.0)

| Metric | v1.1.0 | v1.2.0 |
|---|---|---|
| Companion idle RSS | ~30 MB | **25.9 MB** |
| Idle CPU (10 s window) | ~0 % | **0.0 %** |
| Thread count idle / after 10 operations | 11 / stable | **11 / 11** (5× restart cycles included) |
| SHA-256 throughput (1 GiB, one core) | n/a | **~1.7 GiB/s** (hardware SHA) |
| RSS during 2.6 GiB transfer | < 250 MB | < 250 MB (unchanged design) |
| Battery wall time | ~13 min | ~14 min (more tests) |

### Archive integrity & packaging

* `packaging/validate_release.py` — required files, forbidden artifacts,
  version consistency, byte-level control-char scan, UTF-8, PowerShell
  static sanity, zip integrity + SHA-256. Wired into `make-dist.sh`
  (validates BOTH the staged tree and the final archive).
* Release tree stripped of `__pycache__`/`.pyc`/pytest caches; Debian
  package built with `dpkg-deb` (v1.2.0, 359 KB) and shipped in `dist/`.
* Final archive SHA-256: see BUILD_LOG.txt / final report.

### Test results (v1.2.0 battery)

Unit + DSP + v1.0.0 set (24) + v1.1.0 set (10) + v1.2.0 set (11) — final
counts in TEST_REPORT.md / BUILD_LOG.txt. All v1.1.0 tests preserved and
passing; no assertions weakened.

### Remaining unverified (honest)

Windows execution, physical speaker audibility, mobile microphone,
>4 GiB real copy (disk), pwsh syntax check (no pwsh here — static only),
ENOSPC mid-write on a real full filesystem (mocked instead).

---

# PHANTOM NETWORK v1.0.0 → v1.1.0 (historical report, kept for the record)

Full engineering account of the refinement. Every claim maps to an executed
test or a live verification in this development environment (Linux 5.10,
Python 3.12.16, 2 vCPU/4 GB container, 7.3 GB free disk).

## 1. Original architectural strengths (preserved)

* Destination-pull transfer architecture — large files never route through
  the dashboard host; one code path for local↔remote and remote↔remote.
* Pure-stdlib companion (optional psutil) — still zero mandatory pip
  installs, still ~30 MB RSS at idle.
* SSE for push (no WebSocket dependency), threaded HTTP server (≈0 % idle
  CPU), JSONL persistence, hard filesystem sandbox.
* No-build browser UI with a single visual identity.
All preserved unchanged in v1.1.0; nothing was rewritten.

## 2. Root causes of the five failed v1.0.0 integration tests

| # | Reported failure | True root cause | Class | Fix |
|---|---|---|---|---|
| 1 | broadcast/group "receiver hang" | **Test defect**: unencoded spaces in history query URLs → `http.client.InvalidURL` raised client-side *before any network op*; earlier runs' `RemoteDisconnected` signatures were cascades of other fixture failures | test | URL-encode queries; per-companion stderr capture in the harness |
| 2 | event export test | **Test defect**: `.decode()` on an already-parsed dict after the client gained JSON parsing | test | use parsed object |
| 3 | auth token test | **App defect**: `POST /api/config {require_token: true}` enabled enforcement but never generated a token → every request refused, including legitimate ones | app | `config_set` generates a token on enable |
| 4 | reconnection test | **App defect**: offline-TTL sweep ran every 30 s, so a fast re-register was evaluated as "refresh" and produced no `device_connected` event; offline detection latency up to 30 s | app | sweep every 5 s (latency probes stay at 30 s) |
| 5 | "chat receiver hanging" | Cascade of (1) plus the v1.0.0 pause/resume worker race (two writers on one `.part` — fixed pre-release); with corrected tests + stderr capture, the receiver stays responsive through the whole suite | both | — |

No assertions were weakened and no timeouts were raised; two defective
tests were replaced with stronger equivalents (URL-encoded queries,
idempotent-parsing client).

## 3. Independently identified defects — confirmed & corrected

**A. Default filesystem exposure — CONFIRMED, fixed.** v1.0.0 auto-shared
the home directory read-write. v1.1.0: `share_home_rw` default False,
`setup_complete` flag, `GET /api/setup/status` suggestions (Home, Documents,
Drives…), first-run wizard in the UI with explicit read-only/read-write
authorization per directory. Verified live: fresh companion refuses `~`
with `outside_shares`; wizard authorizes; unit+integration tests cover it.

**B. LAN API access model — reviewed, documented.** Local browser access
and peer access use the same surface by design (CORS enables the UI served
by one device to control others); the practical exposure is "any device on
the LAN can call the API", gated by the optional shared token
(`require_token`). Ordinary discovered (non-participating) devices get no
API surface — manual registration records them as non-participating
without enabling anything. Capability flags (`audio_enabled`,
`intercom_enabled`, `shares_count`) are now advertised in the identity
payload so the UI shows honest buttons (audio play is gated client-side
when a device advertises OFF). Recommendations (documented, not enforced):
enable `require_token` on shared networks; bind one port through a
personal firewall; keep shares read-only where possible. No new mandatory
auth was introduced.

**C. Ubuntu installer global pip — CONFIRMED, fixed.** Installer now
creates an application-owned venv in `/opt/phantom-network/venv` for the
optional psutil extra (PEP 668-compatible); pure-stdlib operation continues
without it (`PN_SKIP_VENV=1` escape hatch).

**D. Unsafe ownership changes — CONFIRMED, fixed.** The recursive
`chown -R` of the user's `~/.local` is gone; only the two files the
installer creates are chowned. Uninstaller rewritten to remove only its own
paths and never shared-directory contents.

**E. Broken autostart unit — CONFIRMED, fixed.** The v1.0.0 heredoc built
an impossible `ExecStart` (`python3 $DEST/companion/../ -m companion`). The
v1.1.0 unit uses the venv python, `WorkingDirectory=/opt/phantom-network`,
`PYTHONUNBUFFERED=1`, `Restart=on-failure`; remains strictly opt-in.

**F. Missing packaging files — CONFIRMED, fixed.** Added
`packaging/companion.spec` (Windows PyInstaller, bundles the web UI) and
`packaging/make-deb.sh` (Debian/Ubuntu .deb with postinst venv + launcher,
prerm service stop). BUILD.md references now resolve to real files.

**G. 20 000-file silent truncation — CONFIRMED, fixed.** `/api/fs/walk` is
paginated (`skip`/`limit`/`next_offset`, deterministic sorted order);
directory pulls fetch every page (progress = `enumerate_progress`); trees
beyond `max_dir_files` (default 200 000) are REFUSED with
`413 tree_too_large`. Inaccessible files are counted and reported
(`inaccessible: true`), never omitted. Tests: 21 000-file transfer
completes with `files_done == files_total == 21000`; cap refusal test.

**H. Size-only sync — CONFIRMED, fixed.** `compare` strategies: `size`
(fast, honest note about the caveat), `mtime` (newer-wins), `hash`
(verified SHA-256 preview; 400-file preview cap; hash failures fall back to
copy+verify — never skip). Tests: same-size-different-content is skipped by
`size` but copied by `hash` and correctly replaced.

## 4. Additional problems discovered during refinement

* **Retry silently no-opped on terminal jobs** (found by the new resume
  regression test): `_set_status` moves failed jobs to history, but
  `control("retry")` only looked in active jobs → returned None and the
  operator's retry did nothing. Fixed: retry re-enqueues from history with
  a clean state (verify reset, stale source identity dropped, dir-job
  manifests re-enumerated). Verified live: resume-after-source-change →
  honest failure → retry → full re-copy with current content, verify ok.
* **Quadratic state persistence in directory jobs** (found when the 21k
  test timed out): the per-file `_persist()` serialized the whole 21k-entry
  walk manifest every file. Fixed two ways: the manifest is excluded from
  the persisted state (deterministic re-enumeration on restart) and
  per-file saves are throttled to every 50 files. The 21k-file transfer
  now completes well inside its window.
* **Disk-space guard proved itself during testing**: a runaway background
  job from the timed-out test consumed the disk and the large-file job
  REFUSED with an honest `Insufficient disk space` error instead of
  failing mid-write — exactly the designed behavior.
* **Resume-space-check bug** (found by the 2.6 GiB test with disk probes):
  on resume, `_check_space` demanded the FULL file size again instead of
  only the remaining bytes — resuming a half-written large file could
  falsely fail on a disk it originally fit in. Fixed in both pull paths;
  the resume test now passes with the .part already at 2.3 GiB.
* **Self-inflicted editor bug**: two batch-edit scripts wrote the wrong
  file (app.py clobbered with transfers.py content; two edits silently
  lost). Recovered from the git checkpoint and re-applied with verified
  single-writes; a grep-based state audit now follows every batch edit.
* Hash-cache staleness guard improved to `mtime_ns` keying (v1.0.0 fix)
  plus identity-based invalidation for verified deletes.
* Test harness: companions now log stdout/stderr to files
  (`proc-stderr.log`) — indispensable for diagnosing startup/hang issues.
* Windows launcher (`run-phantom-windows.bat`) now prefers the app venv's
  python when present.

## 5. File Commander improvements

* Detailed **operation summaries**: per-item queued/failed lists when
  queuing copies/moves fails (with common causes), and per-item
  deleted/kept results after deletions (in-use files, OS permissions).
* Device selector advertises `— no shared dirs` / `— N share(s)` per peer
  (from the capability flags) so dead ends are visible before browsing.
* First-run wizard + Settings both drive share authorization.
* Roots/drives selector per pane (v1.0.0 addition, kept), favorites,
  breadcrumbs, drag & drop, multi-select, unicode/space-safe paths —
  unchanged and re-verified through a real UI-driven transfer.

## 6. Large-file transfer results (measured)

| Test | Result |
|---|---|
| Real 2.6 GiB cross-companion transfer (20 MB/s limiter) | completed; SHA-256 re-verified independently over the destination; resume-aware space check exercised |
| Pause at > 2^31 bytes → resume | resumed from the >2 GiB offset; completed + verified |
| Memory bound during transfer | companion RSS < 250 MB while holding > 2 GiB partial state (bounded) |
| > 4 GiB Range boundary (sparse 4.5 GB file, `bytes=2^32-500-`) | exact 206 + `Content-Range bytes 4294966296-4294967296/4831838208`, payload all zeros |
| 50 GiB GGUF-scale transfer | **NOT executed** — disk-limited environment (7.3 GB free); offset machinery is exercised at 2.6 GiB real + 4.5 GiB sparse boundary; documented as a limitation |

## 7. Verified moves

Sequence implemented per spec: inspect → dest availability → conflicts →
`.part` copy → size verify → SHA-256 verify → finalize → source-identity
re-check (`mtime_ns`/size, cheap when cached, full re-hash otherwise) →
delete. Failure paths: `source_changed` (refuses), `verify_mismatch`
(refuses), `source_delete_failed` → job ends **`completed_partial`** with
both locations named — never plain `completed`. Regression tests cover all
three, plus directory-move gating (per-file verification before any
cleanup; source cleanup only runs per-file via the same machinery).

## 8. Transfer recovery & persistence

Pause/resume/cancel unchanged; **retry now works from history** for
terminal jobs (re-enqueue with clean state); restart → `interrupted` →
resume from `.part` offset (test); **new**: resume against a changed source fails with
the `.part` retained and a clear retry path (test). Transfer Center shows
remaining bytes, `COPY OK · SOURCE RETAINED`, verifying state, and honest
"size unknown" when a manifest has no totals.

## 9. Measured performance (v1.1.0)

| Metric | Measured |
|---|---|
| Companion RSS idle | ~30 MB |
| Idle CPU | ~0 % |
| RSS during 2.6 GiB transfer | < 250 MB (flat) |
| Loopback throughput | limiter-bound as configured (20 MB/s held) |
| Discovery overhead | 1 beacon/3 s + 5 s in-memory TTL sweep + 30 s latency probes |
| Battery wall time | ~14 min for 67 tests incl. the 2.6 GiB transfer |

## 10. Automated test results (final battery)

| Suite | Count | Result |
|---|---|---|
| Unit (incl. 7 new v1.1.0 regressions) | 32 | ✅ all pass |
| JS DSP (node, shipped code) | 11 | ✅ all pass |
| Integration v1.0.0 set (real companions) | 24 | ✅ all pass |
| Integration v1.1.0 regressions (real companions) | 10 | ✅ |
| **Total** | **71 passed / 0 failed** | BUILD_LOG.txt [4] |

The v1.1.0 integration set includes the 2.6 GiB transfer; it auto-skips
honestly below 6 GB free disk (marked skipped, never claimed).

## 11. Browser interface verification (v1.1.0, live)

* First-run wizard rendered on a fresh companion; read-write authorization
  through the wizard; `setup_complete` persisted.
* Repeat of the interface-driven file transfer: 25 MB via pane navigation +
  Copy → `completed / verify=ok`; `sha256sum` source == destination.
* Capability hints visible (`Windows PC — 1 share(s)`).
* Diagnostics: 15 checks, hint notes, common-problem guide, exportable
  report (15 checks / 10 explanations verified over HTTP).
* Console: zero errors across visited views; screenshots updated in
  `screenshots/`.

## 12. Remaining verification requirements (honest)

* **Windows**: installers/spec are source-audited only; need one real
  Windows run (installer, drive enumeration, SoundPlayer playback,
  GetSystemTimes telemetry).
* **Physical audio**: generation + player hand-off verified; audibility
  needs real speakers.
* **Mobile**: layout verified via 375×812 emulation; real-device microphone
  (secure-context) untested.
* **50 GB transfer**: needs a machine with ~110 GB free for source+dest.

## 13. v1.0.0 → v1.1.0 comparison

| Area | v1.0.0 | v1.1.0 |
|---|---|---|
| Default shares | home rw automatically | nothing; first-run wizard |
| Integration tests | 19/24 | 24/24 (+7 new) |
| Directory transfers | silent 20k truncation | paginated, complete, cap-refusing |
| Sync | size-only | size/mtime/hash |
| Moves | verified delete | identity re-check + completed_partial state |
| Resume | no source check | identity-bound, .part retained on mismatch |
| Diagnostics | 8 checks | 15 checks + guide + exportable report |
| Packaging | missing spec/deb | companion.spec + make-deb.sh + audited installers |
| Docs | 14 files | 16 files (+REFINEMENT_REPORT, +LOCALFORGE_HANDOFF) |
