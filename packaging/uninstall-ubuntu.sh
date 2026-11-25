#!/usr/bin/env bash
# PHANTOM NETWORK uninstaller — removes ONLY Phantom Network's own files.
# --purge additionally deletes the app's config/state (~/.config/phantom-network).
# Shared directories and their CONTENTS are never touched.
set -e
echo "Stopping autostart (if any)…"
systemctl --user disable --now phantom-network 2>/dev/null || true
U="${SUDO_USER:-$USER}"
UHOME=$(getent passwd "$U" | cut -d: -f6)
pkill -f "python3 -m companion" 2>/dev/null || true
rm -rf /opt/phantom-network
rm -f "$UHOME/.local/share/applications/phantom-network.desktop"
rm -f "$UHOME/.local/bin/phantom-network"
rm -f "$UHOME/.config/systemd/user/phantom-network.service"
if [ "$1" = "--purge" ]; then
  rm -rf "$UHOME/.config/phantom-network" \
         "$UHOME/.local/state/phantom-network"
  echo "Application data + logs purged."
else
  echo "Application data kept at $UHOME/.config/phantom-network"
  echo "Logs kept at $UHOME/.local/state/phantom-network (use --purge to remove both)."
fi
echo "Phantom Network removed. Shared directories and transferred files were NOT touched."
