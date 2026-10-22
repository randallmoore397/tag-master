# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build file for BluePeak TAG Master.

Builds a folder (onedir) executable that bundles the app logo. Run:

    pyinstaller --clean --noconfirm BluePeak-TAG-Master.spec

The result is dist\\BluePeak TAG Master\\BluePeak TAG Master.exe. The app keeps
its database (bluepeak_tag_master.db) next to the executable, so the folder is
portable and the installer simply copies it under Program Files.
"""
import os

block_cipher = None

project_dir = os.path.abspath(os.getcwd())
logo_name = "TAG Master Pro Logo.png"

datas = []
logo_path = os.path.join(project_dir, logo_name)
if os.path.exists(logo_path):
    datas.append((logo_path, "."))

a = Analysis(
    ["bluepeak_tag_master.py"],
    pathex=[project_dir],
    binaries=[],
    datas=datas,
    hiddenimports=["bluepeak_cloud"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc", "doctest"],
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
    name="BluePeak TAG Master",
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
    icon=os.path.join(project_dir, "BluePeak-TAG-Master.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="BluePeak TAG Master",
)
