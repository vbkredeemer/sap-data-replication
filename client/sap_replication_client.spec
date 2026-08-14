# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for SAP Data Replication GUI Client
Builds a standalone Windows .exe with all dependencies bundled.

Usage:
  pip install pyinstaller PySide6 pyrfc pyodbc
  pyinstaller sap_replication_client.spec
"""

import os

a = Analysis(
    ['gui_client.py'],
    pathex=[os.path.dirname(os.path.abspath(__file__))],
    binaries=[],
    datas=[
        ('config.example.json', '.'),
    ],
    hiddenimports=[
        'pyrfc',
        'pyodbc',
        'sap_replicate',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'numpy',
        'scipy',
        'PIL',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    noarchive=False,
)

pyz = PYZ(
    a.pure,
    a.zipped_data,
)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='SAPDataReplication',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # Add icon='icon.ico' if you have one
)