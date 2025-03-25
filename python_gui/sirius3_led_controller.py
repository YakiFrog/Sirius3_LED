import sys
import time
import asyncio
import logging
import queue
import concurrent.futures
from threading import Thread, Lock, Event
from functools import partial
from datetime import datetime
import numpy as np
import pyaudio
import struct
import colorsys
from collections import deque

from bleak import BleakScanner, BleakClient
from bleak.exc import BleakError, BleakDeviceNotFoundError

from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                              QHBoxLayout, QPushButton, QLabel, QSlider, QComboBox,
                              QGroupBox, QCheckBox, QColorDialog, QMessageBox,
                              QTextEdit, QSplitter, QProgressBar, QRadioButton,
                              QButtonGroup)
from PySide6.QtCore import Qt, Signal, Slot, QObject, QTimer, QSize
from PySide6.QtGui import QColor, QPainter, QBrush, QTextCursor, QFont

# BLEデバイス情報
DEVICE_NAMES = {
    "LEFT": "Sirius3_LEFT_EAR",
    "RIGHT": "Sirius3_RIGHT_EAR"
}

# UUIDの定義
SERVICE_UUID = "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
CHARACTERISTIC_UUID = "beb5483e-36e1-4688-b7f5-ea07361b26a8"

# コマンドタイプの定義
CMD_MODE = "M"      # モード設定 (0:固定色、1:自動色相変化)
CMD_COLOR = "C"     # RGB色設定
CMD_HUE = "H"       # 色相設定

# ロギング設定
class QTextEditLogger(logging.Handler):
    """QTextEditにログを出力するためのハンドラー"""
    def __init__(self, widget):
        super().__init__()
        self.widget = widget
        self.widget.setReadOnly(True)
        self.widget.setFont(QFont("Monospace", 9))
        
        # フォーマットの設定
        formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', 
                                      datefmt='%H:%M:%S')
        self.setFormatter(formatter)
        
        # エラーメッセージの色を設定
        self.level_colors = {
            logging.DEBUG: "gray",
            logging.INFO: "white",
            logging.WARNING: "orange",
            logging.ERROR: "red",
            logging.CRITICAL: "darkred"
        }
    
    def emit(self, record):
        msg = self.format(record)
        color = self.level_colors.get(record.levelno, "white")
        
        # メインスレッドからの呼び出しを保証
        self.widget.textCursor().insertHtml(
            f'<font color="{color}">{msg}</font><br>'
        )
        
        # 自動スクロール
        self.widget.moveCursor(QTextCursor.End)
        
# BLEコマンドキュー項目
class BLECommand:
    """BLEデバイスに送信するコマンド"""
    def __init__(self, device_key, cmd_type, value, callback=None):
        self.device_key = device_key
        self.cmd_type = cmd_type
        self.value = value
        self.callback = callback
        self.timestamp = time.time()
        
    def get_command_string(self):
        """コマンド文字列を返す"""
        if self.cmd_type == CMD_COLOR:
            r, g, b = self.value
            return f"{self.cmd_type}:{r},{g},{b}"
        else:
            return f"{self.cmd_type}:{self.value}"
            
    def __str__(self):
        return f"BLECommand({self.device_key}, {self.get_command_string()})"

class BLESignals(QObject):
    """BLEコントローラーからのシグナル"""
    connection_status = Signal(str, bool)
    command_status = Signal(str, bool, str)  # device_key, success, message
    log_message = Signal(int, str)  # level, message
    error_occurred = Signal(str)

# ThreadPoolの代わりにシンプルなワーカースレッド実装を追加
class AsyncWorker(Thread):
    """非同期処理を実行するワーカースレッド"""
    
    def __init__(self, name="AsyncWorker"):
        super().__init__(name=name, daemon=True)
        self.queue = queue.Queue()
        self.running = True
        self.loop = None
        # スレッド開始
        self.start()
    
    def run(self):
        """スレッドのメインループ"""
        # 専用のイベントループを作成
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        
        while self.running:
            try:
                # キューからタスクを取得
                task, future = self.queue.get(timeout=0.1)
                
                try:
                    # asyncioタスクを実行
                    result = self.loop.run_until_complete(task)
                    # 結果を設定
                    future.set_result(result)
                except Exception as e:
                    # エラーを設定
                    future.set_exception(e)
                    
                self.queue.task_done()
            except queue.Empty:
                pass
            except Exception as e:
                print(f"AsyncWorker error: {str(e)}")
                
        # 終了時にループをクローズ
        if self.loop and not self.loop.is_closed():
            self.loop.close()
    
    def stop(self):
        """ワーカーを停止"""
        self.running = False
        self.join(timeout=1.0)
    
    def run_coroutine(self, coro):
        """コルーチンを実行して結果を返す"""
        future = concurrent.futures.Future()
        self.queue.put((coro, future))
        return future

# スレッド固有のイベントループを管理する簡易な機能
class BLEIOThread(Thread):
    """BLE通信専用スレッド"""
    
    def __init__(self):
        super().__init__(daemon=True, name="BLE-IO-Thread")
        self.tasks = queue.Queue()
        self.loop = None
        self.running = True
        self.start()
    
    def run(self):
        """スレッドのメインループ"""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        
        while self.running:
            try:
                # タスクを取得
                coro, future = self.tasks.get(timeout=0.1)
                
                try:
                    # コルーチンを実行
                    result = self.loop.run_until_complete(coro)
                    # 結果を設定
                    future.set_result(result)
                except Exception as e:
                    # エラーを設定
                    future.set_exception(e)
                
                self.tasks.task_done()
            except queue.Empty:
                pass
            except Exception as e:
                print(f"BLEIOThread error: {e}")
        
        # 終了時の処理
        if self.loop and not self.loop.is_closed():
            self.loop.close()
    
    def execute(self, coro):
        """コルーチンを実行して結果を返す"""
        future = concurrent.futures.Future()
        self.tasks.put((coro, future))
        return future
    
    def stop(self):
        """スレッドを停止"""
        self.running = False
        self.join(timeout=1.0)

class BLEController(QObject):
    """BLEデバイスとの通信を管理するコントローラー"""
    
    def __init__(self):
        super().__init__()
        
        # デバイス管理
        self.clients = {
            "LEFT": None,
            "RIGHT": None
        }
        self.connected = {
            "LEFT": False,
            "RIGHT": False
        }
        self.device_addresses = {
            "LEFT": None,
            "RIGHT": None
        }
        
        # スレッド管理
        self.command_queue = queue.Queue()
        self.queue_processing = False
        self.stop_event = Event()
        
        # BLE IO専用スレッド
        self.io_thread = BLEIOThread()
        
        # 同期オブジェクト
        self.lock = Lock()
        self.signals = BLESignals()
        
        # 通信タイムアウト設定（秒）
        self.command_timeout = 5.0  
        
        # コマンド送信間隔（秒）
        self.command_interval = 0.1

        # オーディオ連動モード
        self.audio_mode = False
        self.audio_timer = None
    
    def start_queue_processor(self):
        """コマンドキュー処理スレッドを開始"""
        if not self.queue_processing:
            self.queue_processing = True
            self.stop_event.clear()
            Thread(target=self._process_command_queue, daemon=True, 
                  name="CommandQueueProcessor").start()
    
    def stop_queue_processor(self):
        """コマンドキュー処理スレッドを停止"""
        self.stop_event.set()
        self.queue_processing = False
    
    def _log(self, level, message):
        """ログメッセージを発行"""
        self.signals.log_message.emit(level, message)
        
    def _process_command_queue(self):
        """コマンドキューを処理するスレッド関数"""
        self._log(logging.INFO, "コマンドキュー処理を開始しました")
        
        while not self.stop_event.is_set():
            try:
                # キューからコマンドを取得（タイムアウト付き）
                try:
                    command = self.command_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                
                # コマンドの処理
                device_key = command.device_key
                
                # 対象デバイスの接続状態をチェック
                if not self.connected.get(device_key, False):
                    self._log(logging.WARNING, f"{device_key}デバイスは接続されていません。コマンドをスキップします: {command}")
                    if command.callback:
                        command.callback(False)
                    self.command_queue.task_done()
                    continue
                
                # コマンドの処理
                if self.audio_mode and command.cmd_type == CMD_COLOR:
                    # オーディオ連動モードの場合、色設定コマンドは無視
                    self.command_queue.task_done()
                    continue
                
                # BLEコマンドを実行
                success = self._execute_ble_command(command)
                
                # コールバックがあれば呼び出し
                if command.callback:
                    command.callback(success)
                
                self.command_queue.task_done()
                
                # 連続送信による過負荷を防ぐための短いスリープ
                time.sleep(self.command_interval)  # 設定可能な間隔を使用
                
            except Exception as e:
                self._log(logging.ERROR, f"コマンドキュー処理中にエラーが発生しました: {str(e)}")
                continue
                
        self._log(logging.INFO, "コマンドキュー処理を終了しました")
    
    def _execute_ble_command(self, command):
        """BLEコマンドを実行"""
        device_key = command.device_key
        command_str = command.get_command_string()
        
        try:
            # デバイス取得（スレッドセーフに）
            client = None
            with self.lock:
                client = self.clients.get(device_key)
                if not client or not self.connected.get(device_key, False):
                    self._log(logging.WARNING, f"{device_key}デバイスは接続されていません")
                    return False
            
            # 送信処理
            async def send_command():
                try:
                    self._log(logging.DEBUG, f"{device_key}デバイスにコマンド送信開始: {command_str}")
                    await client.write_gatt_char(CHARACTERISTIC_UUID, command_str.encode())
                    self._log(logging.DEBUG, f"{device_key}デバイスにコマンド送信完了: {command_str}")
                    return True
                except Exception as e:
                    self._log(logging.ERROR, f"{device_key}デバイスへのコマンド送信エラー: {str(e)}")
                    return False
            
            # IO専用スレッドで実行
            future = self.io_thread.execute(send_command())
            
            try:
                # タイムアウト付きで結果を待機
                result = future.result(timeout=self.command_timeout)
                
                if result:
                    self._log(logging.INFO, f"{device_key}デバイスにコマンド送信: {command_str}")
                    self.signals.command_status.emit(device_key, True, f"コマンド送信成功: {command_str}")
                    return True
                else:
                    self.signals.command_status.emit(device_key, False, f"コマンド送信失敗: {command_str}")
                    return False
            except concurrent.futures.TimeoutError:
                self._log(logging.ERROR, f"{device_key}デバイスへのコマンド送信がタイムアウトしました: {command_str}")
                self.signals.command_status.emit(device_key, False, f"コマンド送信タイムアウト: {command_str}")
                self._update_connection_status(device_key, False)
                return False
        except Exception as e:
            self._log(logging.ERROR, f"{device_key}デバイスへのコマンド送信に失敗: {str(e)}")
            self.signals.command_status.emit(device_key, False, f"コマンド送信エラー: {str(e)}")
            return False
    
    def _update_connection_status(self, device_key, connected):
        """接続状態を更新"""
        with self.lock:
            self.connected[device_key] = connected
            self.signals.connection_status.emit(device_key, connected)
    
    def scan_and_connect(self, device_key):
        """デバイスをスキャンして接続"""
        device_name = DEVICE_NAMES.get(device_key)
        if not device_name:
            self._log(logging.ERROR, f"不明なデバイスキー: {device_key}")
            return False
        
        self._log(logging.INFO, f"{device_key} ({device_name})デバイスを探しています...")
        future = concurrent.futures.Future()
        
        # 接続処理
        async def scan_and_connect_async():
            try:
                # デバイススキャン
                devices = await BleakScanner.discover(timeout=5.0)
                
                target_device = None
                for device in devices:
                    if device.name == device_name:
                        self._log(logging.INFO, f"デバイスが見つかりました: {device.name} ({device.address})")
                        target_device = device
                        break
                
                if not target_device:
                    self._log(logging.WARNING, f"{device_key}デバイスが見つかりませんでした")
                    return False
                
                # アドレスを保存
                self.device_addresses[device_key] = target_device.address
                
                # 接続
                client = BleakClient(target_device.address)
                await client.connect()
                
                if client.is_connected:
                    with self.lock:
                        self.clients[device_key] = client
                        self.connected[device_key] = True
                    
                    self._log(logging.INFO, f"{device_key}デバイスに接続しました")
                    self._update_connection_status(device_key, True)
                    return True
                else:
                    self._log(logging.WARNING, f"{device_key}デバイスに接続できませんでした")
                    return False
            except Exception as e:
                self._log(logging.ERROR, f"{device_key}デバイスへの接続中にエラーが発生: {str(e)}")
                return False
        
        # IO専用スレッドで実行
        io_future = self.io_thread.execute(scan_and_connect_async())
        
        # 完了コールバック
        def on_done(f):
            try:
                result = f.result()
                future.set_result(result)
            except Exception as e:
                self._log(logging.ERROR, f"接続処理中にエラーが発生: {str(e)}")
                self._update_connection_status(device_key, False)
                future.set_exception(e)
        
        io_future.add_done_callback(on_done)
        return future
    
    def disconnect(self, device_key):
        """デバイスを切断"""
        future = concurrent.futures.Future()
        
        with self.lock:
            if not self.clients.get(device_key) or not self.connected.get(device_key, False):
                self._log(logging.WARNING, f"{device_key}デバイスは接続されていません")
                future.set_result(False)
                return future
        
        client = self.clients.get(device_key)
        
        # 切断処理
        async def disconnect_async():
            try:
                await client.disconnect()
                return True
            except Exception as e:
                self._log(logging.ERROR, f"{device_key}デバイスの切断中にエラーが発生: {str(e)}")
                return False
        
        # IO専用スレッドで実行
        io_future = self.io_thread.execute(disconnect_async())
        
        # 完了コールバック
        def on_done(f):
            try:
                result = f.result()
                
                # 接続状態を更新
                with self.lock:
                    self.clients[device_key] = None
                    self.connected[device_key] = False
                
                self._log(logging.INFO, f"{device_key}デバイスを切断しました")
                self._update_connection_status(device_key, False)
                future.set_result(result)
            except Exception as e:
                self._log(logging.ERROR, f"{device_key}デバイスの切断処理でエラー: {str(e)}")
                
                # エラーが発生しても接続状態をリセット
                with self.lock:
                    self.clients[device_key] = None
                    self.connected[device_key] = False
                
                self._update_connection_status(device_key, False)
                future.set_exception(e)
        
        io_future.add_done_callback(on_done)
        return future

    def enqueue_command(self, device_key, cmd_type, value, callback=None):
        """コマンドをキューに追加"""
        command = BLECommand(device_key, cmd_type, value, callback)
        self._log(logging.DEBUG, f"コマンドをキューに追加: {command}")
        self.command_queue.put(command)
        
        # コマンドキュー処理が動いていなければ開始
        if not self.queue_processing:
            self.start_queue_processor()
    
    def set_rgb_color(self, device_key, r, g, b, callback=None):
        """RGB値で色を設定"""
        self.enqueue_command(device_key, CMD_COLOR, (r, g, b), callback)
    
    def set_mode(self, device_key, auto_mode, callback=None):
        """モードを設定 (0=固定色, 1=自動色相変化)"""
        mode_value = 1 if auto_mode else 0
        self.enqueue_command(device_key, CMD_MODE, mode_value, callback)
    
    def set_hue(self, device_key, hue, callback=None):
        """色相を設定 (0-255)"""
        self.enqueue_command(device_key, CMD_HUE, hue, callback)
    
    def apply_settings(self, device_key, auto_mode, r=0, g=0, b=0, hue=0, callback=None):
        """設定を適用"""
        if auto_mode:
            # 自動モードの場合は、モード設定のみ行う（H:コマンドは送信しない）
            self.set_mode(device_key, auto_mode, callback)
        else:
            # 固定色モードの場合は、M:0は送らずに直接色だけを設定
            self.set_rgb_color(device_key, r, g, b, callback)
    
    def apply_settings_to_both(self, auto_mode, r=0, g=0, b=0, hue=0, callback=None):
        """両方のデバイスに設定を適用"""
        # 接続済みのデバイスを確認
        connected_devices = []
        for device_key in ["LEFT", "RIGHT"]:
            if self.connected.get(device_key, False):
                connected_devices.append(device_key)
        
        if not connected_devices:
            self._log(logging.WARNING, "接続されているデバイスがありません")
            if callback:
                callback(False)
            return
        
        if auto_mode:
            # 同時にモード変更コマンドを送信
            commands = []
            for device_key in connected_devices:
                commands.append((device_key, CMD_MODE, 1))
            
            self._send_commands_simultaneously(commands, callback)
        else:
            # 同時に色設定コマンドを送信
            commands = []
            for device_key in connected_devices:
                commands.append((device_key, CMD_COLOR, (r, g, b)))
            
            self._send_commands_simultaneously(commands, callback)
    
    def _send_commands_simultaneously(self, commands, callback=None):
        """複数のコマンドをできるだけ同時に送信"""
        if not commands:
            if callback:
                callback(True)
            return
        
        # 同時実行するために全てのコマンドを先に準備
        prepared_commands = []
        command_strs = []
        
        for device_key, cmd_type, value in commands:
            try:
                # デバイス取得（スレッドセーフに）
                with self.lock:
                    client = self.clients.get(device_key)
                    if not client or not self.connected.get(device_key, False):
                        self._log(logging.WARNING, f"{device_key}デバイスは接続されていません")
                        continue
                
                # コマンド文字列を生成
                if cmd_type == CMD_COLOR:
                    r, g, b = value
                    command_str = f"{cmd_type}:{r},{g},{b}"
                else:
                    command_str = f"{cmd_type}:{value}"
                
                prepared_commands.append((device_key, client, command_str))
                command_strs.append(f"{device_key}:{command_str}")
                
            except Exception as e:
                self._log(logging.ERROR, f"{device_key}デバイスのコマンド準備に失敗: {str(e)}")
        
        if not prepared_commands:
            if callback:
                callback(False)
            return
        
        self._log(logging.INFO, f"同時コマンド送信: {', '.join(command_strs)}")
        
        # 全てのコマンドを同時に送信するコルーチン
        async def send_all_commands():
            tasks = []
            for device_key, client, command_str in prepared_commands:
                # 各デバイスごとにタスクを作成
                task = asyncio.create_task(self._async_send_command(device_key, client, command_str))
                tasks.append(task)
            
            # 全てのタスクが完了するのを待機
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # 結果を確認
            success = all(isinstance(r, bool) and r for r in results)
            return success
        
        # IO専用スレッドで一括実行
        future = self.io_thread.execute(send_all_commands())
        
        # 完了コールバック
        def on_done(f):
            try:
                result = f.result()
                if callback:
                    callback(result)
            except Exception as e:
                self._log(logging.ERROR, f"同時コマンド送信でエラーが発生: {str(e)}")
                if callback:
                    callback(False)
        
        future.add_done_callback(on_done)
    
    async def _async_send_command(self, device_key, client, command_str):
        """単一コマンドを非同期で送信"""
        try:
            self._log(logging.DEBUG, f"{device_key}デバイスにコマンド送信開始: {command_str}")
            await client.write_gatt_char(CHARACTERISTIC_UUID, command_str.encode())
            self._log(logging.DEBUG, f"{device_key}デバイスにコマンド送信完了: {command_str}")
            return True
        except Exception as e:
            self._log(logging.ERROR, f"{device_key}デバイスへのコマンド送信エラー: {str(e)}")
            return False
    
    def cleanup(self):
        """リソースをクリーンアップ"""
        self.stop_queue_processor()
        
        # IO専用スレッドを停止
        if hasattr(self, 'io_thread'):
            self.io_thread.stop()

    def check_connection(self, device_key):
        """デバイスの接続状態をチェック"""
        future = concurrent.futures.Future()
        
        with self.lock:
            client = self.clients.get(device_key)
            if not client:
                self._log(logging.DEBUG, f"{device_key}デバイスのクライアントが存在しません")
                self._update_connection_status(device_key, False)
                future.set_result(False)
                return future
        
        # 接続状態確認処理
        async def check_connection_async():
            try:
                if client.is_connected:
                    # services プロパティを使用して警告を回避
                    services = client.services
                    if services:
                        return True
                return False
            except Exception as e:
                self._log(logging.DEBUG, f"{device_key}デバイス接続確認中にエラー: {str(e)}")
                return False
        
        # IO専用スレッドで実行
        io_future = self.io_thread.execute(check_connection_async())
        
        # 完了コールバック
        def on_done(f):
            try:
                result = f.result()
                # 接続状態を更新
                self._update_connection_status(device_key, result)
                future.set_result(result)
            except Exception as e:
                self._log(logging.ERROR, f"{device_key}デバイスの接続確認でエラー: {str(e)}")
                self._update_connection_status(device_key, False)
                future.set_exception(e)
        
        io_future.add_done_callback(on_done)
        return future
    
    def check_all_connections(self):
        """全デバイスの接続状態をチェック"""
        futures = []
        for device_key in ["LEFT", "RIGHT"]:
            if self.clients.get(device_key):
                futures.append(self.check_connection(device_key))
        
        return futures
    
    def set_audio_mode(self, enabled):
        """オーディオ連動モードの設定"""
        self.audio_mode = enabled
        
        # オーディオ連動タイマーの制御
        if self.audio_mode:
            self._log(logging.INFO, "オーディオ連動モードを開始しました")
        else:
            self._log(logging.INFO, "オーディオ連動モードを停止しました")
    
    def update_audio_color(self, color):
        """オーディオ処理からの色更新"""
        if not self.audio_mode:
            return
            
        # 接続済みのデバイスを確認
        connected_devices = []
        for device_key in ["LEFT", "RIGHT"]:
            if self.connected.get(device_key, False):
                connected_devices.append(device_key)
        
        if not connected_devices:
            return
            
        # 全デバイスに同時に色を送信
        commands = []
        r, g, b = color.red(), color.green(), color.blue()
        
        for device_key in connected_devices:
            commands.append((device_key, CMD_COLOR, (r, g, b)))
        
        # コールバックなしで送信（軽量処理）
        self._send_commands_simultaneously(commands)

class ColorPreviewWidget(QWidget):
    """色のプレビューを表示するウィジェット"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.color = QColor(255, 255, 255)
        self.setMinimumSize(100, 50)
        
    def setColor(self, color):
        self.color = color
        self.update()
        
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setBrush(QBrush(self.color))
        painter.drawRect(0, 0, self.width(), self.height())

class AudioProcessor(QObject):
    """音声信号を処理してLED色情報に変換するクラス"""
    
    # 色更新シグナル
    color_changed = Signal(QColor)
    audio_level = Signal(float)  # 0.0-1.0 のレベル
    
    def __init__(self):
        super().__init__()
        
        # ロガーの設定
        self.logger = logging.getLogger("sirius3.audio")
        
        # PyAudioの設定
        self.p = pyaudio.PyAudio()
        self.CHUNK = 1024  # 一度に読み取るサンプル数
        self.FORMAT = pyaudio.paInt16  # 16bit整数
        self.CHANNELS = 1  # モノラル
        self.RATE = 44100  # サンプリングレート
        
        # 音声処理用の変数
        self.stream = None
        self.running = False
        self.thread = None
        self.lock = Lock()
        
        # FFT解析用のバッファ
        self.fft_buffer = deque(maxlen=8)  # バッファサイズを増やす
        
        # パラメータ設定を調整
        self.sensitivity = 0.65      # 感度を上げる
        self.smoothing = 0.85       # スムージングをより強く
        self.bass_boost = 1.2       # 低音の強調を調整
        self.treble_boost = 1.1     # 高音の強調を調整
        
        # 色変化用のパラメータ調整
        self.color_smoothing = 0.82  # 色の変化をより滑らかに
        self.saturation_min = 0.6    # 最小彩度を上げる（より鮮やか）
        self.value_min = 0.5         # 最小明度を上げる（より明るく）
        self.value_boost = 1.4       # 明度のブースト係数を上げる
        
        # FFTバッファサイズを増やして安定化
        self.fft_buffer = deque(maxlen=12)  # バッファサイズを増やす
        
        # 色相範囲の設定（0-1の範囲）
        self.hue_range = (0.0, 1.0)  # 全色相を使用
        
        # 移動平均用のバッファサイズを増やす
        self.hue_buffer_size = 8
        self.value_buffer_size = 8
        
        # バンドごとの重み付け調整
        self.band_weights = {
            "sub_bass": 1.8,   # サブベース
            "bass": 1.5,       # ベース
            "low_mid": 1.2,    # 低中音
            "mid": 1.0,        # 中音
            "high_mid": 1.3,   # 高中音
            "high": 1.4        # 高音
        }
        
        # 前回の色とレベル値（スムージング用）
        self.prev_hue = 0.0
        self.prev_saturation = 0.0
        self.prev_value = 0.0
        self.prev_level = 0.0
        
        # パワー計算用の指数
        self.power_scale = 1.5     # パワースペクトルのスケーリング係数
        
        # 色相範囲の制限（0-1の範囲で）
        self.hue_range = (0.0, 0.85)  # 赤から紫までの範囲
    
    def start(self):
        """オーディオ処理を開始"""
        if self.running:
            return True

        try:
            # 利用可能なオーディオデバイスをチェック
            input_devices = []
            for i in range(self.p.get_device_count()):
                device_info = self.p.get_device_info_by_index(i)
                if device_info['maxInputChannels'] > 0:
                    input_devices.append(device_info)
                    self.logger.debug(f"検出されたオーディオ入力デバイス: {device_info['name']}")
            
            if not input_devices:
                self.logger.error("利用可能なオーディオ入力デバイスが見つかりません")
                return False

            # デフォルトの入力デバイスを使用
            default_input = self.p.get_default_input_device_info()
            self.logger.info(f"使用するオーディオ入力デバイス: {default_input['name']}")
            
            # オーディオ入力ストリームを開く
            self.stream = self.p.open(
                format=self.FORMAT,
                channels=self.CHANNELS,
                rate=self.RATE,
                input=True,
                input_device_index=default_input['index'],
                frames_per_buffer=self.CHUNK,
                stream_callback=self._audio_callback
            )
            
            self.running = True
            self.thread = Thread(target=self._processing_thread, daemon=True)
            self.thread.start()
            
            return True
            
        except Exception as e:
            self.logger.error(f"オーディオ処理の開始に失敗: {str(e)}")
            return False
    
    def stop(self):
        """オーディオ処理を停止"""
        if self.running:
            self.logger.info("オーディオ処理を停止します")
        self.running = False
        
        if self.stream:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except:
                pass
            finally:
                self.stream = None
        
        if self.thread:
            self.thread.join(timeout=1.0)
            self.thread = None
    
    def _audio_callback(self, in_data, frame_count, time_info, status):
        """オーディオコールバック（別スレッドで呼ばれる）"""
        if status:  # エラー状態をチェック
            self.logger.warning(f"オーディオコールバックでエラー: {status}")
            return (None, pyaudio.paAbort)
            
        with self.lock:
            # バッファにデータを追加
            if self.running:
                self.fft_buffer.append(in_data)
        
        # 処理を続行
        return (None, pyaudio.paContinue)
    
    def _processing_thread(self):
        """オーディオデータを処理するスレッド"""
        
        # 周波数バンドの定義をさらに詳細に
        bands = {
            "sub_bass": (20, 60),    # サブベース
            "bass": (60, 250),       # ベース
            "low_mid": (250, 500),   # 低中音
            "mid": (500, 2000),      # 中音
            "high_mid": (2000, 4000),# 高中音
            "high": (4000, 12000)    # 高音
        }
        
        # 移動平均用のバッファ
        hue_buffer = deque([0.0] * 5, maxlen=5)
        value_buffer = deque([0.0] * 5, maxlen=5)
        
        while self.running:
            try:
                # データ取得とFFT処理
                with self.lock:
                    if not self.fft_buffer:
                        time.sleep(0.01)
                        continue
                    
                    # 最新のデータを取得
                    data = self.fft_buffer.pop()
                
                # バイトデータを整数に変換
                count = len(data) // 2
                format = f"{count}h"
                samples = struct.unpack(format, data)
                
                # 正規化（-1.0 から 1.0 の範囲に）
                samples = np.array(samples) / 32768.0
                
                # FFT処理
                fft_data = np.abs(np.fft.rfft(samples))
                
                # 周波数ビンのインデックス計算
                freq_bins = np.fft.rfftfreq(len(samples), 1.0/self.RATE)
                
                # 各周波数帯の強度を計算（よりスムーズに）
                band_levels = {}
                for band_name, (low_freq, high_freq) in bands.items():
                    # 該当する周波数範囲のインデックスを取得
                    band_indices = np.where((freq_bins >= low_freq) & (freq_bins <= high_freq))[0]
                    
                    # この帯域の平均振幅を計算
                    if len(band_indices) > 0:
                        # パワースペクトルの計算を改善
                        band_power = np.mean(np.power(fft_data[band_indices], self.power_scale))
                        
                        # 重み付けとブースト処理
                        weight = self.band_weights.get(band_name, 1.0)
                        if band_name in ["sub_bass", "bass"]:
                            band_power *= self.bass_boost
                        elif band_name in ["high_mid", "high"]:
                            band_power *= self.treble_boost
                            
                        band_levels[band_name] = band_power * weight
                    else:
                        band_levels[band_name] = 0.0
                
                # 低音と高音のバランスで色相を計算
                bass_energy = (band_levels["sub_bass"] * 2.0 + band_levels["bass"]) / 3.0
                treble_energy = (band_levels["high_mid"] + band_levels["high"]) / 2.0
                
                # 色相の計算
                target_hue = 0.0
                if bass_energy > 0 or treble_energy > 0:
                    # より自然な色相の変化
                    total_energy = bass_energy + treble_energy
                    if total_energy > 0:
                        balance = bass_energy / total_energy
                        # 色相範囲のマッピング (低音が強いほど赤系、高音が強いほど青系)
                        hue_min, hue_max = self.hue_range
                        target_hue = hue_min + (hue_max - hue_min) * (1.0 - balance)
                
                # 色相の移動平均を計算
                hue_buffer.append(target_hue)
                smoothed_hue = np.mean(hue_buffer)
                
                # 中音のエネルギーで彩度を決定
                mid_energy = (band_levels["low_mid"] + band_levels["mid"] + band_levels["high_mid"]) / 3.0
                target_saturation = max(
                    self.saturation_min,
                    min(1.0, mid_energy * 2.0 * self.sensitivity)
                )
                
                # 全体的な強度で明度を決定
                overall_level = np.mean([
                    band_levels[band] for band in bands.keys()
                ])
                base_value = max(
                    self.value_min,
                    min(1.0, overall_level * self.sensitivity * self.value_boost)
                )
                
                # 明度の移動平均を計算
                value_buffer.append(base_value)
                smoothed_value = np.mean(value_buffer)
                
                # さらに強いスムージング処理
                hue = smoothed_hue * (1.0 - self.color_smoothing) + self.prev_hue * self.color_smoothing
                saturation = target_saturation * (1.0 - self.color_smoothing) + self.prev_saturation * self.color_smoothing
                value = smoothed_value * (1.0 - self.smoothing) + self.prev_value * self.smoothing
                
                # 前回の値を更新
                self.prev_hue = hue
                self.prev_saturation = saturation
                self.prev_value = value
                
                # HSVからRGBに変換
                r, g, b = colorsys.hsv_to_rgb(hue, saturation, value)
                
                # QColorに変換して発信
                color = QColor(
                    int(r * 255), 
                    int(g * 255), 
                    int(b * 255)
                )
                
                # 信号発信
                self.color_changed.emit(color)
                self.audio_level.emit(smoothed_value)
                
                # フレームレートを調整
                time.sleep(0.04)  # 25FPSに制限してより安定した表示に
                
            except Exception as e:
                logging.error(f"オーディオ処理中にエラー: {str(e)}")
                time.sleep(0.1)  # エラー時は少し待機
    
    def cleanup(self):
        """リソースの解放"""
        self.stop()
        if self.p:
            self.p.terminate()
            self.p = None

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        
        self.current_color = QColor(255, 255, 255)
        self.current_hue = 0
        self.auto_mode = False
        self.audio_mode = False
        
        # オーディオプロセッサの初期化
        self.audio_processor = AudioProcessor()
        self.audio_processor.color_changed.connect(self.update_audio_color)
        
        # BLEコントローラーの初期化
        self.ble_controller = BLEController()
        self.ble_controller.signals.connection_status.connect(self.update_connection_status)
        self.ble_controller.signals.command_status.connect(self.update_command_status)
        self.ble_controller.signals.log_message.connect(self.log_message)
        self.ble_controller.signals.error_occurred.connect(self.show_error)
        
        # コマンドキュー処理を開始
        self.ble_controller.start_queue_processor()
        
        # UI初期化
        self.init_ui()
        
        # ロギング設定
        self.logger = logging.getLogger("sirius3")
        self.logger.setLevel(logging.DEBUG)
        
        # コンソールハンドラー
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)
        
        # QTextEditハンドラー
        text_handler = QTextEditLogger(self.log_text)
        text_handler.setLevel(logging.INFO)
        self.logger.addHandler(text_handler)
        
        self.logger.info("シリウス3 LEDコントローラーを起動しました")
        
        # 接続状態定期チェック用タイマー
        self.connection_check_timer = QTimer(self)
        self.connection_check_timer.timeout.connect(self.check_connections)
        self.connection_check_timer.start(5000)  # 5秒ごとに接続状態をチェック
    
    def init_ui(self):
        self.setWindowTitle("Sirius3 LED Controller")
        self.setMinimumSize(800, 600)
        
        # メインレイアウト（上下分割）
        main_splitter = QSplitter(Qt.Vertical)
        
        # 上部ウィジェット（コントロール部分）
        top_widget = QWidget()
        top_layout = QVBoxLayout(top_widget)
        
        # デバイス接続部分
        connection_group = QGroupBox("デバイス接続")
        connection_layout = QHBoxLayout()
        
        # LEFT EAR接続
        left_layout = QVBoxLayout()
        self.left_connect_btn = QPushButton("LEFT EAR 接続")
        self.left_connect_btn.setMinimumHeight(40)
        self.left_status_label = QLabel("未接続")
        self.left_status_label.setStyleSheet("color: red; font-weight: bold;")
        self.left_connect_btn.clicked.connect(lambda: self.connect_device("LEFT"))
        left_layout.addWidget(self.left_connect_btn)
        left_layout.addWidget(self.left_status_label)
        
        # RIGHT EAR接続
        right_layout = QVBoxLayout()
        self.right_connect_btn = QPushButton("RIGHT EAR 接続")
        self.right_connect_btn.setMinimumHeight(40)
        self.right_status_label = QLabel("未接続")
        self.right_status_label.setStyleSheet("color: red; font-weight: bold;")
        self.right_connect_btn.clicked.connect(lambda: self.connect_device("RIGHT"))
        right_layout.addWidget(self.right_connect_btn)
        right_layout.addWidget(self.right_status_label)
        
        connection_layout.addLayout(left_layout)
        connection_layout.addLayout(right_layout)
        connection_group.setLayout(connection_layout)
        top_layout.addWidget(connection_group)
        
        # カラー設定部分
        color_group = QGroupBox("カラー設定")
        color_layout = QVBoxLayout()
        
        # カラープレビュー
        preview_layout = QHBoxLayout()
        preview_layout.addWidget(QLabel("現在の色:"))
        self.color_preview = ColorPreviewWidget()
        self.color_preview.setMinimumHeight(60)
        preview_layout.addWidget(self.color_preview)
        color_layout.addLayout(preview_layout)
        
        # カラーピッカーボタン
        color_btn_layout = QHBoxLayout()
        self.color_picker_btn = QPushButton("カラーピッカー")
        self.color_picker_btn.setMinimumHeight(30)
        self.color_picker_btn.clicked.connect(self.show_color_picker)
        color_btn_layout.addWidget(self.color_picker_btn)
        color_layout.addLayout(color_btn_layout)
        
        # 色相スライダー
        hue_layout = QHBoxLayout()
        hue_layout.addWidget(QLabel("色相:"))
        self.hue_slider = QSlider(Qt.Horizontal)
        self.hue_slider.setRange(0, 255)
        self.hue_slider.setValue(0)
        self.hue_slider.valueChanged.connect(self.hue_changed)
        hue_layout.addWidget(self.hue_slider)
        self.hue_value_label = QLabel("0")
        self.hue_value_label.setMinimumWidth(30)
        hue_layout.addWidget(self.hue_value_label)
        color_layout.addLayout(hue_layout)
        
        # 動作モード
        mode_layout = QHBoxLayout()
        
        # モード選択ラジオボタン
        self.mode_group = QButtonGroup(self)
        
        self.fixed_mode_radio = QRadioButton("固定色モード")
        self.fixed_mode_radio.setChecked(True)
        self.fixed_mode_radio.toggled.connect(self.on_mode_changed)
        self.mode_group.addButton(self.fixed_mode_radio)
        
        self.auto_mode_radio = QRadioButton("自動色相変化モード")
        self.auto_mode_radio.toggled.connect(self.on_mode_changed)
        self.mode_group.addButton(self.auto_mode_radio)
        
        self.audio_mode_radio = QRadioButton("音楽連動モード")
        self.audio_mode_radio.toggled.connect(self.on_mode_changed)
        self.mode_group.addButton(self.audio_mode_radio)
        
        mode_layout.addWidget(self.fixed_mode_radio)
        mode_layout.addWidget(self.auto_mode_radio)
        mode_layout.addWidget(self.audio_mode_radio)
        
        color_layout.addLayout(mode_layout)
        
        # 自動モードのチェックボックスは非表示にする（ラジオボタンに置き換え）
        self.auto_mode_check = QCheckBox("自動色相変化モード")
        self.auto_mode_check.setVisible(False)
        
        color_group.setLayout(color_layout)
        top_layout.addWidget(color_group)
        
        # 適用ボタン
        apply_group = QGroupBox("設定適用")
        apply_layout = QHBoxLayout()
        
        self.apply_left_btn = QPushButton("LEFT EARに適用")
        self.apply_left_btn.setMinimumHeight(50)
        self.apply_left_btn.clicked.connect(lambda: self.apply_settings("LEFT"))
        self.apply_left_btn.setEnabled(False)
        
        self.apply_right_btn = QPushButton("RIGHT EARに適用")
        self.apply_right_btn.setMinimumHeight(50)
        self.apply_right_btn.clicked.connect(lambda: self.apply_settings("RIGHT"))
        self.apply_right_btn.setEnabled(False)
        
        self.apply_both_btn = QPushButton("両方に適用")
        self.apply_both_btn.setMinimumHeight(50)
        self.apply_both_btn.setStyleSheet("font-weight: bold;")
        self.apply_both_btn.clicked.connect(self.apply_to_both)
        self.apply_both_btn.setEnabled(False)
        
        apply_layout.addWidget(self.apply_left_btn)
        apply_layout.addWidget(self.apply_right_btn)
        apply_layout.addWidget(self.apply_both_btn)
        apply_group.setLayout(apply_layout)
        top_layout.addWidget(apply_group)
        
        # ステータス表示
        status_layout = QHBoxLayout()
        status_layout.addWidget(QLabel("ステータス:"))
        self.status_label = QLabel("準備完了")
        status_layout.addWidget(self.status_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumHeight(15)
        self.progress_bar.setVisible(False)
        status_layout.addWidget(self.progress_bar)
        top_layout.addLayout(status_layout)
        
        # 下部ウィジェット（ログ表示）
        bottom_widget = QWidget()
        bottom_layout = QVBoxLayout(bottom_widget)
        
        log_group = QGroupBox("ログ")
        log_layout = QVBoxLayout()
        
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setLineWrapMode(QTextEdit.NoWrap)
        log_layout.addWidget(self.log_text)
        
        log_btn_layout = QHBoxLayout()
        self.clear_log_btn = QPushButton("ログをクリア")
        self.clear_log_btn.clicked.connect(self.clear_log)
        log_btn_layout.addWidget(self.clear_log_btn)
        log_layout.addLayout(log_btn_layout)
        
        log_group.setLayout(log_layout)
        bottom_layout.addWidget(log_group)
        
        # スプリッターに追加
        main_splitter.addWidget(top_widget)
        main_splitter.addWidget(bottom_widget)
        main_splitter.setSizes([400, 200])
        
        self.setCentralWidget(main_splitter)
    
    def log_message(self, level, message):
        """ログメッセージを記録"""
        if level == logging.DEBUG:
            self.logger.debug(message)
        elif level == logging.INFO:
            self.logger.info(message)
        elif level == logging.WARNING:
            self.logger.warning(message)
        elif level == logging.ERROR:
            self.logger.error(message)
        elif level == logging.CRITICAL:
            self.logger.critical(message)
    
    def clear_log(self):
        """ログをクリア"""
        self.log_text.clear()
        self.logger.info("ログをクリアしました")
    
    def connect_device(self, device_key):
        """デバイスに接続/切断"""
        if not self.ble_controller.connected.get(device_key, False):
            # 接続処理
            btn = self.left_connect_btn if device_key == "LEFT" else self.right_connect_btn
            btn.setEnabled(False)
            btn.setText("接続中...")
            
            # プログレスバーを表示
            self.progress_bar.setVisible(True)
            self.progress_bar.setRange(0, 0)  # 不定のプログレス表示
            
            # 接続処理を実行
            future = self.ble_controller.scan_and_connect(device_key)
            
            # 完了時の処理
            def on_connect_done(future):
                self.progress_bar.setVisible(False)
                btn.setEnabled(True)
                
                try:
                    result = future.result()
                    if not result:
                        self.logger.warning(f"{device_key}デバイスへの接続に失敗しました")
                except Exception as e:
                    self.logger.error(f"接続処理中にエラーが発生: {str(e)}")
            
            # 完了コールバックを設定
            future.add_done_callback(on_connect_done)
            
        else:
            # 切断処理
            self.ble_controller.disconnect(device_key)
    
    @Slot(str, bool)
    def update_connection_status(self, device_key, connected):
        """接続状態の表示を更新"""
        if device_key == "LEFT":
            btn = self.left_connect_btn
            label = self.left_status_label
            apply_btn = self.apply_left_btn
        else:  # RIGHT
            btn = self.right_connect_btn
            label = self.right_status_label
            apply_btn = self.apply_right_btn
        
        if connected:
            label.setText("接続済み")
            label.setStyleSheet("color: green; font-weight: bold;")
            btn.setText("切断")
            apply_btn.setEnabled(True)
        else:
            label.setText("未接続")
            label.setStyleSheet("color: red; font-weight: bold;")
            btn.setText(f"{device_key} EAR 接続")
            apply_btn.setEnabled(False)
        
        btn.setEnabled(True)
        
        # 両方に適用ボタンの状態を更新
        self.apply_both_btn.setEnabled(
            self.ble_controller.connected.get("LEFT", False) and 
            self.ble_controller.connected.get("RIGHT", False)
        )
    
    @Slot(str, bool, str)
    def update_command_status(self, device_key, success, message):
        """コマンド実行状態を更新"""
        if success:
            self.status_label.setText(f"{device_key}: {message}")
            self.status_label.setStyleSheet("color: green;")
        else:
            self.status_label.setText(f"{device_key}: {message}")
            self.status_label.setStyleSheet("color: red;")
    
    @Slot(str)
    def show_error(self, message):
        """エラーメッセージを表示"""
        QMessageBox.critical(self, "エラー", message)
    
    def show_color_picker(self):
        """カラーピッカーダイアログを表示"""
        color = QColorDialog.getColor(self.current_color, self, "色を選択")
        if (color.isValid()):
            self.current_color = color
            self.color_preview.setColor(color)
            self.auto_mode_check.setChecked(False)  # 色を選択したら自動モードをオフ
    
    def hue_changed(self, value):
        """色相スライダーの値が変更されたときの処理"""
        self.current_hue = value
        self.hue_value_label.setText(str(value))
        
        # 色相に基づいてプレビューの色を更新（HSVからRGB変換）
        h = value / 255.0
        s = 1.0
        v = 1.0
        
        if s == 0.0:
            r = g = b = v
        else:
            h *= 6.0
            i = int(h)
            f = h - i
            p = v * (1.0 - s)
            q = v * (1.0 - s * f)
            t = v * (1.0 - s * (1.0 - f))
            
            if i == 0:
                r, g, b = v, t, p
            elif i == 1:
                r, g, b = q, v, p
            elif i == 2:
                r, g, b = p, v, t
            elif i == 3:
                r, g, b = p, q, v
            elif i == 4:
                r, g, b = t, p, v
            else:
                r, g, b = v, p, q
        
        self.current_color = QColor(
            int(r * 255),
            int(g * 255),
            int(b * 255)
        )
        self.color_preview.setColor(self.current_color)
    
    def on_mode_changed(self):
        """モード切替ラジオボタンが変更されたときの処理"""
        if self.fixed_mode_radio.isChecked():
            self.auto_mode = False
            self.audio_mode = False
            self.color_picker_btn.setEnabled(True)
            self.hue_slider.setEnabled(True)
            
            # オーディオ処理を停止
            self.audio_processor.stop()
            self.ble_controller.set_audio_mode(False)
            
        elif self.auto_mode_radio.isChecked():
            self.auto_mode = True
            self.audio_mode = False
            self.color_picker_btn.setEnabled(False)
            self.hue_slider.setEnabled(True)
            
            # オーディオ処理を停止
            self.audio_processor.stop()
            self.ble_controller.set_audio_mode(False)
            
        elif self.audio_mode_radio.isChecked():
            self.auto_mode = False
            self.audio_mode = True
            self.color_picker_btn.setEnabled(False)
            self.hue_slider.setEnabled(False)
            
            # オーディオ処理を開始
            if not self.audio_processor.start():
                self.logger.error("オーディオ処理の開始に失敗しました")
                self.audio_mode_radio.setChecked(False)
                self.fixed_mode_radio.setChecked(True)
                return
                
            self.ble_controller.set_audio_mode(True)
        
        # 現在選択されているモードをログに出力
        mode_name = "固定色" if self.fixed_mode_radio.isChecked() else \
                    "自動色相変化" if self.auto_mode_radio.isChecked() else \
                    "音楽連動" if self.audio_mode_radio.isChecked() else "不明"
        self.logger.info(f"モードを変更: {mode_name}")
    
    def update_audio_color(self, color):
        """オーディオ処理からの色更新を受け取る"""
        if not self.audio_mode:
            return
            
        # プレビューの色を更新
        self.current_color = color
        self.color_preview.setColor(color)
        
        # BLEコントローラーに色を送信
        self.ble_controller.update_audio_color(color)
    
    def reload_connection(self, device_key):
        """接続状態を再確認"""
        if device_key not in ["LEFT", "RIGHT"]:
            return
        
        # リロードボタンを一時的に無効化
        reload_btn = getattr(self, f"{device_key.lower()}_reload_btn", None)
        if reload_btn:
            reload_btn.setEnabled(False)
        
        # ステータスラベルの表示を更新
        status_label = getattr(self, f"{device_key.lower()}_status_label")
        status_label.setText("確認中...")
        status_label.setStyleSheet("color: blue; font-weight: bold;")
        
        # 接続状態をチェック
        future = self.ble_controller.check_connection(device_key)
        
        def on_check_done(f):
            if reload_btn:
                reload_btn.setEnabled(True)
            try:
                result = f.result()
                self.logger.info(f"{device_key}デバイスの接続状態確認: {'接続中' if result else '未接続'}")
            except Exception as e:
                self.logger.error(f"接続確認中にエラーが発生: {str(e)}")
        
        future.add_done_callback(on_check_done)
    
    def check_connections(self):
        """全デバイスの接続状態を定期的にチェック"""
        futures = self.ble_controller.check_all_connections()
        for future in futures:
            def on_done(f):
                try:
                    f.result()  # 例外をキャッチするため
                except Exception as e:
                    self.logger.debug(f"接続チェック中にエラー: {str(e)}")
            
            future.add_done_callback(on_done)
    
    def apply_settings(self, device_key):
        """設定をデバイスに適用"""
        if not self.ble_controller.connected.get(device_key, False):
            self.logger.warning(f"{device_key}デバイスは接続されていません")
            return
        
        # ボタンを一時的に無効化
        btn = self.apply_left_btn if device_key == "LEFT" else self.apply_right_btn
        btn.setEnabled(False)
        
        # ステータス表示
        self.status_label.setText(f"{device_key}デバイスに設定を適用中...")
        self.status_label.setStyleSheet("color: blue;")
        
        # プログレスバーを表示
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)  # 不定のプログレス表示
        
        # 現在のモードを取得
        if self.audio_mode:
            # 音楽連動モードの場合は、そのままオーディオ処理に委任
            self.status_label.setText(f"{device_key}デバイスは音楽連動モードで動作中です")
            self.status_label.setStyleSheet("color: green;")
            self.progress_bar.setVisible(False)
            btn.setEnabled(True)
            return
            
        # 自動モードかどうか
        auto_mode = self.auto_mode
        
        # 色の値を取得
        r, g, b = self.current_color.red(), self.current_color.green(), self.current_color.blue()
        
        # 現在の色相値を取得
        hue = self.current_hue
        
        # 設定適用
        def on_apply_complete(success):
            btn.setEnabled(True)
            self.progress_bar.setVisible(False)
            
            if success:
                mode_text = "自動色相変化" if auto_mode else "固定色"
                self.status_label.setText(f"{device_key}デバイスに設定を適用しました（{mode_text}モード）")
                self.status_label.setStyleSheet("color: green;")
            else:
                self.status_label.setText(f"{device_key}デバイスへの設定適用に失敗しました")
                self.status_label.setStyleSheet("color: red;")
        
        # 色相値も含めて設定を適用
        self.ble_controller.apply_settings(device_key, auto_mode, r, g, b, hue, on_apply_complete)
    
    def apply_to_both(self):
        """両方のデバイスに設定を適用"""
        if not (self.ble_controller.connected.get("LEFT", False) and self.ble_controller.connected.get("RIGHT", False)):
            self.logger.warning("両方のデバイスが接続されていません")
            return
        
        # ボタンを一時的に無効化
        self.apply_both_btn.setEnabled(False)
        
        # ステータス表示
        self.status_label.setText("両方のデバイスに設定を適用中...")
        self.status_label.setStyleSheet("color: blue;")
        
        # プログレスバーを表示
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)  # 不定のプログレス表示
        
        # 音楽連動モードの場合
        if self.audio_mode:
            self.status_label.setText("両方のデバイスは音楽連動モードで動作中です")
            self.status_label.setStyleSheet("color: green;")
            self.progress_bar.setVisible(False)
            self.apply_both_btn.setEnabled(True)
            return
            
        # 自動モードかどうか
        auto_mode = self.auto_mode
        
        # 色の値を取得
        r, g, b = self.current_color.red(), self.current_color.green(), self.current_color.blue()
        
        # 現在の色相値を取得
        hue = self.current_hue
        
        # 設定適用
        def on_both_complete(success):
            self.apply_both_btn.setEnabled(
                self.ble_controller.connected.get("LEFT", False) and 
                self.ble_controller.connected.get("RIGHT", False)
            )
            self.progress_bar.setVisible(False)
            
            if success:
                mode_text = "自動色相変化" if auto_mode else "固定色"
                self.status_label.setText(f"両方のデバイスに設定を適用しました（{mode_text}モード）")
                self.status_label.setStyleSheet("color: green;")
            else:
                self.status_label.setText("設定適用に一部失敗しました")
                self.status_label.setStyleSheet("color: orange;")
        
        # 色相値も含めて設定を適用
        self.ble_controller.apply_settings_to_both(auto_mode, r, g, b, hue, on_both_complete)
    
    def closeEvent(self, event):
        """アプリケーション終了時の処理"""
        self.logger.info("アプリケーションを終了します")
        
        # オーディオ処理を停止
        if hasattr(self, 'audio_processor'):
            self.audio_processor.cleanup()
        
        # リソース解放
        self.ble_controller.cleanup()
        
        # 各デバイスの切断処理
        for device_key in ["LEFT", "RIGHT"]:
            if self.ble_controller.connected.get(device_key, False):
                try:
                    future = self.ble_controller.disconnect(device_key)
                    # 切断処理が完了するのを少し待つ
                    future.result(timeout=1.0)
                except:
                    pass
        
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
