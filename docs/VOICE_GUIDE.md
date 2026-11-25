# Phantom Voice — User Guide (v1.4.0)

Phantom Voice turns the household LAN into a private voice network: call
any participating computer or phone, join named voice rooms, and see
honestly whether audio flows browser-to-browser over WebRTC or falls
back to the classic PCM intercom.

## What it is

Calls run browser↔browser over WebRTC with **host candidates only**
(`iceServers: []`): on one subnet the peers connect directly and no
external STUN/TURN service is ever contacted. The companions relay only
opaque signaling blobs (offers, answers, ICE candidates) between the
browsers and between companions — audio never transits a companion, and
the companion cannot decode it.

Three modes:

| Mode | What it does |
| --- | --- |
| **Call** | Direct 1:1 computer↔computer, computer↔phone, phone↔phone |
| **Room** | Named group sessions (e.g. `kitchen`); members join/leave freely |
| **PTT** | The classic push-to-talk intercom (unchanged, compatibility mode) |

## Making a call

1. Open **Voice** in the sidebar. Pick a device from the dropdown
   (💻 computers from the fleet, 📱 phones hosted by any companion).
2. Press **☎ Call**. The other device rings — desktops show a call card
   with sfx; phones get a full-screen ring with Accept/Decline.
3. The recipient **must accept**. There is no auto-answer anywhere in
   Phantom Network, and nothing records.
4. In-call: mute, live level meter with voice-activity coloring,
   microphone picker, output picker (where the browser supports
   `setSinkId`), connection quality (jitter/loss), duration, and the
   transport indicator:
   - `direct LAN (host↔host)` — WebRTC host candidates connected
   - `stun` / `relay` — only appears if you configured STUN servers
   - `FAILED` — the browser could not connect; try the intercom

## Cross-computer calls

A call from computer A to computer B relays the invite to B's companion,
which creates a *mirrored* call record and rings B's dashboard. B's
accept/hangup actions are forwarded back to A (the origin companion owns
the call state). This also works through the fleet: calling a phone
hosted on another companion routes through its host automatically.

## Missed calls

A ring that goes unanswered for 45 seconds becomes **missed**, appears in
the call history with a ⊘ marker, and is recorded as a GhostChat system
message so the recipient sees it even after a companion restart.

## Browser requirements

- **Microphone**: needs a *secure context* — `localhost`, or HTTPS with a
  trusted certificate (see Settings → HTTPS for the one-click status and
  certificate download). Plain HTTP works for everything except capture.
- **Browsers**: Chromium-family desktops have full support (including
  output-device picking); Safari on iOS works over HTTPS; Firefox lacks
  output selection (uses the default).
- Phones sync manually — mobile OSes decide when a page may use the mic;
  the call must be accepted on-screen (this is platform policy, and we
  will not pretend otherwise).

## Screen sharing (optional, opt-in)

While in a call, press **🖥 Share screen** and choose a screen, window or
tab (browser-native `getDisplayMedia`). The other side gets a shared-
screen card and can open it in a window. A visible indicator stays up for
the whole session; either side can stop it. Remote keyboard/mouse control
is deliberately **not implemented** in this release, and there is no
capture that the sharing user did not explicitly start.

## Privacy contract

- Every call needs an explicit accept; no hidden capture, no recording,
  no telephone interception, no covert microphone activation.
- Call history stays on your companion (`call_history.jsonl`).
- Disabling voice: set `voice_enabled: false` in config (or via API).
