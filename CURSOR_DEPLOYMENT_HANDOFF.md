# Cursor Deployment Handoff — v1.4.0 (scoped assignment)

## Your assignment is limited to:

1. **Back up** the existing Phantom Network configuration on each
   machine (`cp -r <data-dir> <data-dir>.pre-v140` — one command).
2. **Upgrade the three installed companions** to v1.4.0
   (UPGRADE_GUIDE.md — one procedure, ~5 min per machine; migration
   schema 3 runs automatically and preserves identities, inboxes,
   peers, shares, history, automation).
3. **Verify the new version**:
   - `/api/health` → `1.4.0` on all three machines;
   - `/api/voice/state`, `/api/broadcast/profiles`,
     `/api/media/rooms`, `/api/clipboard/status`, `/api/mesh/status`
     all answer `ok: true`;
   - Radar shows all three machines ONLINE.
4. **Verify existing mobile identities and messaging**: phone reloads
   `/m` with its EXISTING identity (no re-pairing); one message each
   direction with receipts; conversation list shows the phone's NAME
   (not `mob_…`).
5. **Verify the new communications features** using participating
   devices:
   - one computer↔computer voice call answered + hung up (transport
     shows `direct LAN (host↔host)`);
   - one broadcast announcement delivered to two targets (check history
     states);
   - one clipboard send computer→computer with `⧉ copy` on receipt;
   - one media room with a queued file played and paused;
   - (optional HTTPS) `/api/tls/status` after `scripts/gen-lan-cert.sh`;
     install the cert on the phone, verify mic works on the HTTPS port,
     restore the phone identity across origins via More → Identity &
     recovery.
6. **Publish** the release (zip prebuilt; SHA256SUMS included).

## Do NOT:

- repeat the 50 GB physical transfer qualification (the transfer engine
  is byte-identical to the physically verified v1.2.2 line; the focused
  regression suites cover it);
- rebuild unrelated infrastructure or touch LocalForge;
- modify firewall rules, certificate trust stores or startup services;
- enable mobile failover or mDNS on production machines (experimental /
  optional, stay OFF);
- re-pair mobile devices (identities persist; use Restore only for the
  HTTP→HTTPS origin move).

## Acceptance (≈30 minutes total, all machines)

- All machines report `1.4.0` and pass the step-3 endpoint checks.
- Voice call completed with `direct LAN` transport; call history shows
  the ended call.
- Broadcast history shows per-target delivery states (no fabricated
  success).
- Clipboard round-trip verified; history searchable; pins survive
  restart.
- Media room plays a file from an authorized share on another machine.
- Phone: identity preserved, name resolved, message + receipt verified.
- Screenshots (desktop Voice/Broadcast/Media/Clipboard/Mesh + mobile
  Voice/Media/Clip) attached to the release notes from the browser
  qualification run (docs/screenshots/v140-*).

## Version facts (do not re-derive)

- Baseline: v1.3.1 tree (`c4e1c4b` development line) + published v1.3.2
  corrections (`78ea0f4`) integrated; NOT a rebuild from v1.2.2.
- Transfer engine: unchanged from the physically verified implementation.
- Migration: schema 2 → 3, automatic, backed up, rollback documented in
  UPGRADE_GUIDE.md.
