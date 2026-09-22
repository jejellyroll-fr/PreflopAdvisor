# -*- mode: python ; coding: utf-8 -*-

import os
import sys

block_cipher = None

# Base directory
base_dir = os.path.abspath(SPECPATH)


def project_version():
    """The version to stamp the bundle with, taken from pyproject.toml.

    Read rather than hard-coded: a bundle reporting 0.0.0 while the package reports 0.2 is
    a bug report nobody can place. Falls back only when the file cannot be parsed, which
    on Python 3.10 means tomllib is absent.
    """
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            from importlib.metadata import version

            return version("preflop_advisor")
        except Exception:
            return "0.0.0"
    try:
        with open(os.path.join(base_dir, "pyproject.toml"), "rb") as handle:
            return tomllib.load(handle)["project"]["version"]
    except (OSError, KeyError, ValueError):
        return "0.0.0"


VERSION = project_version()

# Data files to bundle
datas = [
    (os.path.join(base_dir, 'preflop_advisor', 'config.ini'), 'preflop_advisor'),
    (os.path.join(base_dir, 'preflop_advisor', 'popup-pics'), os.path.join('preflop_advisor', 'popup-pics')),
]

# The demonstration tree, and only it. `ranges/` is whatever the build machine happens to
# hold -- the generated fake-*max trees alone are ~95MB and are ignored by git -- so
# bundling the folder wholesale makes the binary neither reproducible nor a sane download.
# Users export their own trees from Monker and point [TreeInfos] at them; without any the
# application says so in its status bar rather than failing.
DEMO_TREE = os.path.join('ranges', 'HU-100bb-with-limp')
demo_tree_dir = os.path.join(base_dir, DEMO_TREE)
if os.path.isdir(demo_tree_dir):
    datas.append((demo_tree_dir, DEMO_TREE))

hiddenimports = [
    'PySide6',
    'PySide6.QtCore',
    'PySide6.QtGui',
    'PySide6.QtWidgets',
    'configparser',
    'preflop_advisor',
    'preflop_advisor.gui',
    'preflop_advisor.card_selector',
    'preflop_advisor.position_selector',
    'preflop_advisor.tree_selector',
    'preflop_advisor.outputframe',
    'preflop_advisor.randomizer',
    'preflop_advisor.tooltip',
    'preflop_advisor.tree_reader',
    'preflop_advisor.tree_reader_helpers',
    'preflop_advisor.hand_convert_helper',
    'preflop_advisor.sqlite_store',
    'preflop_advisor.errors',
]

# The application imports QtCore, QtGui and QtWidgets and nothing else. The rest of the
# PySide6 bindings are excluded outright.
excludes = [
    'tkinter',
    'matplotlib',
    'scipy',
    'numpy',
    'PySide6.QtNetwork',
    'PySide6.QtQml',
    'PySide6.QtQuick',
    'PySide6.QtQuickWidgets',
    'PySide6.QtPdf',
    'PySide6.QtPdfWidgets',
    'PySide6.QtOpenGL',
    'PySide6.QtOpenGLWidgets',
    'PySide6.QtVirtualKeyboard',
    'PySide6.QtMultimedia',
    'PySide6.QtWebEngineCore',
    'PySide6.Qt3DCore',
]

# Qt libraries pulled in by *plugins* rather than by an import, so excluding the binding
# above does not drop them: the virtual-keyboard input context drags in Qml and Quick, and
# the pdf image format drags in QtPdf. Roughly 20MB of frameworks for features this
# application has no way to reach. Matched on the path, and only on these names.
UNUSED_QT = (
    'QtQuick',
    'QtQml',
    'QtVirtualKeyboard',
    'QtPdf',
    'Qt3D',
    'QtWebEngine',
    'QtMultimedia',
)
UNUSED_PLUGINS = (
    os.path.join('plugins', 'platforminputcontexts'),
    os.path.join('plugins', 'qmltooling'),
)


def is_unused(destination):
    """Whether a collected file belongs to a Qt feature the application cannot reach."""
    if any(part in destination for part in UNUSED_PLUGINS):
        return True
    name = os.path.basename(destination)
    # Framework directories carry the module name in a parent component on macOS, so the
    # whole destination is tested there, and only the file name elsewhere.
    haystack = destination if '.framework' in destination else name
    return any(module in haystack for module in UNUSED_QT)


a = Analysis(
    ['standalone.py'],
    pathex=[base_dir],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

a.binaries = [entry for entry in a.binaries if not is_unused(entry[0])]
a.datas = [entry for entry in a.datas if not is_unused(entry[0])]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# UPX is off on macOS: it rewrites the Mach-O headers of the Qt dylibs, which invalidates
# the ad-hoc signature PyInstaller applies on arm64 and produces a bundle the loader
# refuses. Elsewhere it is a straight saving.
use_upx = sys.platform != 'darwin'

# An icon if the repository ships one; PyInstaller's own placeholder otherwise.
icon_name = 'icon.icns' if sys.platform == 'darwin' else ('icon.ico' if os.name == 'nt' else None)
icon_path = os.path.join(base_dir, 'resources', icon_name) if icon_name else None
if icon_path and not os.path.isfile(icon_path):
    icon_path = None

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PreflopAdvisor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=use_upx,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=use_upx,
    upx_exclude=[],
    name='PreflopAdvisor',
)

# macOS Application Bundle (.app)
if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='PreflopAdvisor.app',
        icon=icon_path,
        bundle_identifier='com.preflopadvisor.app',
        version=VERSION,
        info_plist={
            'CFBundleShortVersionString': VERSION,
            'CFBundleVersion': VERSION,
            # Qt 6.8 itself requires Big Sur; saying so gives a clear refusal instead of a
            # dynamic-linker error on an older system.
            'LSMinimumSystemVersion': '11.0',
            'NSHighResolutionCapable': True,
        },
    )
