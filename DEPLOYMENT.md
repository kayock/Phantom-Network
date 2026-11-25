# DEPLOYMENT.md

Installing and running PHANTOM NETWORK v1.2.1 on Windows 11 and Ubuntu.

## 1. Requirements

| | Minimum | Notes |
|---|---|---|
| Python | 3.9+ (3.12 recommended) | Windows: python.org or winget; Ubuntu: preinstalled (22.04 ships 3.10, 24.04 ships 3.12) |
| Network | IPv4 LAN | same subnet for auto-discovery; other subnets via manual registration |
| Disk | ~40 MB | app + logs + caches (data grows with message/event history only) |
| Optional | `psutil` (`pip install psutil`) | richer telemetry; the companion degrades gracefully without it |
| Audio playback | Windows: built-in; Ubuntu: `paplay` (PulseAudio/PipeWire) or `aplay` (ALSA) or `ffmpeg` | the Diagnostics page shows which player was found |

No Docker, no database, no compiler, no admin rights **required** (the
Ubuntu installer uses sudo only for the optional system-wide shortcut and
firewall hints).

## 2. Install

### Ubuntu 22.04 / 24.04 (script)

```bash
cd Phantom-Network-v1.2.0
sudo bash packaging/install-ubuntu.sh
```

The script installs to `/opt/phantom-network`, creates a
`phantom-network` launcher (menu entry), an optional `~/.local/bin/phantom-network`
command, and prints the firewall lines (it does **not** change the firewall
itself). It does NOT enable auto-start unless you answer "y" when prompted.

Manual equivalent:

```bash
mkdir -p ~/.local/share/phantom-network
cp -r . ~/.local/share/phantom-network/
cd ~/.local/share/phantom-network
python3 -m companion            # serves http://0.0.0.0:8415
```

### Windows 11 (script)

From an **Administrator** PowerShell in the extracted folder:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\install-windows.ps1
```

Installs to `%LOCALAPPDATA%\PhantomNetwork`, creates a Desktop shortcut and
a Start-menu entry, and prints any Defender Firewall commands for you to
run if you want LAN peers to connect (it does not modify the firewall).

Manual equivalent:

```powershell
cd C:\Phantom-Network-v1.2.0
python -m companion
```

### Portable distributions

* Linux: `packaging/make-portable-linux.sh` produces
  `phantom-network-portable-linux.tar.gz` — extract anywhere, run
  `./phantom-network.sh`. All state stays in the extracted `data/` folder
  (`portable.marker`).
* Windows: copy the source folder anywhere, create `portable.marker` next
  to `companion/`, run `packaging\run-phantom-windows.bat`.

## 3. First run

1. Start the companion (script/launcher/`python -m companion`).
2. Open `http://localhost:8415` — the boot screen connects to the local
   companion and shows the Command Center.
3. Settings → **Device identity**: give the machine a friendly name.
4. Settings → **Authorized directories**: shares are `~` (home, read-write)
   by default; add e.g. `/mnt/media` or `D:\Video` as needed. Everything
   outside the listed roots is invisible to other devices.
5. Repeat on every participating computer. Peers appear in the Network
   Radar within ~10 s (UDP broadcast on port 8414/udp).

## 4. Firewall (only if discovery/UI unreachable from other machines)

| Platform | Command |
|---|---|
| Ubuntu (ufw) | `sudo ufw allow 8415/tcp` and `sudo ufw allow 8414/udp` |
| Ubuntu (firewalld) | `sudo firewall-cmd --permanent --add-port=8415/tcp --add-port=8414/udp && sudo firewall-cmd --reload` |
| Windows | `netsh advfirewall firewall add rule name="Phantom Network API" dir=in action=allow protocol=TCP localport=8415` and the same with `protocol=UDP localport=8414` |

These commands are printed by the installers for you to run manually —
Phantom Network never modifies firewall rules itself.

## 5. Phones & tablets

Open `http://<computer-ip>:8415` in a mobile browser. Everything except
microphone features works over plain HTTP. The **intercom microphone**
requires a *secure context*:

* easiest: open the UI on the computer itself, or
* enable the companion's HTTPS mode (§7), or
* Chrome/Edge desktop on a trusted LAN: add the origin under
  `chrome://flags/#unsafely-treat-insecure-origin-as-secure`.

iPhone Safari will not grant `getUserMedia` on a plain-HTTP LAN address —
this is an honest platform restriction, documented in
KNOWN_LIMITATIONS.md.

## 6. Auto-start (opt-in only)

* Ubuntu: answer "y" during `install-ubuntu.sh` (writes a user systemd
  unit, `systemctl --user enable --now phantom-network`), or later:
  `systemctl --user enable phantom-network`.
* Windows: `install-windows.ps1` asks whether to add a registry
  `HKCU\...\Run` entry (defaults to **No**).

## 7. HTTPS mode (optional, enables phone microphones)

Generate a self-signed certificate once per machine:

```bash
# Ubuntu (openssl is preinstalled)
openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout ~/.config/phantom-network/tls.key \
  -out ~/.config/phantom-network/tls.crt \
  -subj "/CN=phantom-network" \
  -addext "subjectAltName=IP:192.168.1.10,DNS:phantom.local"
```

Place `tls.crt` + `tls.key` in the data directory and start the companion
with `--tls`; it then serves `https://…:8415`. Browsers will warn about the
self-signed certificate (accept for private use). On Windows, generate the
pair the same way if OpenSSL is available (Git for Windows ships it).

## 8. Uninstall

* Ubuntu: `bash packaging/uninstall-ubuntu.sh` (removes /opt copy, launcher
  and optional systemd unit; `--purge` also deletes `~/.config/phantom-network`).
* Windows: `powershell -ExecutionPolicy Bypass -File packaging\uninstall-windows.ps1`
  (removes shortcuts and `%LOCALAPPDATA%\PhantomNetwork` unless `-KeepData`).
* Portable: delete the folder.

## 9. Three-computer quick checklist

1. `install-ubuntu.sh` on the two Ubuntu machines,
   `install-windows.ps1` on the Windows box.
2. Open `http://localhost:8415` on each — confirm the Diagnostics page is
   green on all three.
3. From any one of them, Radar should list the other two (passive discovery).
   Machines on other subnets: Radar → **+ Register device** → IP.
4. File Commander: left pane = Windows, right pane = Ubuntu Station; copy
   something large and watch it stream + verify in the Transfer Center.
5. Optional hardening on a shared network: Settings → participation →
   *require auth token*, set the same token on all three devices.

## 10. Running alongside AI inference workloads

The companion idles near 0 % CPU (threaded server + 3 s UDP beacon + 5 s
telemetry sample). Recommended settings for coexistence with local LLM
inference:

* Transfer Center → bandwidth limit (e.g. 10–50 MB/s) so LAN copies don't
  starve model downloads or NFS/other traffic.
* Settings → max concurrent jobs 1–2 for huge model copies.
* The Fleet Monitor page shows the companion's own RSS (~25–40 MB) so you
  can verify it stays out of the way.
