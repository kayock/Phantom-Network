# -*- mode: python ; coding: utf-8 -*-
# PHANTOM NETWORK v1.1.0 — PyInstaller spec for the Windows companion .exe
#
# Build ON a Windows machine (Linux cannot cross-compile — documented):
#   pip install pyinstaller
#   pyinstaller --noconfirm --clean packaging/companion.spec
# Output: dist/PhantomCompanion.exe  (single file companion; the web UI is
# bundled automatically — no separate web/ copy needed at runtime).
#
# NOTE: PyInstaller may trigger antivirus heuristics on some systems; the
# portable source distribution (run-phantom-windows.bat) needs no build.

import os

block_cipher = None
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(SPEC)))

a = Analysis(
    [os.path.join(ROOT, "companion", "__main__.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[(os.path.join(ROOT, "web"), "web"),        # the command center UI
           (os.path.join(ROOT, "sounds"), "sounds")], # original presets
    hiddenimports=["psutil"],                          # optional telemetry
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "scipy"],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
    name="PhantomCompanion",
    console=True,                 # keep a console: operators see the log
    disable_windowed_traceback=False,
    icon=os.path.join(ROOT, "assets", "branding", "phantom.ico") if os.path.exists(
        os.path.join(ROOT, "assets", "branding", "phantom.ico")) else None,
)
