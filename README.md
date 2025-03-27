[XIAOESP32C6](https://wiki.seeedstudio.com/xiao_esp32c6_bluetooth/)

# Sirius3 LED Controller
## ビルド
```shell
cd python_gui
```

```shell
pyinstaller --onefile --windowed --name="Sirius3 LED Controller" --add-data="sirius3_led_animations.py:." sirius3_led_controller.py --icon=icon.ico
```


## 実行権限付与

```shell
plutil -insert NSBluetoothAlwaysUsageDescription -string "BLEデバイスに接続するために必要です" "dist/Sirius3 LED Controller.app/Contents/Info.plist"
```

```shell
codesign --force --deep --sign - "dist/Sirius3 LED Controller.app"
```