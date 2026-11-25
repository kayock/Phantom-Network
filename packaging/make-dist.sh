#!/usr/bin/env bash
# Build + verify the release ZIP: Phantom-Network-v1.2.0.zip
set -e
cd "$(dirname "$0")/.."
VERSION=1.2.0
OUT=dist; NAME="Phantom-Network-v$VERSION"
mkdir -p "$OUT"

# 1. source audit: every version indicator must report the release version
grep -q '__version__ = "1.2.0"' companion/__init__.py
grep -q "NETWORK v1.2.0" web/index.html
echo "[audit] version strings OK"

# 2. stage the release tree (no git internals, no dev logs, no archives)
STAGE=$(mktemp -d); mkdir -p "$STAGE/$NAME"
for item in companion web sounds assets screenshots packaging scripts tests \
            README.md ARCHITECTURE.md API_REFERENCE.md DEVELOPER_GUIDE.md \
            DEPLOYMENT.md FILE_TRANSFER_PROTOCOL.md HARDWARE_COMPATIBILITY.md \
            BUILD.md CHANGELOG.md TEST_REPORT.md KNOWN_LIMITATIONS.md \
            DEVELOPMENT_BACKLOG.md REFINEMENT_REPORT.md LOCALFORGE_HANDOFF.md \
            CURSOR_DEPLOYMENT_HANDOFF.md \
            BUILD_LOG.txt LICENSE requirements.txt pytest.ini; do
  cp -r "$item" "$STAGE/$NAME/" 2>/dev/null || true
done

# 3. v1.2.0 hygiene: strip bytecode caches / dev artifacts from the tree
find "$STAGE" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -type d -name ".pytest_cache" -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -type f \( -name "*.pyc" -o -name "*.pyo" -o -name ".coverage" \) -delete 2>/dev/null || true

# 4. validate the TREE before zipping (required files, forbidden artifacts,
#    version consistency, control characters, UTF-8, PowerShell statics)
python3 "$STAGE/$NAME/packaging/validate_release.py" || { echo "[validate] FAILED"; exit 1; }
echo "[validate] release tree OK"

# 4b. include the compiled Debian package when one was built
if [ -f "$OUT/phantom-network_${VERSION}_all.deb" ]; then
  mkdir -p "$STAGE/$NAME/dist"
  cp "$OUT/phantom-network_${VERSION}_all.deb" "$STAGE/$NAME/dist/"
  echo "[include] Debian package"
fi

# 5. build the archive
( cd "$STAGE" && zip -qr "$OLDPWD/$OUT/$NAME.zip" "$NAME" )
echo "[build] $OUT/$NAME.zip"

# 6. integrity + clean-extract verification
unzip -t "$OUT/$NAME.zip" >/dev/null && echo "[verify] zip integrity OK"
rm -rf /tmp/pn-verify && mkdir -p /tmp/pn-verify
unzip -q "$OUT/$NAME.zip" -d /tmp/pn-verify
python3 -m py_compile /tmp/pn-verify/$NAME/companion/*.py && echo "[verify] shipped source compiles"
node /tmp/pn-verify/$NAME/tests/js/test_dsp.mjs >/dev/null && echo "[verify] shipped DSP suite passes"

# 7. validate the ARCHIVE itself (entry scan + SHA-256)
python3 packaging/validate_release.py "$OUT/$NAME.zip"
echo "[done] $OUT/$NAME.zip"
rm -rf "$STAGE"
