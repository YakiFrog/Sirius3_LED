# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['sirius3_led_controller.py'],
    pathex=[],
    binaries=[('/System/Library/Frameworks/CoreBluetooth.framework', 'CoreBluetooth.framework')],
    datas=[],
    hiddenimports=['bleak', 'pyaudio', 'numpy'],
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
    name='Sirius3 LED Controller',
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
)
app = BUNDLE(
    exe,
    name='Sirius3 LED Controller.app',
    icon=None,
    bundle_identifier='com.nlab.sirius3ledcontroller',
)
