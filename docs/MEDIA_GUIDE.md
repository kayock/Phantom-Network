# Phantom Media — User Guide (v1.4.0)

Shared music/video queues and synchronized playback across the
household — built on the same authorized-directory model as the File
Commander, with no new server software.

## Rooms

Create a room (e.g. "Kitchen"), then join it from any participating
device. Each room has its own queue, playback state, repeat/shuffle and
per-device volumes. Joining a room puts your device into the synced
playback group; leaving it stops your local playback but does not disturb
others.

## Queuing files

Press **📂 Browse files…** — the browser shows the SAME authorized
directories the File Commander uses (the PathSandbox validates every
path; nothing outside a share can be queued). Audio/video files are
listed; **＋ queue** adds them to the room queue.

## Streaming model (honest)

Files stream from the machine that OWNS them over `/api/media/stream`
with **HTTP Range** support — seeking works, and nothing is copied to
every device before playback. The companion never transcodes: your
browser plays what it can decode (MP3, M4A/AAC, FLAC, WAV, OGG, Opus,
MP4, WebM, MKV container files vary by browser codec support). If the
owning device is unreachable, the item reports honestly and does not
play.

## Playback control

Play / Pause / Stop / seek / next / prev / repeat-all / shuffle are room
commands — every joined device follows the room state (SSE
`media_state`). Volume is per device with a **0.80 ceiling** that matches
the companion-wide audible-amplitude limit; dragging past it clamps.

## Synchronization precision — the honest numbers

Playback sync uses server timestamps plus a measured per-device clock
offset. Measured behavior on a wired/Wi-Fi LAN: **±50–150 ms** alignment
across devices. That is good for music around the house; it is NOT
sample-accurate and we do not claim it. The Media view shows the measured
clock offset so you can see your own numbers.

## From a phone

The mobile **Media** tab lists rooms with play/pause/next/stop controls
and the honest sync note. Start playback on a computer, control it from
the couch.

## History

Room actions (play/seek/skip…) are recorded in `media_history.jsonl`
(last 200 events) for the "what was that song?" moments.

## Non-goals

No heavyweight media server, no transcoding, no metadata scraping, no
DRM circumvention, no acquisition services — use media you own or that
is licensed for your use.
