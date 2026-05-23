# PyInstaller spec for the agent. Run via: pyinstaller agent.spec
# Produces dist/dld-agent (or dld-agent.exe on Windows).
#
# `target_arch` is read from the env var `DLD_AGENT_TARGET_ARCH` so the CI
# build script can switch between native (None) and universal2 without
# editing this file per release. macOS-only — PyInstaller ignores it on
# Windows + Linux.
import os
_TARGET_ARCH = os.environ.get("DLD_AGENT_TARGET_ARCH") or None

block_cipher = None

a = Analysis(
    ['agent/main.py'],
    pathex=['.'],
    binaries=[],
    datas=[('agent/release_pubkey.pem', 'agent')],
    hiddenimports=['core.file_scanner', 'keyring.backends.Windows', 'keyring.backends.macOS'],
    hookspath=[],
    runtime_hooks=[],
    excludes=['playwright', 'flask', 'flask_sock', 'openpyxl'],  # server-side; agent doesn't need them
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
    name='dld-agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,  # phase 2b: keeps the pairing prompt visible
    disable_windowed_traceback=False,
    target_arch=_TARGET_ARCH,
    codesign_identity=None,
    entitlements_file=None,
)
