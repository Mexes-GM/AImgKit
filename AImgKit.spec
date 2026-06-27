# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('icon.ico', '.')]
binaries = []
hiddenimports = ['tkinterdnd2', 'PIL._tkinter_finder']
tmp_ret = collect_all('tkinterdnd2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# The PyInstaller hook for tkinterdnd2 looks for 'win-x64' but the package ships
# 'win64' — include the native files manually so drag & drop works at runtime.
import tkinterdnd2 as _dnd
_dnd_tkdnd = os.path.join(os.path.dirname(_dnd.__file__), 'tkdnd')
datas += [(_dnd_tkdnd, 'tkinterdnd2/tkdnd')]
# CustomTkinter ships theme/asset files that must be bundled too.
tmp_ret = collect_all('customtkinter')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

import os
_ffmpeg = 'ffmpeg.exe'
if os.path.exists(_ffmpeg):
    datas.append((_ffmpeg, '.'))


a = Analysis(
    ['AImgKit.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AImgKit',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
