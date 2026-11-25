# Cursor Deployment Handoff — v1.3.1 (scoped assignment)

## Your assignment is limited to:

1. **Install** the v1.3.1 upgrade on Ubuntu Station, Ubuntu Portable and
   Windows 11 (UPGRADE_GUIDE.md — one procedure, ~5 min per machine).
2. **Verify mobile messaging**: phone reconnects with its existing
   identity; one message each direction; read receipts visible.
3. **Publish** the release (zip is prebuilt; SHA256SUMS included).

## Do NOT:

- repeat the 50 GB / 8 GiB physical qualification (transfer engine is
  byte-identical to the physically verified v1.2.2);
- re-run the full multi-day verification campaign;
- modify firewall rules, certificate stores or startup services;
- re-pair mobile devices (identities persist; just reload `/m`).

## Acceptance (15 minutes total, all machines)

- `/api/health` → 1.3.1 on all three machines.
- Radar: three machines ONLINE.
- One verified file transfer Station→Windows.
- Phone: message in + message out + receipts.
- Diagnostics self-test passes.
