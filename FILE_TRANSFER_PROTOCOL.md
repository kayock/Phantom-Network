# FILE_TRANSFER_PROTOCOL.md

How Phantom Network v1.0.0 moves bytes between participating companions.

## 1. Topology

```
UI (browser)                DEST companion                  SRC companion
     │                            │                              │
     │ POST /api/jobs ───────────►│                              │
     │                            │  GET /api/fs/hash?path ─────►│
     │                            │◄─── {size, sha256} ──────────│
     │                            │  GET /api/fs/download        │
     │                            │    ?path  Range: 0-(C-1) ───►│
     │                            │◄──── 206 chunk (1 MiB) ──────│  ×N
     │                            │  (hash received bytes live)  │
     │  SSE transfer events ◄─────│  compare sha256              │
     │                            │  rename .part → final        │
     │                            │  (move) POST /api/fs/        │
     │                            │       verified-delete ──────►│
```

* The job **always executes on the destination companion** — that machine
  owns disk-space checks, conflict resolution and the resume state.
* `src_base` may be `"self"` (a job on the same machine), a companion URL
  (`http://192.168.1.10:8415`), or any HTTP Range-capable endpoint — which
  also makes the protocol testable through proxies (the test suite uses a
  corrupting MITM proxy exactly this way).
* Remote↔remote transfers never touch the dashboard host: the UI simply
  POSTs the job to the destination companion.

## 2. Job specification

```json
POST /api/jobs
{
  "kind": "pull" | "pull_dir",
  "src_base": "http://192.168.1.10:8415" | "self",
  "src_path": "/srv/data/model.bin",
  "dest_path": "~/models/model.bin",
  "conflict": "rename" | "skip" | "replace" | "compare",
  "delete_source": false,          // true ⇒ safe move
  "sha256": null,                  // optional override of expected hash
  "src_device": "Ubuntu Station",  // display labels for the UI
  "dest_device": "Windows PC"
}
```

Directory jobs (`pull_dir`) fetch a manifest from the source
(`GET /api/fs/walk?path&skip&limit` — paginated with
`next_offset`; the job fetches every page so trees of any size within
`max_dir_files` (default 200 000) transfer COMPLETELY — beyond the cap the
job is refused with `tree_too_large` rather than silently truncated), create
all directories on the destination, then run one `pull` per file
sequentially, updating `files_done / files_total` and the aggregate byte
counter. Progress is checkpointed (`file_index`) so directory jobs survive
restarts too.

## 3. Chunked streaming

* Chunk size = `config.chunk_size` (default **1 MiB**), requested with
  `Range: bytes=<offset>-<offset+chunk-1>`; source answers `206` with exact
  bytes. Nothing is buffered beyond one chunk — memory stays flat for
  100 GB files.
* The receiving worker feeds every received chunk into a **live SHA-256
  digest** — the receiver never re-reads the final file to verify.
* The **source** hashes the file once via `/api/fs/hash` (streamed, cached
  by `(path, size, mtime_ns)`), so each side reads the data at most once.
* Bandwidth limit: a shared token-bucket (`RateLimiter`) throttles all
  workers of a companion to `bandwidth_limit_bps` bytes/s (0 = unlimited).

## 4. Integrity verification

1. Expected digest = `sha256` from the job spec, else the source's
   `/api/fs/hash` result.
2. Actual digest = live hash of received bytes (re-read from `.part` when
   resuming older bytes: those were verified in the previous session and the
   digest is recomputed over the whole `.part` at finalize — the engine
   hashes the `.part` file before finalizing, guaranteeing the entire file
   matches, not just the newly received bytes).
3. A job is marked **completed** only if `actual == expected`. On mismatch:
   * `.part` is preserved as `<dest>.part.corrupt` for diagnosis,
   * the destination file is NOT created,
   * the job fails with `SHA-256 mismatch…`, event `verification_failed`,
   * the source file is untouched (delete-source moves abort before the
     source is ever asked to delete).

## 5. Source identity binding & resume

Before streaming, the engine records the source identity
`(size, mtime_ns)` in the job. On resume the identity is re-fetched and
compared:

* match → continue from the `.part` byte offset,
* mismatch → job **fails** with `Source file changed…`; the `.part` is
  retained (retry with `conflict: replace` discards it, `skip` keeps it).

**v1.2.0 — verified deletion never trusts the cache.** For destructive
operations the CURRENT content is always re-hashed immediately before the
unlink (the (size, mtime_ns)-keyed cache is used only for non-destructive
work: compare-conflict decisions and sync previews). The file identity
`(st_dev, st_ino, size, mtime_ns)` is captured before hashing and
re-checked right before unlink, so modifications or same-path replacements
during/after verification refuse the deletion; symbolic links are never
followed into deletion. The cost is one extra full read of the source per
move — deliberate: source preservation outranks transfer speed.

## 5b. Resume & crash recovery

* `.part` byte length = resume offset. Pause/resume/cancel are flags the
  worker checks between chunks.
* Job state is persisted to `jobs_state.json` on every status change and
  every progress tick. After a companion restart, unfinished jobs come back
  as `interrupted` and resume with `POST /api/jobs/{id}/control {"action":
  "resume"}` — verified by the integration test that SIGKILLs a source
  mid-transfer and resumes.
* If the source shrank (`offset > size`), the `.part` is truncated and the
  transfer restarts from zero for that file.

## 6. Conflict resolution

| `conflict` | Behaviour when destination file exists |
|---|---|
| `rename` (default) | destination becomes `name (1).ext`, `name (2).ext`, … |
| `skip` | no transfer; job completes immediately (counts as verified) |
| `replace` | overwrite the existing file (after verification) |
| `compare` | same size **and** same SHA-256 → skip; otherwise transfer |

Upload sessions (browser → companion) support the same modes via
`POST /api/fs/upload/session`.

## 7. Safe move (copy → verify → delete)

1. Copy the source (pull with `delete_source: true`).
2. Verify destination size + checksum (§4).
3. Destination companion asks the source:
   `POST /api/fs/verified-delete {path, sha256}`.
4. The **source independently re-hashes** the file and refuses
   (`409 verify_mismatch`) if anything changed — the original is preserved.
5. Only then is the source file removed.

Failure at any step keeps the original; the UI shows the verified-move
completion plus any `move_error` separately.

## 8. Disk space & errors

Before the first byte is written the destination checks
`shutil.disk_usage(dest)` against `size + min_free_space_bytes`
(default 64 MiB) → `507 insufficient_space` fails the job without touching
disk. Network failures retry 3× with backoff, then park the job as
`interrupted`. Permission errors surface the OS message verbatim.

## 9. Directory synchronization

`POST /api/sync/preview` walks both trees and classifies each file as
`copy` or `skip` using the requested strategy:

| `compare` | equal-size decision |
|---|---|
| `size` (fast) | skipped (equal size only — documented caveat) |
| `mtime` | destination kept if not older than source |
| `hash` (verified, default in the UI) | both sides hashed (≤ 400 files per preview; beyond that files are copied + verified) |

Every applied copy is a normal SHA-256-verified pull job regardless of
strategy. **Sync never deletes anything.**

## 10. Browser uploads

Small/medium files can be pushed from a browser (e.g. phone) without a
companion on the client: `upload/session` → repeated `PUT` 512 KB slices →
`finish` (size check; optional sha256). Large transfers should always go
through companion-to-companion pulls.

## 11. Intercom audio (related transport)

PTT audio is base64 Int16 LE PCM at 16 kHz mono, ~60 ms chunks, relayed
companion→companion over `POST /api/intercom/relay` and delivered to
browsers via SSE. Live relay only — nothing is persisted.
