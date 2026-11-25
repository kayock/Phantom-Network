# Upgrade Guide — to v1.3.2

## From v1.3.1 → v1.3.2

Same procedure as below (replace the application tree, keep the data
directory). v1.3.2 is a targeted routing/discovery repair — no schema
migration beyond what v1.3.1 already applied.

# Upgrade Guide — to v1.3.1 (baseline)

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
