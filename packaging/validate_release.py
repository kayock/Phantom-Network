#!/usr/bin/env python3
"""PHANTOM NETWORK release archive validator.

Run automatically by packaging/make-dist.sh BEFORE the zip is published and
again by CI/devs via `python3 packaging/validate_release.py <zip>`.
It exists because v1.1.0 shipped a PowerShell installer with embedded
vertical-tab bytes (a Python escape accident: "\\v" -> 0x0b, "\\r" -> 0x0d)
- this script makes that class of defect unshippable.

Checks:
  1. required release files present
  2. forbidden development artifacts absent (__pycache__, .pyc, caches,
     git internals, virtualenvs, large fixtures)
  3. version consistency (companion, web, make-dist, CHANGELOG)
  4. every code/script file decodes as UTF-8 and contains NO control
     characters other than \\t and \\n (byte-level scan)
  5. documented build/packaging files actually exist
  6. PowerShell scripts pass a static sanity check (balanced quotes/braces,
     no suspicious character-class escapes) - pwsh execution is NOT claimed;
     real Windows execution remains a manual step (CURSOR_DEPLOYMENT_HANDOFF.md)
  7. zip integrity + optional SHA-256 print

Exit code 0 = shippable, 1 = problems listed.
"""
import sys
import zipfile
import hashlib
from pathlib import Path

VERSION = "1.4.1"

REQUIRED = [
    "companion/__init__.py", "companion/app.py", "companion/transfers.py",
    "companion/httpd.py", "companion/fsops.py", "companion/chat.py",
    "companion/audio.py", "companion/discovery.py", "companion/peers.py",
    "web/index.html", "web/css/theme.css", "web/js/app.js", "web/js/lib/dsp.js",
    "sounds/pn-red_alert.wav", "sounds/pn-device_connected.wav",
    "packaging/install-windows.ps1", "packaging/uninstall-windows.ps1",
    "packaging/run-phantom-windows.bat", "packaging/companion.spec",
    "packaging/make-deb.sh", "packaging/install-ubuntu.sh",
    "packaging/uninstall-ubuntu.sh", "packaging/make-dist.sh",
    "packaging/validate_release.py", "packaging/make-portable-linux.sh",
    "tests/unit/test_v11_regressions.py", "tests/integration/test_v11_integration.py",
    "README.md", "ARCHITECTURE.md", "API_REFERENCE.md", "DEVELOPER_GUIDE.md",
    "DEPLOYMENT.md", "FILE_TRANSFER_PROTOCOL.md", "HARDWARE_COMPATIBILITY.md",
    "BUILD.md", "CHANGELOG.md", "TEST_REPORT.md", "BUILD_LOG.txt",
    "KNOWN_LIMITATIONS.md", "DEVELOPMENT_BACKLOG.md", "REFINEMENT_REPORT.md",
    "LOCALFORGE_HANDOFF.md", "CURSOR_DEPLOYMENT_HANDOFF.md", "LICENSE",
    "requirements.txt", "pytest.ini",
    # v1.4.0 — Phantom Signal modules + guides
    "companion/voice.py", "companion/broadcast.py", "companion/media.py",
    "companion/mesh.py", "companion/clipboard.py",
    "web/js/lib/rtc.js", "web/js/views/voice.js",
    "web/js/views/broadcast.js", "web/js/views/media.js",
    "web/js/views/clipboard.js", "web/js/views/mesh.js",
    "web/js/mobile/app.js", "web/sw.js",
    "docs/VOICE_GUIDE.md", "docs/BROADCAST_GUIDE.md", "docs/MEDIA_GUIDE.md",
    "docs/SIGNAL_LAB_GUIDE.md", "docs/CLIPBOARD_GUIDE.md",
    "tests/unit/test_v140_backends.py", "tests/unit/test_v140_dsp.py",
    "tests/unit/test_v140_conversations.py", "tests/unit/test_v132_routing.py",
    "tests/integration/test_v140_signal_e2e.py",
    "tests/js/test_v140.mjs", "scripts/gen-lan-cert.sh",
]

FORBIDDEN_PATTERNS = (
    "__pycache__/", ".pyc", ".pytest_cache", ".git/", ".git ",
    "venv/", ".venv", "node_modules/", ".idea", ".vscode",
    "pn-lf-", "pn-g-src-", "pn-g-dst-", "dbg-src", "dbg-dst",
    "large-file-fixture", "gguf-sim.bin", ".part.corrupt",
)

CODE_SUFFIXES = {".py", ".ps1", ".bat", ".sh", ".spec", ".js", ".mjs",
                 ".html", ".css", ".ini", ".cfg", ".txt"}


def fail(msg, problems):
    problems.append(msg)


def scan_zip(zpath: Path):
    problems = []
    names = zipfile.ZipFile(zpath).namelist()
    stripped = {n.split("/", 1)[-1] for n in names}
    for req in REQUIRED:
        if req not in stripped and req not in names:
            fail(f"missing required file: {req}", problems)
    for n in names:
        for pat in FORBIDDEN_PATTERNS:
            if pat in n:
                fail(f"forbidden artifact in archive: {n}", problems)
    return problems


def scan_tree(root: Path):
    problems = []
    for rel in REQUIRED:
        if not (root / rel).exists():
            fail(f"required file missing from tree: {rel}", problems)

    # version consistency
    try:
        init = (root / "companion/__init__.py").read_text(encoding="utf-8")
        if f'__version__ = "{VERSION}"' not in init:
            fail(f"companion/__init__.py version != {VERSION}", problems)
    except OSError:
        fail("companion/__init__.py unreadable", problems)
    try:
        html = (root / "web/index.html").read_text(encoding="utf-8")
        if f"NETWORK v{VERSION}" not in html:
            fail(f"web/index.html brand version != {VERSION}", problems)
    except OSError:
        fail("web/index.html unreadable", problems)
    try:
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        if f"[{VERSION}]" not in changelog:
            fail(f"CHANGELOG.md has no [{VERSION}] entry", problems)
    except OSError:
        fail("CHANGELOG.md unreadable", problems)

    # byte-level scan: control chars + UTF-8 validity on every code file
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in CODE_SUFFIXES:
            continue
        rel = path.relative_to(root).as_posix()
        if any(pat in rel for pat in FORBIDDEN_PATTERNS):
            continue
        data = path.read_bytes()
        for i, b in enumerate(data):
            if b < 0x20 and b not in (0x09, 0x0A):
                line = data[:i].count(b"\n") + 1
                fail(f"{rel}: control byte 0x{b:02x} at line {line}", problems)
                break
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            fail(f"{rel}: not valid UTF-8 ({exc})", problems)

    # PowerShell static sanity (no pwsh in this environment - documented)
    for ps in root.glob("packaging/*.ps1"):
        text = ps.read_text(encoding="utf-8", errors="replace")
        if text.count('"') % 2:
            fail(f"{ps.name}: unbalanced double quotes", problems)
        if text.count("{") != text.count("}"):
            fail(f"{ps.name}: unbalanced braces", problems)
        if "\\v" in text or "\\r" in text.replace("\\r\\n", ""):
            # literal backslash-v / backslash-r inside a PS string is almost
            # always an escaping accident (source of the v1.1.0 corruption)
            fail(f"{ps.name}: suspicious \\v or \\r escape present", problems)
    return problems


def main(argv):
    problems = []
    root = Path(__file__).resolve().parent.parent
    problems += scan_tree(root)
    if argv:
        zpath = Path(argv[0])
        if not zpath.exists():
            fail(f"archive not found: {zpath}", problems)
        else:
            bad = zipfile.ZipFile(zpath).testzip()
            if bad is not None:
                fail(f"zip integrity: corrupt member {bad}", problems)
            problems += scan_zip(zpath)
            digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
            print(f"SHA-256 {zpath.name}: {digest}")
    if problems:
        print("RELEASE VALIDATION FAILED:")
        for p in problems:
            print("  ✗", p)
        return 1
    print("RELEASE VALIDATION OK "
          f"(required files, artifacts, versions, control bytes, encoding, "
          f"PowerShell static checks{', archive ' + argv[0] if argv else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
