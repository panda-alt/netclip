# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:\\Users\\zxy\\Documents\\Python\\Temp\\netclip\\build\\netclip_entry.py'],
    pathex=['C:\\Users\\zxy\\Documents\\Python\\Temp\\netclip'],
    binaries=[],
    datas=[('C:\\Users\\zxy\\Documents\\Python\\Temp\\netclip\\config.example.toml', '.'), ('C:\\Users\\zxy\\Documents\\Python\\Temp\\netclip\\assets\\netclip.ico', 'assets')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['win32api', 'win32evtlog', 'win32evtlogutil', 'pywintypes', 'pythoncom'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='netclip',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['C:\\Users\\zxy\\Documents\\Python\\Temp\\netclip\\assets\\netclip.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='netclip',
)
