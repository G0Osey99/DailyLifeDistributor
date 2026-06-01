# PyInstaller spec for the agent. Run via: pyinstaller agent.spec
# Produces dist/dld-agent (or dld-agent.exe on Windows). On macOS we ALSO
# emit dist/dld-agent.app — a proper .app bundle so Finder treats the
# download as an application instead of a generic "Unix executable"
# document. scripts/build_agent.py then zips the .app for distribution
# (zip preserves the executable bit, which browsers strip on a raw
# binary download).
#
# `target_arch` is read from the env var `DLD_AGENT_TARGET_ARCH` so the CI
# build script can switch between native (None) and universal2 without
# editing this file per release. macOS-only — PyInstaller ignores it on
# Windows + Linux.
import os
import sys
_TARGET_ARCH = os.environ.get("DLD_AGENT_TARGET_ARCH") or None
_IS_MACOS = sys.platform == "darwin"

block_cipher = None

# customtkinter ships JSON themes + PNG assets that PyInstaller needs to
# carry along. The community hook (auto-loaded from pip-installed
# pyinstaller-hooks-contrib) collects them; collect_data_files makes the
# bundling explicit so a fresh dev env without that hook still ships a
# functional GUI binary.
from PyInstaller.utils.hooks import collect_data_files
_ctk_data = collect_data_files('customtkinter')
# Upload engines (Phase 3 — the agent runs the bundled uploaders locally):
#   * Playwright ships a node driver under playwright/driver/ that
#     sync_playwright() spawns. It MUST be collected or every Playwright
#     upload dies with "Playwright is not installed". (pyinstaller-hooks-
#     contrib also auto-collects it; we add it explicitly so a build env
#     without that hook still produces a working binary.)
#   * google-api-python-client ships discovery cache JSON used by build().
_playwright_data = collect_data_files('playwright')
_googleapi_data = collect_data_files('googleapiclient')
# certifi ships cacert.pem next to its __init__.py. PyInstaller's auto-
# discovery picks it up via the `requests` import chain on most builds,
# but agent/transport.py imports certifi DIRECTLY now (to build the SSL
# context for simple-websocket — `requests` defaults to certifi, but
# simple-websocket uses ssl.create_default_context() which on a
# PyInstaller .app bundle returns a context with no trust anchors). Add
# the data file explicitly so the bundle stays correct even if the
# requests/certifi version pair drops the auto-hook in the future.
_certifi_data = collect_data_files('certifi')

a = Analysis(
    ['agent/main.py'],
    pathex=['.'],
    binaries=[],
    datas=([('agent/release_pubkey.pem', 'agent')] + _ctk_data + _certifi_data
           + _playwright_data + _googleapi_data),
    hiddenimports=[
        'core.file_scanner',
        # The agent dispatch path imports these FUNCTION-LEVEL (run_batch
        # _make_elements / _entry_obj build ReviewEntry/UploadElements, and
        # _dispatch_upload imports the uploaders), which PyInstaller's static
        # analysis misses — so without listing them the bundle dropped the
        # chain and the agent crashed on first dispatch.
        'core.session_state',
        'core.config',
        'core.circuit_breaker',
        'core.org_context',
        'core.playwright_session',
        'core.hosted',
        'core.image_gatherer',
        # The bundled uploaders (run on the agent's own machine, Phase 3).
        'uploaders.youtube_uploader',
        'uploaders.simplecast_uploader',
        'uploaders.vista_social_uploader',
        'uploaders.rock.orchestrator',
        'uploaders.rock.email',
        'uploaders.rock.client',
        # Upload engines — function/try-guarded imports the analysis misses.
        'playwright.sync_api',
        'googleapiclient.discovery',
        'googleapiclient.http',
        'googleapiclient.errors',
        'google_auth_oauthlib.flow',
        'google.oauth2.credentials',
        'httplib2',
        'keyring.backends.Windows',
        'keyring.backends.macOS',
        # GUI deps — explicit so an analysis-time miss doesn't surface
        # as a runtime ImportError in the bundled binary.
        'customtkinter',
        'tkinter',
        'tkinter.font',
        'tkinter.ttk',
        # certifi is imported directly from agent/transport.py for the
        # wss SSL context (see comment by collect_data_files above).
        'certifi',
    ],
    hookspath=[],
    runtime_hooks=[],
    # flask/flask_sock are server-only (org_context is now Flask-optional so
    # the agent imports it without them). openpyxl is server-only too — the
    # agent builds ReviewEntry from the job envelope, never parses a sheet.
    # playwright was previously excluded here, which is exactly why agent
    # uploads were impossible; it is now bundled (see _playwright_data).
    excludes=['flask', 'flask_sock', 'openpyxl'],
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
    # v0.6.6: hide the console window. The GUI is the primary surface
    # (pairing prompt is a Tk modal now, not a stdin input), and the
    # background console was confusing users who launched the .exe.
    # CLI mode (--no-gui) still works for scripted use; stdout in that
    # mode goes nowhere visible but agent.log + boot.log capture
    # everything. faulthandler.enable() is still installed at the top
    # of agent/main.py so C-level crashes still leave a trace.
    console=False,
    disable_windowed_traceback=False,
    target_arch=_TARGET_ARCH,
    codesign_identity=None,
    entitlements_file=None,
)

# macOS: wrap the EXE in an .app bundle. Without this, the downloaded
# binary is a bare Mach-O that Finder shows as a generic Unix executable
# document — browsers also strip the executable bit, so the user has to
# `chmod +x` from a terminal. A .app:
#   * has a real application icon in Finder
#   * is double-clickable
#   * survives zip round-tripping with the executable bit intact
#
# We're not Apple-notarized, so first launch still requires a single
# right-click → Open to bypass Gatekeeper. After that the OS remembers
# the user's approval and double-click works forever.
if _IS_MACOS:
    app = BUNDLE(
        exe,
        name='dld-agent.app',
        # Reverse-DNS bundle id under autoalert.pro so future signing /
        # notarization can register against an Apple Developer team
        # without renaming. NOT under com.* because we don't own that
        # tree; pro.autoalert.* is what the website is published under.
        bundle_identifier='pro.autoalert.dld-agent',
        info_plist={
            # LSUIElement=1 would hide the dock icon (background daemon
            # style). We DO want the dock icon — the GUI is a primary
            # surface — so leave it 0/unset.
            'CFBundleName': 'DLD Agent',
            'CFBundleDisplayName': 'DLD Agent',
            # NSHighResolutionCapable so Tk widgets aren't pixel-doubled
            # on Retina. PyInstaller defaults this to True for .app
            # bundles but be explicit so a PyInstaller default flip
            # doesn't surprise users.
            'NSHighResolutionCapable': True,
        },
    )
