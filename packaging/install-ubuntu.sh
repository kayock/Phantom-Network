#!/usr/bin/env bash
# PHANTOM NETWORK v1.2.2 — Ubuntu installer
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

# The desktop user is whoever invoked sudo/pkexec, not root. $HOME and
# XDG_* in a sudo session usually belong to root and must not decide
# where configuration or logs are created.
resolve_desktop_user() {
  if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    printf '%s' "$SUDO_USER"
    return
  fi
  if [ -n "${PKEXEC_UID:-}" ]; then
    getent passwd "$PKEXEC_UID" | cut -d: -f1
    return
  fi
  local ln
  ln=$(logname 2>/dev/null || true)
  if [ -n "$ln" ] && [ "$ln" != "root" ]; then
    printf '%s' "$ln"
    return
  fi
  printf '%s' "${USER:-}"
}

# Accept an XDG directory only when it belongs to this user. A value
# inherited from the root installer (for example /root/.config) is ignored
# and the XDG default under the user's home is used instead.
xdg_base() {
  local given="$1" home="$2" fallback="$3"
  if [ -n "$given" ] && [ "$given" = "$home" -o "${given#"$home"/}" != "$given" ]; then
    printf '%s' "$given"
  else
    printf '%s' "$home/$fallback"
  fi
}

U="$(resolve_desktop_user)"
[ -n "$U" ] && [ "$U" != "root" ] || { echo "Could not resolve the desktop user (refusing to use root's home)."; exit 1; }
UHOME=$(getent passwd "$U" | cut -d: -f6)
[ -n "$UHOME" ] && [ -d "$UHOME" ] || { echo "Could not resolve a home directory for $U."; exit 1; }

SESS_CONFIG=""
SESS_STATE=""
if command -v runuser >/dev/null 2>&1; then
  SESS_CONFIG=$(runuser -u "$U" -- systemctl --user show-environment 2>/dev/null | sed -n 's/^XDG_CONFIG_HOME=//p' | head -1 || true)
  SESS_STATE=$(runuser -u "$U" -- systemctl --user show-environment 2>/dev/null | sed -n 's/^XDG_STATE_HOME=//p' | head -1 || true)
fi
CONFIG_BASE=$(xdg_base "${SESS_CONFIG:-${XDG_CONFIG_HOME:-}}" "$UHOME" ".config")
STATE_BASE=$(xdg_base "${SESS_STATE:-${XDG_STATE_HOME:-}}" "$UHOME" ".local/state")
STATE_DIR="$STATE_BASE/phantom-network"
DATA_DIR="$CONFIG_BASE/phantom-network"

if [ "${PN_RESOLVE_ONLY:-0}" = "1" ]; then
  printf 'user=%s\nhome=%s\nstate=%s\ndata=%s\n' "$U" "$UHOME" "$STATE_DIR" "$DATA_DIR"
  exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
  if [ "${PN_USER_INSTALL:-0}" != "1" ]; then
    echo "Run with sudo, or set PN_USER_INSTALL=1 for a user-owned install."
    exit 1
  fi
  # No passwordless sudo on this host. A user-owned program directory
  # keeps program files separate from runtime data without touching /opt.
  if [ -z "${PN_DEST:-}" ]; then
    DEST="$HOME/.local/opt/phantom-network"
  fi
fi

echo "Installing Phantom Network v1.4.0 from $SRC → $DEST"
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
# Paths expand when the desktop user launches this script. They are never
# captured from the root account that ran the installer.
STATE_DIR="\${XDG_STATE_HOME:-\$HOME/.local/state}/phantom-network"
DATA_DIR="\${XDG_CONFIG_HOME:-\$HOME/.config}/phantom-network"
PROG_DIR="$DEST"
PY_BIN="$PY_BIN"
mkdir -p "\$STATE_DIR" "\$DATA_DIR"
if ! curl -sf http://127.0.0.1:8415/api/health >/dev/null 2>&1; then
  (cd "\$PROG_DIR" && nohup "\$PY_BIN" -m companion --data-dir "\$DATA_DIR" \\
      >>"\$STATE_DIR/companion.out" 2>&1 &)
  sleep 2
fi
xdg-open http://localhost:8415
RUN
chmod 755 "$BIN_DIR/phantom-network"
# fix ownership ONLY of files we just created inside our own dirs
chown "$U":"$U" "$APPS_DIR/phantom-network.desktop" "$BIN_DIR/phantom-network" 2>/dev/null || true

# ---- optional autostart (defect E fix: correct unit construction) ----
if [ "${PN_NO_AUTOSTART:-0}" = "1" ]; then
  ANS=N
else
  read -r -p "Enable auto-start at login (systemd --user)? [y/N] " ANS || ANS=N
fi
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
