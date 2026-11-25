# Upgrade Guide — to v1.3.1

One procedure per machine, about five minutes. Nothing is reinstalled,
re-qualified or re-configured beyond the steps below.

## What v1.3.1 changes

Stabilization release on top of v1.3.0 — same feature set, corrected:

1. **Mobile identity protection** (security): an existing mobile client id
   can no longer be re-attached without its secret; the secret is never
   disclosed by registration, and invalid credentials fail closed on every
   mobile endpoint.
2. **Mobile UI fixes**: the mobile interface now boots correctly (a
   syntax error in v1.3.0 prevented it), messages sent by the host appear
   correctly as received, conversation keys are consistent across
   same-host and cross-host delivery, `peer_probe` works for peers on
   custom ports.
3. **Reliability**: automation job history no longer self-reports failure
   (v1.3.0 logging bug), delivery/read receipts route even when the peer's
   cached online flag is stale, queued mobile messages retry promptly.
4. Fresh v1.3.1 desktop + mobile screenshots, SW cache version bumped.

## Preserved by the upgrade

Companion identity (device id/name), registered peers, authorized
directories (shares), transfer history, message history, optional auth
settings (`require_token`/`auth_token`), desktop launchers, user
configuration. The automatic migration backs up every state file before
touching anything (`migrations/v1-<timestamp>/`).

## Procedure (v1.2.2 or v1.3.0 → v1.3.1)

1. Stop the companion (`Ctrl+C` / service stop).
2. Replace the application directory with the v1.3.1 tree.
3. Keep the data directory untouched
   (`~/.config/phantom-network` on Linux, `%APPDATA%\PhantomNetwork` on
   Windows, `./data` in portable mode).
4. Start: `python -m companion` — the migration line appears in the log.
5. Verify (2 minutes):
   - `/api/health` → version 1.3.1;
   - Network Radar shows the other machines ONLINE;
   - one small SHA-256-verified file transfer;
   - mobile: open `/m` on the phone → if already registered, it reconnects
     with its existing identity (no re-pairing);
   - send one message computer→phone and one phone→computer.

## Rollback

Restore the previous application tree and copy the state files back from
`migrations/<…>/` — they are byte-identical snapshots of the pre-upgrade
state. No data loss.

## Mixed fleets

v1.3.1 accepts v1.2.x and v1.3.0 peers. Mobile clients require their host
on v1.3.1 or v1.3.0; v1.3.0 hosts work but phones should be refreshed to
the v1.3.1 page (reload once).

## Cursor's deployment assignment (scoped)

Install the upgrade on the three machines, verify mobile messaging per
step 5, publish the release. **No further exhaustive physical
qualification campaign is required** — the 50 GB transfer qualification
of v1.2.2 remains valid (the engine is byte-identical).

---

# Upgrading to v1.4.0 (from v1.3.1 / v1.3.2)

## What the upgrade preserves

Everything: companion identity, mobile client identities + secrets +
inboxes, registered peers, shares, message history, transfer history,
favorites, automation jobs, notification preferences, auth settings,
TLS configuration. Migration schema **3** runs automatically on first
boot, **backs up** every state file it touches to
`data/migrations/v<from>-<timestamp>/`, and only ADDS the new feature
flags (voice/screen/broadcast/media ON; mobile failover OFF;
clipboard auto-sync OFF; clipboard caps/retention defaults).

## Steps

1. Stop the running companion.
2. (Cheap extra safety) copy the whole data dir aside — the migration
   backup covers state files, not your shares.
3. Replace the application (unzip the release over the install, or
   re-run the installer — both preserve the data dir).
4. Start the companion. The log shows the migration step and
   `companion serving on 0.0.0.0:<port>`.
5. Hard-refresh the dashboard (Ctrl+Shift+R) — the service worker bumps
   to v1.4.0 and offers "update available" automatically.

## Rollback

Stop the companion, restore the previous application version, and copy
the migration backup files back over `data/` if you want the pre-upgrade
flags removed. v1.3.1/v1.3.2 companions ignore unknown config keys, so
a v1.4.0 config is also safe to keep when rolling back.

## Mixed-version behavior

Older (≤ v1.3.2) companions simply ignore the new endpoints/topics;
they keep chatting, transferring and registering exactly as before.
Newer capabilities are offered only to peers whose identity advertises
them (capability flags in `/api/identity`).

## New config keys (all optional)

```json
{
  "voice_enabled": true,        "screen_enabled": true,
  "broadcast_enabled": true,    "broadcast_tts_enabled": false,
  "broadcast_quiet_hours": "22:00-07:00",
  "media_enabled": true,
  "mdns_enabled": false,
  "mobile_failover_enabled": false,
  "voice_stun_servers": [],
  "clipboard_enabled": true,    "clipboard_auto_enabled": false,
  "clipboard_paused": false,    "clipboard_private_mode": false,
  "clipboard_participating": true, "clipboard_retention_days": 30,
  "clipboard_max_bytes": 262144, "clipboard_max_image_bytes": 2097152,
  "tls_dual": true, "tls_port": 0
}
```
