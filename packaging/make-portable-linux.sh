#!/usr/bin/env bash
# Build the Linux portable distribution (source tree + portable marker).
set -e
cd "$(dirname "$0")/.."
OUT="${1:-dist}"
mkdir -p "$OUT"
STAGE=$(mktemp -d)
cp -r companion web sounds packaging scripts tests "$STAGE/"
cp README.md ARCHITECTURE.md API_REFERENCE.md DEVELOPER_GUIDE.md DEPLOYMENT.md \
   FILE_TRANSFER_PROTOCOL.md HARDWARE_COMPATIBILITY.md BUILD.md CHANGELOG.md \
   TEST_REPORT.md KNOWN_LIMITATIONS.md DEVELOPMENT_BACKLOG.md LICENSE \
   requirements.txt "$STAGE/" 2>/dev/null || true
touch "$STAGE/portable.marker"
cat > "$STAGE/phantom-network.sh" <<'RUN'
#!/usr/bin/env bash
cd "$(dirname "$0")"
python3 -m companion "$@"
RUN
chmod +x "$STAGE/phantom-network.sh"
TARBALL="$OUT/phantom-network-portable-linux.tar.gz"
tar -czf "$TARBALL" -C "$STAGE" .
echo "wrote $TARBALL"
rm -rf "$STAGE"
