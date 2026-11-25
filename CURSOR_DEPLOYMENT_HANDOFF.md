# CURSOR_DEPLOYMENT_HANDOFF.md — PHANTOM NETWORK v1.2.1

Field guide for installing and verifying Phantom Network on **three
physical computers**:

| Machine | OS | Role |
|---|---|---|
| Ubuntu Station | Ubuntu 24.04+ (desktop) | primary dashboard, dashboard host |
| Ubuntu Portable | Ubuntu 22.04+ (laptop/NUC) | second companion, mobility tests |
| Windows 11 PC | Windows 11 | Windows path/playback verification |

Rules for whoever executes this (Cursor or a human):

* Never point Phantom Network at irreplaceable data during first contact —
  use disposable test directories until every stage passes.
* Record what each stage actually printed. Anything not executed must stay
  marked **NOT RUN** in the results — never inferred.
* This document was written from a Linux verification environment; the
  Windows stages are precise but **unexecuted** until a real Windows
  machine runs them.

---

## Stage A — Ubuntu Station

**A1. Prerequisites (2 minutes)**

```bash
python3 --version            # must be ≥ 3.9
curl --version | head -1     # used by the launcher health check
xdg-open --version 2>/dev/null || echo "xdg-open missing (install xdg-utils)"
df -h ~                      # ≥ 5 GB free recommended for Stage E
```

**A2. Install (no unrelated directories touched)**

```bash
cd ~/Downloads
unzip Phantom-Network-v1.2.0.zip
cd Phantom-Network-v1.2.0
sudo bash packaging/install-ubuntu.sh        # answer N to autostart first
```

The installer creates:
- program files: `/opt/phantom-network` (root-owned, read-only for users)
- user launcher: `~/.local/bin/phantom-network` + desktop entry
- at first launch: config in `~/.config/phantom-network`,
  logs in `~/.local/state/phantom-network`

**A3. Verify ordinary-user launcher (the v1.1.0 defect class)**

```bash
ls -ld /opt/phantom-network                     # root-owned, NOT group/world writable
touch /opt/phantom-network/probe 2>&1 | grep -q "Permission denied" \
  && echo "OK: program dir is read-only for users"
~/.local/bin/phantom-network                    # must start WITHOUT sudo
sleep 3
curl -s http://127.0.0.1:8415/api/health        # {"ok": true, "version": "1.2.1"}
ls ~/.local/state/phantom-network/              # companion.out lives HERE, not in /opt
find /opt/phantom-network -newer ~/.local/bin/phantom-network -type f
#   → must print NOTHING: zero writes into the program directory
```

**A4. Verify the command center + first-run wizard**

1. Open http://localhost:8415 — the first-run wizard must appear.
2. Authorize ONE disposable test directory **read-write** via the wizard.
3. Settings → confirm the share is listed with its mode.
4. Diagnostics view → run self-test; expect green checks; export the
   diagnostic report (JSON) and keep it for the release notes.

**A5. Companion shutdown/restart**

```bash
pkill -f "python3 -m companion"; sleep 2
~/.local/bin/phantom-network & sleep 3
curl -s http://127.0.0.1:8415/api/health    # back online, same device_id
curl -s http://127.0.0.1:8415/api/identity | grep -o '"device_id":"[^"]*"'
```

## Stage B — Ubuntu Portable

**B1. Install** (same steps as A2; answer N to autostart).

**B2. Discovery & registration**

1. Start both companions. On Station: Network Radar — Portable should
   appear as *discovered*; register it. Radar must show exactly ONE entry
   per machine (no duplicates).
2. `curl -s http://localhost:8415/api/peers` on Station — Portable listed
   `online` with matching `device_id` from B's identity.

**B3. Messaging**

GhostChat on Station → send a direct message to Portable → status must
become `delivered` (acknowledged by B), reply, verify read receipt.

**B4. Disposable transfers (copy, never move, in this stage)**

1. On Portable create `~/pn-test/src` with ~50 files (some with spaces and
   Unicode names). Authorize it read-write.
2. Station File Commander → browse Portable's share → copy into a Station
   test dir. Verify: 50/50 files, Transfer Center `completed`,
   `verify=ok`.
3. `sha256sum` a 1 GB test file on both machines — must match.
4. Pause a 2 GB transfer mid-way; pause; resume; verify completion.
5. Cancel a 1 GB transfer — `.part` retained, source untouched
   (`find ~/pn-test/src -size 1G | wc -l` unchanged).

**B5. Move qualification (destructive — only after B4 passes)**

Move the 2 GB file Station→Portable. Required outcome: `completed` +
source gone; OR `completed_partial` + source intact + clear error. Never
plain `completed` with the source still present.

## Stage C — Windows 11

> **Stage C requires separate user authorization before execution.**
> Everything below was source-audited and statically validated (byte scan,
> quote/brace sanity) but NOT executed on Windows.

**C1. Prepare**

- Install Python 3.11+ from python.org, tick *"Add python.exe to PATH"*.
- Create `C:\Users\<you>\pn-test` (disposable test dir).

**C2. Install**

```powershell
cd $env:USERPROFILE\Downloads
Expand-Archive Phantom-Network-v1.2.0.zip -DestinationPath .
cd Phantom-Network-v1.2.0
powershell -ExecutionPolicy Bypass -File packaging\install-windows.ps1
```

Expected: app in `%LOCALAPPDATA%\PhantomNetwork`, data dir created at
`%APPDATA%\PhantomNetwork`, shortcuts on Desktop + Start Menu, venv for
optional psutil.

**C3. Verify**

```powershell
Get-Content "$env:LOCALAPPDATA\PhantomNetwork\packaging\run-phantom-windows.bat"   # human review
Start-Process "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Phantom Network.lnk"
curl.exe -s http://127.0.0.1:8415/api/health        # {"ok": true ...}
```

**C4. Windows path handling**

1. Authorize `C:\Users\<you>\pn-test` read-write via the wizard.
2. From Ubuntu Station, copy files into/out of the Windows share:
   - name with spaces ✓  - Unicode name ✓  - 200+ char name ✓
   - a file named `aux.txt` on the LINUX side pulled TO Windows must fail
     with `invalid_dest_name` (explicit, never renamed) — this validates
     the v1.2.0 Windows-name guard end-to-end.
3. Verify `\\UNC\paths` behave per KNOWN_LIMITATIONS (best effort).

**C5. Cross-machine transfers** — repeat B4 in both directions
(Windows→Linux and Linux→Windows), including pause/resume and checksums
(`Get-FileHash` on Windows, `sha256sum` on Linux).

## Stage D — Physical audio & mobile

1. Audio Command → play each of the 8 presets on Station speakers → human
   confirms audibility (the UI only ever claims *handed to player*).
2. Intercom: open the Intercom view, grant microphone (secure context —
   localhost counts as secure), push-to-talk between Station and Portable.
3. Mobile: open Station's UI from an Android/iPhone browser over the LAN —
   microphone will be BLOCKED on plain HTTP (browser policy); document the
   observed behavior; audio playback participation should work.
4. Signal Lab: run the on-screen FSK/CRC experiments between two laptops'
   speakers in a quiet room. Record SNR observations. All DSP correctness
   is already covered by the offline test suite.

## Stage E — Large model file qualification

1. Disposable generated files first: 2 GB → 4 GB → 8 GB (`head -c 8G
   /dev/urandom > test.bin`), verify SHA-256 on both ends independently.
2. Interrupt one 4 GB+ transfer mid-flight; resume; verify.
3. Only now copy a REAL GGUF model — **copy first** (never move) — verify
   SHA-256 both sides; keep the original untouched.
4. Moves of valuable files only after every copy test passed twice.
5. Record: wall time, average MB/s, companion RSS (`/api/stats`) — RSS
   must stay ~flat regardless of file size.

## Stage F — GitHub preparation

1. Create a fresh repository locally; copy the RELEASE ZIP contents (never
   a working tree with caches) — `packaging/make-dist.sh` output is clean.
2. Required: LICENSE (MIT) + THIRD-PARTY attribution if any bundled asset
   requires it (sounds are generated in-repo; no third-party assets).
3. Exclude: `dist/*.zip` artifacts, personal `data/` dirs, diagnostic
   exports containing hostnames/IPs, `BUILD_LOG.txt` if it embeds internal
   hostnames (it does not — verify).
4. Suggested repo layout: the archive contents at root; GitHub Actions
   optional: `python -m pytest tests/unit -q` (fast suite) on push.
5. **Do not publish without the owner's explicit approval.**

---

## Result recording template

| Stage | Step | Result (executed / NOT RUN) | Notes |
|---|---|---|---|
| A3 | ordinary-user launcher | | |
| A4 | wizard + diagnostics | | |
| B4 | 50-file copy + checksum | | |
| B5 | verified move | | |
| C4 | aux.txt explicit refusal | | |
| D1 | speaker audibility | | |
| E3 | GGUF copy checksums | | |
