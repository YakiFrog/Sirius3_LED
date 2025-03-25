"""
Sirius3 LED直接コマンド送信ツール
BLEデバイスに直接コマンドを送信してデバッグするためのツール
"""

import sys
import asyncio
import logging
from bleak import BleakScanner, BleakClient
from PySide6.QtWidgets import (QApplication, QMainWindow, QVBoxLayout, QHBoxLayout, QPushButton, 
                             QLabel, QComboBox, QLineEdit, QTextEdit, QWidget, QSpinBox)
from PySide6.QtCore import Qt

# UUIDの定義
SERVICE_UUID = "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
CHARACTERISTIC_UUID = "beb5483e-36e1-4688-b7f5-ea07361b26a8"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class DebugWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.device = None
        self.client = None
        self.loop = asyncio.new_event_loop()
        
        self.init_ui()
        
    def init_ui(self):
        self.setWindowTitle("Sirius3 LEDデバッグモード")
        self.setGeometry(100, 100, 600, 500)
        
        central_widget = QWidget()
        layout = QVBoxLayout()
        
        # デバイス選択
        device_layout = QHBoxLayout()
        device_layout.addWidget(QLabel("デバイス:"))
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(300)
        device_layout.addWidget(self.device_combo)
        self.scan_btn = QPushButton("スキャン")
        self.scan_btn.clicked.connect(self.scan_devices)
        device_layout.addWidget(self.scan_btn)
        layout.addLayout(device_layout)
        
        # 接続ボタン
        connect_layout = QHBoxLayout()
        self.connect_btn = QPushButton("接続")
        self.connect_btn.clicked.connect(self.connect_device)
        self.connect_btn.setEnabled(False)
        connect_layout.addWidget(self.connect_btn)
        self.disconnect_btn = QPushButton("切断")
        self.disconnect_btn.clicked.connect(self.disconnect_device)
        self.disconnect_btn.setEnabled(False)
        connect_layout.addWidget(self.disconnect_btn)
        layout.addLayout(connect_layout)
        
        # ステータス表示
        self.status_label = QLabel("ステータス: 未接続")
        layout.addWidget(self.status_label)
        
        # コマンド送信部分
        layout.addWidget(QLabel("コマンド送信:"))
        
        # プリセットコマンド
        preset_layout = QHBoxLayout()
        preset_layout.addWidget(QLabel("プリセット:"))
        self.preset_combo = QComboBox()
        self.preset_combo.addItems([
            "M:1 (自動モード ON)",
            "M:0 (自動モード OFF)",
            "H:0 (色相 0)",
            "H:128 (色相 128)",
            "H:255 (色相 255)",
            "C:255,0,0 (赤色)",
            "C:0,255,0 (緑色)",
            "C:0,0,255 (青色)",
            "C:255,255,255 (白色)",
            "T:255,0,0,1000 (赤色に1秒で遷移)",
            "T:0,255,0,2000 (緑色に2秒で遷移)",
            "T:0,0,255,500 (青色に0.5秒で遷移)"
        ])
        preset_layout.addWidget(self.preset_combo)
        self.send_preset_btn = QPushButton("送信")
        self.send_preset_btn.clicked.connect(self.send_preset)
        self.send_preset_btn.setEnabled(False)
        preset_layout.addWidget(self.send_preset_btn)
        layout.addLayout(preset_layout)
        
        # カスタムコマンド
        custom_layout = QHBoxLayout()
        custom_layout.addWidget(QLabel("カスタム:"))
        self.command_input = QLineEdit()
        self.command_input.setPlaceholderText("例: M:1 または H:128 または C:255,0,0 または T:255,0,0,1000")
        custom_layout.addWidget(self.command_input)
        self.send_custom_btn = QPushButton("送信")
        self.send_custom_btn.clicked.connect(self.send_custom)
        self.send_custom_btn.setEnabled(False)
        custom_layout.addWidget(self.send_custom_btn)
        layout.addLayout(custom_layout)
        
        # 色遷移コマンド
        transition_group = QWidget()
        transition_layout = QVBoxLayout(transition_group)
        transition_layout.addWidget(QLabel("色遷移コマンド (T:):"))
        
        # 説明文
        transition_info = QLabel("色遷移コマンド(T:)は全モードで使用可能。遷移中に新しいコマンドが送られると、その時点の色から新しい目標色へ遷移します。")
        transition_info.setWordWrap(True)
        transition_info.setStyleSheet("color: blue;")
        transition_layout.addWidget(transition_info)
        
        # RGB値の入力
        rgb_layout = QHBoxLayout()
        rgb_layout.addWidget(QLabel("R:"))
        self.r_input = QSpinBox()
        self.r_input.setRange(0, 255)
        self.r_input.setValue(255)
        rgb_layout.addWidget(self.r_input)
        
        rgb_layout.addWidget(QLabel("G:"))
        self.g_input = QSpinBox()
        self.g_input.setRange(0, 255)
        self.g_input.setValue(0)
        rgb_layout.addWidget(self.g_input)
        
        rgb_layout.addWidget(QLabel("B:"))
        self.b_input = QSpinBox()
        self.b_input.setRange(0, 255)
        self.b_input.setValue(0)
        rgb_layout.addWidget(self.b_input)
        
        transition_layout.addLayout(rgb_layout)
        
        # 遷移時間の入力
        time_layout = QHBoxLayout()
        time_layout.addWidget(QLabel("遷移時間 (ミリ秒):"))
        self.time_input = QSpinBox()
        self.time_input.setRange(100, 10000)
        self.time_input.setSingleStep(100)
        self.time_input.setValue(1000)
        time_layout.addWidget(self.time_input)
        
        self.send_transition_btn = QPushButton("遷移開始")
        self.send_transition_btn.clicked.connect(self.send_transition)
        self.send_transition_btn.setEnabled(False)
        time_layout.addWidget(self.send_transition_btn)
        
        transition_layout.addLayout(time_layout)
        layout.addWidget(transition_group)
        
        # ログ表示
        layout.addWidget(QLabel("ログ:"))
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        layout.addWidget(self.log_text)
        
        # 起動時の説明を追加
        self.log("Sirius3 LEDデバッグツールを起動しました")
        self.log("コマンド一覧:")
        self.log("・M:0/1 - モード設定 (0:固定色モード、1:自動色相変化モード)")
        self.log("・C:R,G,B - RGB色設定 (例: C:255,0,0 で赤色)")
        self.log("・H:hue - 色相設定 (0-255の値)")
        self.log("・T:R,G,B,time - 色遷移 (例: T:255,0,0,1000 で1秒かけて赤色に遷移)")
        self.log("※T:コマンドは全モード（自動/固定/音声連動）で使用可能です")
        
        central_widget.setLayout(layout)
        self.setCentralWidget(central_widget)
        
    def scan_devices(self):
        self.log("デバイスをスキャンしています...")
        self.scan_btn.setEnabled(False)
        self.scan_btn.setText("スキャン中...")
        self.device_combo.clear()
        
        async def do_scan():
            devices = await BleakScanner.discover()
            device_list = []
            for device in devices:
                if device.name:
                    device_list.append((device.name, device.address))
                    self.log(f"デバイス発見: {device.name} ({device.address})")
            
            self.device_combo.clear()
            for name, addr in device_list:
                self.device_combo.addItem(f"{name} ({addr})", addr)
            
            self.scan_btn.setEnabled(True)
            self.scan_btn.setText("スキャン")
            
            if self.device_combo.count() > 0:
                self.connect_btn.setEnabled(True)
        
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(do_scan())
    
    def connect_device(self):
        if self.device_combo.currentIndex() < 0:
            return
        
        address = self.device_combo.currentData()
        self.log(f"デバイスに接続しています: {address}")
        self.connect_btn.setEnabled(False)
        
        async def do_connect():
            try:
                self.client = BleakClient(address)
                await self.client.connect()
                
                if self.client.is_connected:
                    self.status_label.setText(f"ステータス: 接続済み ({address})")
                    self.disconnect_btn.setEnabled(True)
                    self.send_preset_btn.setEnabled(True)
                    self.send_custom_btn.setEnabled(True)
                    self.send_transition_btn.setEnabled(True)
                    self.log("接続成功")
                else:
                    self.status_label.setText("ステータス: 接続失敗")
                    self.connect_btn.setEnabled(True)
                    self.log("接続失敗")
            except Exception as e:
                self.log(f"接続エラー: {str(e)}")
                self.connect_btn.setEnabled(True)
        
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(do_connect())
    
    def disconnect_device(self):
        self.log("デバイスを切断しています...")
        
        async def do_disconnect():
            try:
                await self.client.disconnect()
                self.log("切断完了")
            except Exception as e:
                self.log(f"切断エラー: {str(e)}")
            finally:
                self.status_label.setText("ステータス: 未接続")
                self.connect_btn.setEnabled(True)
                self.disconnect_btn.setEnabled(False)
                self.send_preset_btn.setEnabled(False)
                self.send_custom_btn.setEnabled(False)
                self.send_transition_btn.setEnabled(False)
                self.client = None
        
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(do_disconnect())
    
    def send_preset(self):
        selected = self.preset_combo.currentText()
        command = selected.split(" ")[0]
        self.send_command(command)
    
    def send_custom(self):
        command = self.command_input.text().strip()
        if command:
            self.send_command(command)
    
    def send_transition(self):
        r = self.r_input.value()
        g = self.g_input.value()
        b = self.b_input.value()
        time_ms = self.time_input.value()
        
        command = f"T:{r},{g},{b},{time_ms}"
        self.send_command(command)
        self.log(f"色遷移コマンド送信: 目標RGB({r},{g},{b})、遷移時間{time_ms}ms")
        self.log("※遷移コマンドは遷移完了後もT:モードを維持します")
    
    def send_command(self, command):
        self.log(f"コマンド送信: {command}")
        
        async def do_send():
            try:
                await self.client.write_gatt_char(CHARACTERISTIC_UUID, command.encode())
                self.log(f"送信成功: {command}")
            except Exception as e:
                self.log(f"送信エラー: {str(e)}")
        
        if self.client and self.client.is_connected:
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(do_send())
        else:
            self.log("デバイスが接続されていません")
    
    def log(self, message):
        self.log_text.append(message)
        logger.info(message)
    
    def closeEvent(self, event):
        if self.client and self.client.is_connected:
            async def cleanup():
                await self.client.disconnect()
            
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(cleanup())
        
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = DebugWindow()
    window.show()
    sys.exit(app.exec())
