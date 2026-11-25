# Phantom Mobile — Mobile Client Guide

Phones and tablets join Phantom Network as **independent participants**.
The companion that serves the mobile page is the client's **host**: it
stores the identity, relays messages and holds the inbox. The phone itself
runs only a browser — no server, no background app, no cloud.

## Registering a phone

1. Open Phantom Network on a computer → Settings → **Add Mobile Device**.
2. Scan the QR with the phone camera (or type the LAN URL, e.g.
   `http://192.168.1.10:8415/m`).
3. On the phone: choose a device name and type → **Register**.
4. The phone appears immediately in the Network Radar and in GhostChat on
   every participating device.

The QR contains only the LAN URL of `/m` — no tokens, no secrets. Manual
registration is exactly the same flow via the URL. If optional shared-token
auth is enabled, the token must be entered manually; it is never embedded
automatically.

## Identity and storage recovery

- Identity (`mob_…` client id + secret) lives in the browser's
  `localStorage`. **Refreshing never creates a new identity.**
- **Identity protection (v1.3.1)**: the secret is sent to the browser
  exactly once, at registration. Re-attaching an existing client id
  requires that secret — a bare id is refused (HTTP 401) and discloses
  nothing. Wrong credentials fail closed on every mobile endpoint
  (heartbeat, inbox, send, ack, stream). Lost storage = Recover identity
  (a fresh identity) + remove the orphaned entry in Settings.
- Cleared browser storage → the phone registers as a NEW participant. The
  orphaned entry can be renamed or removed in Settings (Phantom Mobile list).
- Multiple phones per companion are supported.

## Presence and honesty

- Heartbeats every ~10 s while the browser tab is alive. After the TTL
  (`mobile_client_ttl_ms`, 45 s default) the client is marked **offline**
  with its real `last_seen` — nothing pretends a locked phone is online.
- Background delivery is bounded by browser reality: when the tab is killed
  the phone cannot receive pushes without platform push infrastructure.
  Phantom deliberately stays local; messages wait in the host-side inbox and
  arrive at the next sync.

## Messaging semantics

- **delivered** is set only when the phone acknowledges receipt — host
  receipt alone never upgrades the status (visible as ✓ vs ✓✓).
- Offline senders see `queued`; reconnection syncs the inbox since the last
  fetch (duplicate-safe by message id).
- Reactions, replies, edits and deletion tombstones propagate between
  computers and phones through the hosts.
- Voice messages: recorded in-browser (≤60 s), stored in the host chat file
  store, size-capped (`mobile_max_attachment_mb`, default 50).

## Files

- Phones send photos/videos/documents **into explicitly authorized (rw)
  directories** of computers — nothing is read from the phone without user
  selection. Camera capture uses the browser capture control.
- Downloads: phones fetch files from authorized shares via `/api/fs/download`.
- Uploads are chunked (512 KiB) with progress and honest errors; a phone
  never appears to offer unrestricted filesystem access
  (`capabilities.file_commander = false`).

## API surface (host side)

| Endpoint | Purpose |
| --- | --- |
| `POST /api/mobile/register` | register / re-attach (returns secret ONCE) |
| `POST /api/mobile/heartbeat` | presence |
| `POST /api/mobile/inbox` | sync inbox since ts |
| `POST /api/mobile/chat/send` | send as the phone |
| `POST /api/mobile/chat/ack` | delivered/read acknowledgment |
| `POST /api/mobile/chat/mutate` | react/edit/delete |
| `GET  /api/mobile/stream?client_id=&secret=` | private SSE feed |
| `GET  /api/mobile/clients` | fleet listing (no secrets) |
| `POST /api/mobile/clients/<id>/rename` `…/remove` | management (local admin) |

## Hosting requirements

- The host must be reachable at the advertised LAN address; a phone keeps
  working through ITS host while the host is up. If the host is down, the
  phone cannot reach the fleet (documented host dependency).
- `mobile_host_enabled: false` disables hosting; registered entries remain
  visible but cannot reconnect.
