# Phantom Clipboard — User Guide (v1.4.0)

One shared clipboard for the household fleet: commands, links, code
snippets, multiline text and small screenshots — sent device-to-device
over your LAN, with history, pinned snippets and search. No cloud, no
new credentials.

## Manual send (works everywhere)

1. Copy anything on any device and press **⇩ From OS clipboard** (desktop
   dashboard) — the content lands in history.
2. Select target device chips and press **⇪ Send to selected**.
3. On the receiving device, the entry appears in history; **⧉ copy**
   writes it back to that device's clipboard (browser API, with the
   companion's native helper as fallback).

Everything is supported as text: multiline commands, code, URLs, any
Unicode. Images ride as small PNG/JPEG data URLs and show as thumbnails.

## Automatic synchronization (opt-in)

OFF by default. When enabled in Settings → Clipboard, the COMPANION polls
the desktop clipboard through native helpers — `xclip`/`xsel` (X11),
`wl-clipboard` (Wayland), PowerShell (Windows), `pbcopy`/`pbpaste`
(macOS) — every 2 s and shares NEW content with participating peers. The
dashboard does **not** need to be open for companion-side sync; that is
the point of the helpers. Status, including which helper was found, is
shown honestly in the Clipboard view.

Phones: mobile OSes do not let a web page watch the clipboard, so
**phones sync manually** (copy from history, send to devices). Stated
plainly, not hidden.

## Safety rails

- **Pause everything** — one toggle stops all sending AND receiving.
- **Private mode** — incoming entries are dropped (send your own, share
  nothing).
- **Per-device participation** — devices can opt out of auto-sync.
- **Retention** — history is pruned after N days (default 30); pinned
  snippets never age out.
- **Clear history** — keeps pinned snippets unless you say otherwise.
- **Size caps** — text 256 KiB, images 2 MiB by default (configurable);
  oversized payloads are rejected with the exact size, never truncated.
- **Duplicate suppression** — identical content is not re-stored or
  re-delivered (SHA-256 payload hashing).
- **Loop prevention** — entries carry origin + hop; a companion never
  re-broadcasts its own content, receiving never triggers sending.
- **Nothing executes** — clipboard content is inert text; Phantom never
  runs it. We make NO claim of detecting passwords or other sensitive
  content — you control what you copy.

## History & pins

Search filters history by content; **★ pin** keeps a snippet above the
list forever (great for the household Wi-Fi password — pinned locally,
never auto-sent anywhere).

## Where it lives

History: `clipboard_history.json` in the companion data dir. Delivery
reuses the existing peer channel and the mobile SSE stream — same
transport as chat, same trust model, nothing new to configure.
