#!/usr/bin/env bash
# PHANTOM NETWORK v1.2.0 — Ubuntu installer
#
# Safety properties (audited):
#   * installs ONLY into /opt/phantom-network and the invoking user's
#     ~/.local/share/applications, ~/.local/bin and (optional) systemd user dir
#   * creates an application-owned virtualenv for the OPTIONAL psutil extra
#     (never a global pip install — compatible with externally-managed
#     Python environments, PEP 668)
#   * never chowns or chmods any pre-existing user directory
#   * never touches the firewall; never enables autostart without asking
set -e
# Destination for PROGRAM FILES. Runtime data (config, logs, transfer
# history) never lives here - see the XDG paths below.  PN_DEST allows a
# test installation into a non-root location (permission-boundary tests);
# on a real system keep the default.
DEST="${PN_DEST:-/opt/phantom-network}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }

# --- user-owned runtime locations (XDG; never inside $DEST) ---
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/phantom-network"
DATA_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/phantom-network"

echo "Installing Phantom Network v1.1.0 from $SRC → $DEST"
mkdir -p "$DEST"
rm -rf "$DEST/companion" "$DEST/web" "$DEST/packaging" "$DEST/scripts" "$DEST/sounds"
cp -r "$SRC/companion" "$SRC/web" "$DEST/"
for d in packaging scripts sounds tests; do
  [ -d "$SRC/$d" ] && cp -r "$SRC/$d" "$DEST/" || true
done
cp "$SRC"/*.md "$SRC"/LICENSE "$SRC"/requirements.txt "$SRC"/pytest.ini "$DEST/" 2>/dev/null || true

python3 -c "import sys; assert sys.version_info >= (3,9), 'Python 3.9+ required'"

# ---- optional dependency isolation (defect C fix) ----
# psutil is OPTIONAL: the companion runs fully without it. If the operator
# wants richer telemetry we build a dedicated venv INSIDE $DEST.
VENV_PY="$DEST/venv/bin/python3"
if python3 -c "import ensurepip" 2>/dev/null && [ "${PN_SKIP_VENV:-0}" != "1" ]; then
  echo "Creating application virtualenv for optional psutil…"
  python3 -m venv "$DEST/venv" 2>/dev/null || echo "  (venv unavailable — continuing without psutil)"
  if [ -x "$VENV_PY" ]; then
    "$VENV_PY" -m pip install --quiet --upgrade pip 2>/dev/null || true
    "$VENV_PY" -m pip install --quiet psutil 2>/dev/null \
      || echo "  (psutil not installed — telemetry falls back to /proc, still works)"
  fi
else
  echo "Skipping virtualenv (PN_SKIP_VENV=1 or ensurepip unavailable) — pure stdlib mode"
fi

# ---- launcher for the invoking user (defect D fix: touch only our files) ----
U="${SUDO_USER:-$USER}"
UHOME=$(getent passwd "$U" | cut -d: -f6)
APPS_DIR="$UHOME/.local/share/applications"
BIN_DIR="$UHOME/.local/bin"
# create ONLY our own subpaths; never chown/chmod existing trees
mkdir -p "$APPS_DIR" "$BIN_DIR"

PY_BIN="$VENV_PY"
[ -x "$PY_BIN" ] || PY_BIN="/usr/bin/python3"

cat > "$APPS_DIR/phantom-network.desktop" <<LAUNCH
[Desktop Entry]
Type=Application
Name=Phantom Network
Comment=LAN Command Center (companion + UI)
Exec=$BIN_DIR/phantom-network
Icon=$DEST/web/assets/logo.svg
Terminal=false
Categories=Network;
LAUNCH

cat > "$BIN_DIR/phantom-network" <<RUN
#!/usr/bin/env bash
# Start the companion (if not already running) and open the command center.
# Ordinary-user safe: logs go to the XDG state dir, state to the XDG config
# dir - nothing is ever written into the root-owned $DEST program directory.
STATE_DIR="$STATE_DIR"
DATA_DIR="$DATA_DIR"
PROG_DIR="$DEST"
PY_BIN="$PY_BIN"
mkdir -p "$STATE_DIR" "$DATA_DIR"
if ! curl -sf http://127.0.0.1:8415/api/health >/dev/null 2>&1; then
  (cd "$PROG_DIR" && nohup "$PY_BIN" -m companion --data-dir "$DATA_DIR" \
      >>"$STATE_DIR/companion.out" 2>&1 &)
  sleep 2
fi
xdg-open http://localhost:8415
RUN
chmod 755 "$BIN_DIR/phantom-network"
# fix ownership ONLY of files we just created inside our own dirs
chown "$U":"$U" "$APPS_DIR/phantom-network.desktop" "$BIN_DIR/phantom-network" 2>/dev/null || true

# ---- optional autostart (defect E fix: correct unit construction) ----
read -r -p "Enable auto-start at login (systemd --user)? [y/N] " ANS || ANS=N
if [ "$ANS" = "y" ]; then
  UNIT_DIR="$UHOME/.config/systemd/user"
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/phantom-network.service" <<UNIT
[Unit]
Description=Phantom Network companion
After=network-online.target

[Service]
Type=simple
ExecStart=$PY_BIN -m companion --data-dir $DATA_DIR
WorkingDirectory=$DEST
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT
  chown "$U":"$U" "$UNIT_DIR/phantom-network.service"
  runuser -u "$U" -- bash -c 'systemctl --user daemon-reload && systemctl --user enable --now phantom-network' \
    && echo "auto-start enabled (stop with: systemctl --user disable --now phantom-network)" \
    || echo "  (systemd user session unavailable — the launcher still works)"
fi

echo
echo "Installed."
echo "  Firewall (only if peers cannot connect — never applied automatically):"
echo "    sudo ufw allow 8415/tcp && sudo ufw allow 8414/udp"
echo "  Launch: 'Phantom Network' app menu entry, or $BIN_DIR/phantom-network"
echo "  Uninstall: sudo bash $DEST/packaging/uninstall-ubuntu.sh [--purge]"
