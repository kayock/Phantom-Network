# API_REFERENCE.md

PHANTOM NETWORK v1.0.0 — REST + SSE reference.

Base URL: `http://<device>:8415` (configurable `api_port`).  All bodies are
JSON unless stated otherwise.  Errors use
`{"ok": false, "error": "<code>", "message": "<human text>"}` with a
meaningful HTTP status (400/401/403/404/409/416/500/507).  When
`require_token` is enabled, every `/api/*` call except `/api/health` and
`/api/identity` must send `X-PN-Token: <shared secret>`.

Cross-origin: companions send CORS headers on `/api/*` so a browser UI
served by one machine can talk to companions on other machines.

The full machine-readable route table lives in `companion/routes.py`.

## System

| Method & path | Description |
|---|---|
| `GET /api/health` | liveness + version: `{ok, product, version, protocol, uptime_s}` (no auth) |
| `GET /api/identity` | identity payload (no auth): `{pn, version, name, host, os, os_full, device_id, api_port, addresses[], uptime_s, audio_enabled, intercom_enabled, shares_count}` |
| `GET /api/stats` | aggregate counters: peers, transfers, chat, events, discovery, process |
| `GET /api/stream` | **SSE** (text/event-stream). Topics: `event`, `chat`, `transfer`, `intercom`. Auto `: keepalive` every 15 s |
| `GET /api/diagnostics` | quick self-check list `[{check, ok, detail, hint}]` |
| `GET /api/diagnostics/report` | exportable local report: identity, listening ports, peers, shares, drives, transfer failures, telemetry, checks, recent errors, `explanations` — nothing leaves the machine |
| `GET /api/setup/status` | first-run helper: `{platform, shares, suggestions[{path,label,drive?}], setup_complete}` |
| `POST /api/diagnostics/selftest` | extended self-test incl. loopback HTTP, transfer engine, chat store, telemetry |

### SSE payloads
* `event` — Event Center record `{id, ts, iso, type, severity, source, text, data}`
* `chat` — full message record (see Chat) on send/deliver/status change
* `transfer` — `{id, status, transferred, size, speed_bps, eta_s, files_done, files_total, verify, error}` (~1 Hz while running)
* `intercom` — `{session, pcm_b64 (Int16LE 16 kHz mono), seq, from, from_name}`

## Config

| | |
|---|---|
| `GET /api/config` | public config view (`auth_token` never included) |
| `POST /api/config` | partial update; `device_id`, `auth_token`, `api_port` are protected. Emits `config_changed` |

Writable keys include: `device_name`, `discovery_enabled`,
`active_sweep_enabled`, `broadcast_addresses[]`, `require_token`,
`max_concurrent_jobs`, `chunk_size`, `bandwidth_limit_bps`,
`min_free_space_bytes`, `verify_sha256`, `remote_audio_enabled`,
`audio_max_volume`, `audio_max_duration_s`, `intercom_enabled`,
`monitoring_enabled`, `manual_peers[]`, `groups[]`.

## Radar / peers

| | |
|---|---|
| `GET /api/peers` | `{self, peers[], discovery}` — each peer: `{device_id, display_name, name, custom_name, icon, group, addresses[], api_port, version, os, os_full, host, first_seen, last_seen, manual, participating, status, latency_ms, uptime_s, audio_enabled, intercom_enabled, shares_count}` |
| `POST /api/peers/register` | `{address, name?, port?}` → probes the address, registers companions, honestly reports non-participating hosts (`ok:false` + reason) |
| `POST /api/peers/{id}/metadata` | `{custom_name?, icon?, group?}` |
| `POST /api/peers/{id}/probe` | immediate re-probe |
| `POST /api/radar/sweep` | `{cidr?}` (default local /24) → TCP-probe of the API port only. Requires `active_sweep_enabled` |

## Filesystem

All paths are validated against configured shares; violations return
`403 outside_shares` with the list of authorized roots in the message.

| | |
|---|---|
| `GET /api/fs/roots` | `{shares[], drives[], platform}` — shares are the authorized roots; drives are OS volumes (browse convenience) |
| `GET /api/fs/list?path&hidden` | `{path, entries:[{name,path,type,size,mtime,ext,hidden,link_target?}], count}` |
| `GET /api/fs/stat?path` | `{path,is_dir,size,mtime}` |
| `GET /api/fs/preview?path` | `{kind: text|image|binary, data?, mime?, size, truncated?}` |
| `POST /api/fs/mkdir` | `{path(parent), name}` |
| `POST /api/fs/rename` | `{path, new_name}` (same-directory rename) |
| `POST /api/fs/delete` | `{paths[], confirm:true}` — permanent; refuses without `confirm` |
| `POST /api/fs/search` | `{root, query, max?}` bounded recursive search |
| `GET /api/fs/download?path` | octet-stream with **Range** support (206) — used by browsers and peer pulls |
| `GET /api/fs/zip?path` | zip of a file/dir (≤ 2 GB cap) |
| `GET /api/fs/hash?path` | `{path,size,sha256,mtime_ns}` — streaming hash, cached by (size, mtime_ns) |
| `GET /api/fs/walk?path&skip&limit` | **paginated** manifest `{root, files(page), dirs(first page), total_files, total_bytes, next_offset|null}` — fetch pages until `next_offset` is null; trees beyond `max_dir_files` (config, default 200 000) are refused with `413 tree_too_large` |
| `POST /api/fs/verified-delete` | `{path, sha256, mtime_ns?, size?}` — destructive: re-hashes CURRENT content (cache is never trusted here), re-checks file identity immediately before unlink, refuses symlinks; `409 verify_mismatch` / `409 source_changed` / `409 symlink_refused`, delete failure → `500 source_delete_failed` |
| `POST /api/fs/share` | `{path, mode: rw|ro, label?}` add authorized root |
| `POST /api/fs/unshare` | `{path}` |
| `POST /api/fs/upload/session` | `{path(dest dir), name, size, conflict}` → `{sid, name, target}` |
| `PUT /api/fs/upload/{sid}` | raw bytes (append; 512 KB slices from the browser) |
| `POST /api/fs/upload/{sid}/finish` | `{sha256?}` → validates size, moves `.part` into place |
| `POST /api/fs/upload/{sid}/cancel` | discards session + temp file |

## Transfers

| | |
|---|---|
| `POST /api/jobs` | create job (executed by THIS companion as destination). Payload: `{kind: pull|pull_dir, src_base, src_path, dest_path, conflict: rename|skip|replace|compare, delete_source?, sha256?, src_device?, dest_device?}` → `201 {job}`. `src_base:"self"` means the source is this companion |
| `GET /api/jobs` | `{jobs[](active), history[](≤300), stats}` |
| `GET /api/jobs/{id}` | current or historical record |
| `POST /api/jobs/{id}/control` | `{action: pause|resume|cancel|retry}` |
| `POST /api/jobs/history/clear` | clears local history |
| `POST /api/sync/preview` | `{src_base, src_path, dst_base, dst_path, compare: size|mtime|hash, src_device?, dst_device?}` → `{copy[{rel,src,dest,size,reason}], skip[], dirs[], copy_bytes}` (size-compare preview; nothing is deleted) |
| `POST /api/sync/apply` | `{plan, delete_confirmed:false}` → enqueues pull jobs (on this companion or, when `dst_base` is remote, POSTs them to that companion) |

Job record: `{id, kind, src_base, src_path, dest_path, src_device, dest_device,
size, transferred, status: queued|running|verifying|paused|interrupted|
completed|failed|cancelled, verify:{expected,actual,status},
speed_bps, avg_bps, eta_s, error, conflict, delete_source, files_done,
files_total, current_file, created, started, finished, retries}`

Status codes of note: `507 insufficient_space`, `409 verify_mismatch` /
`exists`, `416 bad range`.

## Chat

| | |
|---|---|
| `POST /api/chat/send` | `{to_kind: direct|group|broadcast, to_id, body, attachment?{name,size,path,src_base,from_name}}` → message record; `status` becomes `delivered` (ack) or `queued` (outbox) |
| `POST /api/chat/incoming` | peer→peer delivery: `{message}` → ack `{acked, id, ts}` |
| `POST /api/chat/read` | `{conv, up_to_ts}` — marks read locally + sends receipt |
| `POST /api/chat/ack-read` | `{reader, up_to_ts}` — receipt from peer |
| `GET /api/chat/conversations` | `{conversations:[{conv,kind,title,peer_id,online,last_ts,last_body,unread}]}` |
| `GET /api/chat/history?conv&q&limit&before_ts` | newest-first messages |
| `POST /api/chat/groups` | `{name, members[]}` |
| `DELETE /api/chat/groups/{id}` | remove group |

Message record: `{id, ts, from:{device_id,name}, to_kind, to_id, body,
attachment?, status: sent|delivered|read|queued|failed, outgoing, conv,
delivered_to?}`

## Audio

| | |
|---|---|
| `GET /api/audio/status` | `{enabled, player, playing, current?, limits}` |
| `POST /api/audio/tone` | `{freq(20–20000), duration(≤30), waveform: sine|square|triangle|sawtooth, volume(≤0.8)}` |
| `POST /api/audio/preset` | `{name: red_alert|incoming_transmission|device_connected|device_disconnected|transfer_complete|warning|morse_notification|scifi_chirp, volume?}` |
| `POST /api/audio/file` | `{path}` — authorized uploaded/shared audio file |
| `POST /api/audio/stop` | kills the active player process |
| `POST /api/audio/enabled` | `{enabled}` — the visible per-device remote-audio switch |

## Intercom

| | |
|---|---|
| `GET /api/intercom/sessions` | `{enabled, sessions[]}` |
| `POST /api/intercom/session` | `{participants[device_id], label?}` → `{ok, session}` |
| `POST /api/intercom/session/{id}/end` | end session |
| `POST /api/intercom/talk` | `{session, pcm_b64, seq}` — relay to all other members |
| `POST /api/intercom/relay` | peer→peer: publish to local SSE clients |
| `POST /api/intercom/stop` | `{session}` — PTT released |

## Monitoring / Events

| | |
|---|---|
| `GET /api/monitor` | snapshot: `{ts, host, os, psutil, cpu{percent,cores,loadavg}, memory{total,available,used,percent,swap_*}, disks[{mount,total,free,used,percent}], network{rx_bps,tx_bps,rx_total,tx_total,addresses}, interfaces[], uptime_s, gpu?{gpus[]}, process{rss_bytes,threads}}` — unavailable values are `null`, never fabricated |
| `GET /api/monitor/history` | rolling 5 s samples `{ts, cpu, mem, net_bps}` (240 points) |
| `GET /api/events?types&q&since&limit&severity` | filtered event query (newest first) |
| `GET /api/events/export` | JSON download of the local event log |

### v1.2.0 — local-administrative boundary

Administrative endpoints accept changes only from the machine's own
context (loopback). Ordinary peer operations — browsing authorized shares,
transfers, chat, audio triggering, `verified-delete` callbacks from a
remote move — remain available to LAN peers. The boundary is policy, not a
security wall: combined with the optional `require_token` it stops
*accidental* configuration drift from other devices; a hostile LAN client
can still attempt ordinary operations (see KNOWN_LIMITATIONS.md). Escape
hatch: set `admin_local_only: false` for headless file-server setups.


---

# v1.3.0 API additions

## Mobile clients
- `POST /api/mobile/register` {name, device_type, icon, client_id?,
  client_secret?} → 200 {ok, client{client_id, secret (ONCE), name, kind,
  capabilities…}, host{device_id, name, version}} — auth: none.
- `POST /api/mobile/recover` — fresh identity after browser storage loss.
- `POST /api/mobile/heartbeat` {client_id, client_secret} → {ok, ts, ttl_ms}.
- `POST /api/mobile/inbox` {client_id, client_secret, since_ts, limit} →
  {messages[]} — fetch marks messages delivered and routes receipts.
- `POST /api/mobile/chat/send` {client_id, client_secret, to_kind,
  to_id, body, attachment?, reply_to?} → {message}.
- `POST /api/mobile/chat/ack` {client_id, client_secret, ids, state:
  delivered|read} → routes receipts to origin companions.
- `POST /api/mobile/chat/mutate` {client_id, client_secret, action:
  react|edit|delete, msg_id, emoji?, body?}.
- `GET /api/mobile/stream?client_id=&secret=` — private SSE
  (`event: mobile` {type: message|receipt|presence|client_updated}).
- `GET /api/mobile/clients` → {clients[]} (public, no secrets).
- `POST /api/mobile/clients/<id>/rename` / `…/remove` — local admin only.

## GhostChat 2.0
- `POST /api/chat/react|edit|delete` {msg_id, emoji?|body?} — mutations
  propagate to involved companions; deletion leaves a tombstone.
- `POST /api/chat/mutate` {mutation} — remote propagation sink.
- `POST /api/chat/ack-delivery` {reader, reader_kind, ids, state}.
- `POST /api/chat/file/session` {name, size, mime} → {sid, max_bytes};
  `PUT /api/chat/file/<sid>` chunks; `POST …/finish` {sha256?} → {file};
  `GET /api/chat/file/<id>` download (Range supported, auth: none).
- `to_kind` gains `"mobile"` (to_id = client id) and `"system"` (local
  alert channel, conv `system:alerts`).

## Discovery / fleet
- `GET /api/discovery/diagnostics` — socket state, interfaces, beacon
  targets, rx/tx counters, last RX, manual peers, unknown senders,
  peer-exchange flag, per-event ring log.
- `POST /api/peers/exchange` {peers[]} — validated-then-added sink.
- `GET /api/storage?view=useful|physical|all` — classified mounts (kind,
  removable, read_only, hidden_by_config) + gpu + cpu topology.
- `/api/monitor` snapshot gains `gpu_info`, `cpu_topology`, `storage_view`.

## Alerts / automation / media
- `GET|POST /api/alerts`, `DELETE /api/alerts/<id>` — rules
  {type, threshold, duration_s, cooldown_s, notify{event,chat,sound}}.
- `GET|POST /api/automation`, `DELETE /api/automation/<id>`,
  `POST /api/automation/<id>/control` {pause|resume|run|enable|disable},
  `POST /api/automation/preview` — jobs always execute through the
  verified transfer engine; deletions are never mirrored.
- `POST /api/media/party` {kind: preset|tone, name, freq, duration,
  waveform, volume, play_at} → SSE `media_party` on the receiving
  companion; `POST /api/media/party/stop`.
- SSE topics added: `mobile`, `alert`, `media_party`.

---

# v1.4.0 API additions (Phantom Signal)

All endpoints return `{"ok": true, ...}` or an error envelope
`{"ok": false, "error", "message"}`.  Unless marked **public**, endpoints
require the optional auth token when `require_token` is enabled.

## Voice
| Method/Path | Description |
| --- | --- |
| `POST /api/voice/call` | `{mode: call|room|ptt, targets: [{kind, device_id|client_id, host_device_id}]}` → creates + relays the call; `409 already_ringing` guards simultaneous requests |
| `POST /api/voice/answer` | `{call_id, accept}` — accept/decline; mirrored calls forward to the origin companion |
| `POST /api/voice/signal` | `{call_id, kind: offer|answer|ice, payload}` — opaque SDP/ICE relay (≤64 KiB, ≤2000 msgs/call) |
| `POST /api/voice/end` | `{call_id, reason}` |
| `POST /api/voice/room/join` · `/room/leave` | `{room}` — group voice rooms |
| `POST /api/voice/relay` | **public** host-to-host channel: `{etype, payload, origin_host, for_device?, for?}` — delivers to a local client/computer and creates mirrored call records on invites |
| `GET /api/voice/state` | active calls (incl. mirrors with `origin_host`) + rooms |
| `GET /api/voice/history?limit=` | append-only call history (missed/rejected marked) |

SSE topics: desktop `voice` (invites/signals/updates); phones receive
`{type: "voice"}` on the private mobile stream. Ring timeout 45 s →
`missed` + GhostChat system message.

## Broadcast
| Method/Path | Description |
| --- | --- |
| `GET /api/broadcast/profiles` | 7 built-ins + custom + `tts_available` + quiet hours |
| `POST /api/broadcast/profile` | `{name, tone, voice, urgency}` |
| `GET/POST/DELETE /api/broadcast/group[s]` | device groups |
| `POST /api/broadcast/announce` | `{text, tone, voice, urgency, ack, targets}` → per-target states `delivered` / `delivered-silently` / `unavailable` |
| `POST /api/broadcast/incoming` · `/relay` | **public** peer delivery |
| `POST /api/broadcast/ack` | `{id, who}` |
| `GET/POST/DELETE /api/broadcast/schedule[s]` | HH:MM daily/weekday schedules (DST-safe) |
| `GET /api/broadcast/history?limit=` | with `results`, `tone_state`, `tts_state`, `acknowledged` |

Quiet hours: `broadcast_quiet_hours` config `"22:00-07:00"` (midnight
wrap). Emergency urgency overrides only for the sender; participation
settings are never overridden.

## Media
| Method/Path | Description |
| --- | --- |
| `GET /api/media/rooms` · `POST /api/media/room` · `DELETE /api/media/room/:id` | room lifecycle (cap 24) |
| `POST /api/media/room/join` · `/leave` | membership |
| `POST /api/media/enqueue` | `{room_id, path}` — PathSandbox-authorized only; computes cross-host stream URLs |
| `POST /api/media/dequeue` · `/reorder` | queue edits |
| `POST /api/media/control` | `{room_id, action: play|pause|stop|seek|next|prev|repeat|shuffle|volume, ...}` — volume clamped to 0.8 |
| `GET /api/media/state?room_id=` | public room state + `server_now` + `offset_s` |
| `GET /api/media/stream?path=` | Range-capable authorized streaming with media MIME types |
| `GET /api/media/history` | last 200 room actions |

## Mesh
| Method/Path | Description |
| --- | --- |
| `GET /api/mesh/status` | failover/mdns flags, tracked exchange peers, recovery errors, claims |
| `POST /api/mesh/claim` | **experimental** (flag-gated): `{client_id, client_secret, inbox_since}` → secret-hash-verified claim + inbox tail pull |
| `GET /api/mesh/ledger/:client_id?since=` | **public** host-to-host inbox ledger (flag-gated) |

## Clipboard
| Method/Path | Description |
| --- | --- |
| `GET /api/clipboard/status` | flags, caps, retention, native-helper detection |
| `GET /api/clipboard/items?q=&limit=` | history search (pinned first) |
| `POST /api/clipboard/send` | `{kind, text|data_url, targets}` → per-target states; `duplicate: true` on suppression |
| `POST /api/clipboard/pin` · `/clear` | `{id, pinned}` / `{pins_too}` |
| `POST /api/clipboard/incoming` · `/relay` | **public** peer delivery (respects pause/private; hop ≤ 1) |
| `POST /api/clipboard/os-copy` | native-helper clipboard write |
| `POST /api/clipboard/settings` | `{clipboard_paused, clipboard_private_mode, clipboard_auto_enabled, clipboard_retention_days, clipboard_max_bytes, clipboard_max_image_bytes}` |

## TLS (v1.4.0 usability)
| Method/Path | Description |
| --- | --- |
| `GET /api/tls/status` | enabled/dual/ports, cert expiry + SANs, secure-feature list, plain-language notes |
| `GET /api/tls/cert` | **public** PEM download for phone installation |

## System
- `GET /api/identity` now reports capabilities: `voice`, `screen`,
  `broadcast`, `media`, `failover`, `clipboard`, `clipboard_auto`.
- Migration schema 3 runs automatically with backup
  (`migrations/v<from>-<stamp>/`).
