from setuptools import setup

APP = ['sirius3_led_controller.py']
DATA_FILES = []
OPTIONS = {
    'argv_emulation': True,
    'packages': ['PySide6', 'bleak', 'numpy', 'pyaudio'],
    'excludes': ['PyQt5', 'PyInstaller', 'PIL', 'tkinter'],
    'plist': {
        'CFBundleDisplayName': 'Sirius3 LED Controller',
        'CFBundleIdentifier': 'com.nlab.sirius3ledcontroller',
        'CFBundleName': 'Sirius3 LED Controller',
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0.0',
        'NSBluetoothAlwaysUsageDescription': 'このアプリはSirius3 LEDデバイスに接続してコントロールするためにBluetoothを使用します。',
        'NSBluetoothPeripheralUsageDescription': 'このアプリはSirius3 LEDデバイスに接続してコントロールするためにBluetoothを使用します。',
        'NSMicrophoneUsageDescription': 'このアプリは音楽連動モードで音声をキャプチャするためにマイクを使用します。'
    }
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
