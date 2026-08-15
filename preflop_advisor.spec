# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# Base directory
base_dir = os.path.abspath(SPECPATH)

# Data files to bundle
datas = [
    (os.path.join(base_dir, 'preflop_advisor', 'config.ini'), 'preflop_advisor'),
    (os.path.join(base_dir, 'preflop_advisor', 'popup-pics'), os.path.join('preflop_advisor', 'popup-pics')),
]

# Include ranges folder if it exists
ranges_dir = os.path.join(base_dir, 'ranges')
if os.path.isdir(ranges_dir):
    datas.append((ranges_dir, 'ranges'))

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

a = Analysis(
    ['standalone.py'],
    pathex=[base_dir],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'scipy', 'numpy'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PreflopAdvisor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PreflopAdvisor',
)

# macOS Application Bundle (.app)
if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='PreflopAdvisor.app',
        icon=None,
        bundle_identifier='com.preflopadvisor.app',
    )
