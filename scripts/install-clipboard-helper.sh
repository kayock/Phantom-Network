#!/usr/bin/env bash
# Install a user-local clipboard helper for Phantom Network auto-sync.
# Prefers wl-clipboard on Wayland sessions; falls back to xclip on X11.
# No root required: downloads the .deb and extracts into ~/.local/opt.
set -euo pipefail
PREFIX="${HOME}/.local/opt/pn-helpers"
BIN="${HOME}/.local/bin"
mkdir -p "$PREFIX" "$BIN"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

SESSION_TYPE="${XDG_SESSION_TYPE:-}"
if [ -z "$SESSION_TYPE" ] && command -v systemctl >/dev/null 2>&1; then
  SESSION_TYPE=$(systemctl --user show-environment 2>/dev/null \
    | sed -n 's/^XDG_SESSION_TYPE=//p' | head -1 || true)
fi
WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-}"
if [ -z "$WAYLAND_DISPLAY" ] && command -v systemctl >/dev/null 2>&1; then
  WAYLAND_DISPLAY=$(systemctl --user show-environment 2>/dev/null \
    | sed -n 's/^WAYLAND_DISPLAY=//p' | head -1 || true)
fi

want_wayland=0
if [ "$SESSION_TYPE" = "wayland" ] || [ -n "$WAYLAND_DISPLAY" ]; then
  want_wayland=1
fi

pkg="wl-clipboard"
if [ "$want_wayland" != "1" ]; then
  pkg="xclip"
fi

cd "$TMP"
echo "Downloading $pkg (user-local, no sudo)…"
apt-get download "$pkg"
DEB=$(ls ${pkg}_*.deb | head -1)
DEST="$PREFIX/$pkg"
rm -rf "$DEST"
mkdir -p "$DEST"
dpkg-deb -x "$DEB" "$DEST"
# Symlink binaries
find "$DEST/usr/bin" -type f -executable 2>/dev/null | while read -r f; do
  ln -sfn "$f" "$BIN/$(basename "$f")"
  echo "  linked $BIN/$(basename "$f")"
done
export PATH="$BIN:$PATH"
if [ "$pkg" = "wl-clipboard" ]; then
  command -v wl-paste >/dev/null && command -v wl-copy >/dev/null
  echo "Installed wl-clipboard → $DEST"
  echo "Test: echo hello | wl-copy && wl-paste"
else
  command -v xclip >/dev/null
  echo "Installed xclip → $DEST"
  echo "Test: echo hello | xclip -selection clipboard && xclip -selection clipboard -o"
fi
