#!/usr/bin/env bash
# PHANTOM NETWORK v1.2.1 — Debian/Ubuntu .deb builder
# Run on a Debian-based host:  bash packaging/make-deb.sh   →  dist/*.deb
# The package installs to /opt/phantom-network, ships the desktop launcher
# for the CURRENT user at post-install (see maintainer script), and never
# touches firewalls or user directories beyond its own.
set -e
cd "$(dirname "$0")/.."
VERSION=1.2.1
OUT=dist; PKG="phantom-network_${VERSION}_all"
STAGE="$OUT/$PKG"
mkdir -p "$STAGE/DEBIAN" "$STAGE/opt/phantom-network" \
         "$STAGE/usr/share/applications" "$STAGE/usr/share/icons/hicolor/scalable/apps" \
         "$STAGE/usr/bin"
cp -r companion web sounds "$STAGE/opt/phantom-network/"
for f in README.md LICENSE requirements.txt; do cp "$f" "$STAGE/opt/phantom-network/"; done
cp web/assets/logo.svg "$STAGE/usr/share/icons/hicolor/scalable/apps/phantom-network.svg"
cat > "$STAGE/DEBIAN/control" <<CTRL
Package: phantom-network
Version: $VERSION
Section: net
Priority: optional
Architecture: all
Depends: python3 (>= 3.9), python3-venv | python3-distutils, curl | wget
Recommends: pulseaudio-utils | alsa-utils
Suggests: psutil
Maintainer: Phantom Network contributors <phantom@localhost>
Description: Cross-platform LAN command center, File Commander and Signal Lab
 Companion service + browser command center for private networks.
 No cloud, no accounts. Pure Python standard library at runtime;
 optional psutil telemetry is installed into an app-owned venv.
CTRL
cat > "$STAGE/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
PY=/opt/phantom-network/venv/bin/python3
if command -v python3 >/dev/null; then
  python3 -m venv /opt/phantom-network/venv >/dev/null 2>&1 || PY=/usr/bin/python3
  [ "$PY" = "/opt/phantom-network/venv/bin/python3" ] && /opt/phantom-network/venv/bin/pip install --quiet psutil >/dev/null 2>&1 || true
fi
cat > /usr/bin/phantom-network <<'RUN'
#!/bin/sh
# Runtime data follows the user who launches the command, never /opt
# and never root's home. The program directory stays read-only.
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/phantom-network"
DATA_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/phantom-network"
mkdir -p "$STATE_DIR" "$DATA_DIR"
PY=/usr/bin/python3
if [ -x /opt/phantom-network/venv/bin/python3 ]; then
  PY=/opt/phantom-network/venv/bin/python3
fi
if ! curl -sf http://127.0.0.1:8415/api/health >/dev/null 2>&1; then
  (cd /opt/phantom-network && nohup "$PY" -m companion --data-dir "$DATA_DIR" >>"$STATE_DIR/companion.out" 2>&1 &)
  sleep 2
fi
exec xdg-open http://localhost:8415
RUN
chmod 755 /usr/bin/phantom-network
echo "Phantom Network installed. Launch with: phantom-network  (menu: Phantom Network)"
exit 0
POST
cat > "$STAGE/DEBIAN/prerm" <<'PRE'
#!/bin/sh
set -e
pkill -f "python3 -m companion" 2>/dev/null || true
exit 0
PRE
chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm"
mkdir -p "$OUT"
dpkg-deb --build --root-owner-group "$STAGE" "$OUT/${PKG}.deb"
echo "wrote $OUT/${PKG}.deb"
rm -rf "$STAGE"
