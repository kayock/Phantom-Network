# Phantom Broadcast — User Guide (v1.4.0)

A private household announcement system: text banners, notification
tones and optional LOCAL speech — delivered to exactly the devices you
choose, respecting their participation settings and quiet hours.

## Sending an announcement

Open **Broadcast** in the sidebar:

1. **Type text** (the banner body), and/or pick a **tone**, and/or type
   **spoken text** for local text-to-speech.
2. **Pick targets**: click device chips (💻 computers, 📱 phones), or
   select a saved group from the dropdown, or leave everything selected
   for a fleet-wide announcement.
3. Optional: **ask for acknowledgment** — recipients can ack and the
   history shows who did.
4. Press **📣 Announce**. The history immediately shows per-target
   delivery states — `delivered`, `delivered-silently` (quiet hours or a
   silenced device), or `unavailable` (device offline). We never pretend
   an unreachable device heard anything.

## Built-in profiles

Seven ready-made profiles load into the composer with one click:
Dinner announcement, Incoming transmission, Household reminder, Transfer
completed, Device offline, Network maintenance, General household
announcement. Create your own with the **＋ Save profile** row.

## Tones

`chime`, `beacon`, `soft`, `success`, `alarm` — rendered by the existing
Audio Command engine with its amplitude caps on desktops, and by the
browser on phones. Devices with sounds disabled stay silent and receive
the visual banner only.

## Text-to-speech (local only)

Spoken text uses the LOCAL system voice — Linux `espeak-ng`, macOS `say`,
Windows SAPI via PowerShell — **no AI model, no cloud service, no
account**. Enable it in Settings (config key `broadcast_tts_enabled`);
every other device speaks its own copy when the announcement arrives. If
no local voice exists, the delivery report says `unavailable:
no_local_voice` instead of pretending.

## Quiet hours

Set a window (e.g. `22:00-07:00`, midnight wrap supported) in Settings.
During quiet hours tones and speech are suppressed on delivery —
banners still arrive. The **Emergency** urgency overrides quiet hours
*for the sending device only* and is shown clearly in history; silenced
devices stay silent. Nothing overrides participation settings
automatically.

## Schedules

`＋ Schedule` fires an announcement daily (or on chosen weekdays) at
HH:MM local time, with honest last-fired tracking so DST changes fire at
local wall time. Schedules respect quiet hours like any announcement.

## Groups

Saved target sets (`broadcast_groups.json`): "Upstairs", "Everyone with
a screen", "Kitchen + phones". Groups re-resolve membership at send
time, so a phone registered later still gets announcements.

## Integration

- **GhostChat**: broadcast announcements land in the Broadcast
  conversation and system alerts on phones.
- **Automation**: transfer/backup completion notifications can be
  surfaced through Broadcast (see UPGRADE_GUIDE for the config keys).

## Privacy

Announcement content transits your LAN between your companions only.
History (`broadcast_history.jsonl`) lives on the sending companion. No
cloud services are involved at any point.
