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
                              QButtonGroup, QGridLayout)
from PySide6.QtCore import Qt, Signal, Slot, QObject, QTimer, QSize, QEvent
from PySide6.QtGui import QColor, QPainter, QBrush, QTextCursor, QFont, QIcon

# シリウス3アニメーションをインポート
from sirius3_led_animations import LEDAnimation

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
CMD_TRANSITION = "T" # 色遷移設定

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
        QApplication.instance().postEvent(
            self.widget,
            LogUpdateEvent(f'<font color="{color}">{msg}</font><br>')
        )

# LogUpdateEventを追加
class LogUpdateEvent(QEvent):
    def __init__(self, html_text):
        super().__init__(QEvent.Type(QEvent.User + 1))
        self.html_text = html_text

# ウィジェットにイベントハンドラを追加
class LogTextEdit(QTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFont("Monospace", 9))
        
    def event(self, event):
        if event.type() == QEvent.User + 1:
            # LogUpdateEventからのテキスト更新
            cursor = self.textCursor()
            cursor.movePosition(QTextCursor.End)
            cursor.insertHtml(event.html_text)
            self.setTextCursor(cursor)
            # 自動スクロール
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
            return True
        return super().event(event)
        
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
        elif self.cmd_type == CMD_TRANSITION:
            r, g, b, duration = self.value
            return f"{self.cmd_type}:{r},{g},{b},{duration}"
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
        self.audio_transition_time = 100  # オーディオ遷移時間のデフォルト値(ms)
    
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
                devices = await BleakScanner.discover(timeout=2.0)
                
                target_device = None
                for device in devices:
                    if (device.name == device_name):
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
                # await client.connect()
                await client.connect(timeout=2.0, services=[SERVICE_UUID])
                
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
    
    def set_transition_color(self, device_key, r, g, b, duration=1000, callback=None):
        """指定した色へ滑らかに遷移"""
        self.enqueue_command(device_key, CMD_TRANSITION, (r, g, b, duration), callback)
    
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
        
        # 送信するコマンドをログに詳細表示
        command_details = []
        for device_key, cmd_type, value in commands:
            if cmd_type == "C":  # 色設定コマンド
                r, g, b = value
                command_details.append(f"{device_key}:{cmd_type}:{r},{g},{b}")
            elif cmd_type == "T":  # 遷移コマンド
                r, g, b, duration = value
                command_details.append(f"{device_key}:{cmd_type}:{r},{g},{b},{duration}")
            else:
                command_details.append(f"{device_key}:{cmd_type}:{value}")
        
        self._log(logging.DEBUG, f"一括コマンド送信の詳細: {', '.join(command_details)}")
        
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
                elif cmd_type == CMD_TRANSITION:
                    r, g, b, duration = value
                    command_str = f"{cmd_type}:{r},{g},{b},{duration}"
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
    
    def set_audio_transition_time(self, ms):
        """オーディオ連動モードの遷移時間設定"""
        self.audio_transition_time = ms
        self._log(logging.INFO, f"オーディオ連動モードの遷移時間を {ms} msに設定しました")
    
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
            
        # 全デバイスに同時に色を送信（遷移コマンドを使用）
        commands = []
        r, g, b = color.red(), color.green(), color.blue()
        
        for device_key in connected_devices:
            commands.append((device_key, CMD_TRANSITION, (r, g, b, self.audio_transition_time)))
        
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
        
        # パラメータ設定を調整 - 感度を上げてスムージングを減らす
        self.sensitivity = 0.85      # 感度を大幅に上げる（0.65→0.85）
        self.smoothing = 0.65        # スムージングを減らす（0.85→0.65）
        self.bass_boost = 1.3        # 低音の強調を増加
        self.treble_boost = 1.2      # 高音の強調を増加
        
        # 色変化用のパラメータ調整 - よりダイナミックな変化
        self.color_smoothing = 0.55   # 色の変化のスムージングを減らす（0.82→0.55）
        self.saturation_min = 0.65    # 最小彩度を上げる
        self.value_min = 0.3          # 最小明度を下げる（0.5→0.3）- より暗く
        self.value_max = 1.0          # 最大明度
        self.value_boost = 1.7        # 明度のブースト係数を上げる（1.4→1.7）
        
        # FFTバッファサイズを調整
        self.fft_buffer = deque(maxlen=6)  # バッファサイズを減らして反応速度を上げる（12→6）
        
        # 色相範囲の設定（0-1の範囲）
        self.hue_range = (0.0, 1.0)  # 全色相を使用
        
        # 移動平均用のバッファサイズを減らす
        self.hue_buffer_size = 4     # より速い色相の変化（8→4）
        self.value_buffer_size = 4    # より速い明度の変化（8→4）
        
        # バンドごとの重み付け調整 - ベースと高音をより強調
        self.band_weights = {
            "sub_bass": 2.0,   # サブベースの重みを上げる（1.8→2.0）
            "bass": 1.8,       # ベースの重みを上げる（1.5→1.8）
            "low_mid": 1.3,    # 低中音（1.2→1.3）
            "mid": 1.0,        # 中音
            "high_mid": 1.5,   # 高中音の重みを上げる（1.3→1.5）
            "high": 1.7        # 高音の重みを上げる（1.4→1.7）
        }
        
        # 前回の色とレベル値（スムージング用）
        self.prev_hue = 0.0
        self.prev_saturation = 0.0
        self.prev_value = 0.0
        self.prev_level = 0.0
        
        # パワー計算用の指数
        self.power_scale = 1.8     # パワースペクトルのスケーリング係数を上げる（1.5→1.8）
        
        # 色相範囲の制限（0-1の範囲で）
        self.hue_range = (0.0, 0.9)  # 色相範囲を広げる（0.85→0.9）
        
        # 音声反応の更新間隔調整 (ミリ秒)
        self.update_interval = 100  # 更新間隔を短くする（150→100ms）
        self.last_update_time = 0
        
        # ピーク検出用のパラメータ
        self.peak_detection = True   # ピーク検出を有効化
        self.peak_threshold = 0.6    # ピーク検出の閾値
        self.peak_multiplier = 1.5   # ピーク検出時の明度倍率
        self.peak_duration = 100     # ピーク効果の持続時間（ミリ秒）
        self.last_peak_time = 0      # 最後にピークを検出した時間
        self.in_peak = False         # ピーク状態フラグ
        
        # 音量レベルの履歴（ピーク検出用）
        self.level_history = deque(maxlen=20)
    
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
        
        # 移動平均用のバッファ（サイズを小さくして反応速度アップ）
        hue_buffer = deque([0.0] * self.hue_buffer_size, maxlen=self.hue_buffer_size)
        value_buffer = deque([0.0] * self.value_buffer_size, maxlen=self.value_buffer_size)
        
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
                    min(1.0, mid_energy * 2.5 * self.sensitivity)  # 彩度の感度を上げる（2.0→2.5）
                )
                
                # 全体的な強度で明度を決定
                overall_level = np.mean([
                    band_levels[band] for band in bands.keys()
                ])
                
                # ピーク検出のための音量履歴を更新
                self.level_history.append(overall_level)
                
                # ピーク検出処理
                peak_detected = False
                current_time = int(time.time() * 1000)  # 現在時刻（ミリ秒）
                
                if self.peak_detection and len(self.level_history) >= 5:
                    # 直近の平均レベルを計算
                    recent_avg = np.mean(list(self.level_history)[-5:])
                    # 全体の平均レベルを計算
                    overall_avg = np.mean(self.level_history)
                    
                    # 直近の値が平均より大幅に大きい場合はピークと判定
                    if (recent_avg > overall_avg * self.peak_threshold and 
                        recent_avg > self.prev_level * 1.3 and
                        current_time - self.last_peak_time > self.peak_duration * 0.5):
                        peak_detected = True
                        self.last_peak_time = current_time
                        self.in_peak = True
                
                # ピーク効果の適用（ピーク検出時または持続時間内）
                if self.in_peak:
                    # ピーク効果の持続時間をチェック
                    if current_time - self.last_peak_time > self.peak_duration:
                        self.in_peak = False
                    else:
                        # ピーク効果の強さを時間経過で減衰させる
                        peak_factor = 1.0 - ((current_time - self.last_peak_time) / self.peak_duration)
                        # 明度を増加させる効果
                        overall_level *= (1.0 + (self.peak_multiplier - 1.0) * peak_factor)
                
                # 明度の計算（範囲を広げる）
                base_value = max(
                    self.value_min,
                    min(self.value_max, overall_level * self.sensitivity * self.value_boost)
                )
                
                # 明度の移動平均を計算（バッファサイズを減らして反応を早く）
                value_buffer.append(base_value)
                smoothed_value = np.mean(value_buffer)
                
                # スムージング処理（スムージングを減らしてより直接的な反応に）
                hue = smoothed_hue * (1.0 - self.color_smoothing) + self.prev_hue * self.color_smoothing
                saturation = target_saturation * (1.0 - self.color_smoothing) + self.prev_saturation * self.color_smoothing
                value = smoothed_value * (1.0 - self.smoothing) + self.prev_value * self.smoothing
                
                # 前回の値を更新
                self.prev_hue = hue
                self.prev_saturation = saturation
                self.prev_value = value
                self.prev_level = overall_level
                
                # HSVからRGBに変換
                r, g, b = colorsys.hsv_to_rgb(hue, saturation, value)
                
                # QColorに変換して発信
                color = QColor(
                    int(r * 255), 
                    int(g * 255), 
                    int(b * 255)
                )
                
                # 更新間隔を制限して信号発信
                if current_time - self.last_update_time >= self.update_interval or peak_detected:
                    self.color_changed.emit(color)
                    self.audio_level.emit(smoothed_value)
                    self.last_update_time = current_time
                
                # フレームレートを調整（より高いフレームレートで応答性を向上）
                time.sleep(0.03)  # 約33FPSに調整（0.04→0.03）
                
            except Exception as e:
                logging.error(f"オーディオ処理中にエラー: {str(e)}")
                time.sleep(0.1)  # エラー時は少し待機
    
    def cleanup(self):
        """リソースの解放"""
        self.stop()
        if self.p:
            self.p.terminate()
            self.p = None

class ColorPreviewButton(QPushButton):
    """色のプレビューと選択ができるボタン"""
    color_changed = Signal(QColor)
    
    def __init__(self, color=QColor(255, 255, 255), parent=None):
        super().__init__(parent)
        self.color = color
        self.setMinimumSize(30, 30)
        self.setMaximumSize(50, 30)
        self.clicked.connect(self.select_color)
        
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setPen(Qt.black)
        painter.setBrush(QBrush(self.color))
        painter.drawRect(5, 5, self.width() - 10, self.height() - 10)
        
    def select_color(self):
        color = QColorDialog.getColor(self.color, self, "色を選択")
        if color.isValid():
            self.set_color(color)
            self.color_changed.emit(color)
    
    def set_color(self, color):
        self.color = color
        self.update()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        
        # 初期化属性の追加
        self.auto_mode = False  # 自動モードフラグの初期化
        self.current_color = QColor(255, 255, 255)  # 現在の色を白で初期化
        self.current_hue = 0  # 現在の色相を初期化
        
        self.audio_mode = False
        
        # アニメーション切り替え処理のフラグを初期化
        self._animation_transition_in_progress = False
        
        # オーディオプロセッサの初期化
        self.audio_processor = AudioProcessor()
        self.audio_processor.color_changed.connect(self.update_audio_color)
        
        # BLEコントローラーの初期化
        self.ble_controller = BLEController()
        self.ble_controller.signals.connection_status.connect(self.update_connection_status)
        self.ble_controller.signals.command_status.connect(self.update_command_status)
        self.ble_controller.signals.log_message.connect(self.log_message)
        self.ble_controller.signals.error_occurred.connect(self.show_error)
        
        # LEDアニメーションコントローラーの初期化
        self.led_animation = LEDAnimation(self.ble_controller)
        self.led_animation.signals.animation_started.connect(self.on_animation_started)
        self.led_animation.signals.animation_stopped.connect(self.on_animation_stopped)
        self.led_animation.signals.status_message.connect(self.on_animation_status)
        
        # アニメーション後の色を白色に初期設定
        self.led_animation.set_after_animation_color(QColor(255, 255, 255))
        
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
        # ここの "levellevel" を "levelname" に修正
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
        self.setMinimumSize(1200, 800)  # ウィンドウサイズを大きくする
        
        # メインウィジェットとレイアウト（左右分割）
        main_widget = QWidget()
        main_layout = QHBoxLayout(main_widget)
        
        # 左ペイン（基本設定）
        left_pane = QWidget()
        left_layout = QVBoxLayout(left_pane)
        
        # 右ペイン（アニメーション制御とログ）
        right_pane = QWidget()
        right_layout = QVBoxLayout(right_pane)
        
        # ===== 左ペインのコンテンツ =====
        
        # デバイス接続部分
        connection_group = QGroupBox("デバイス接続")
        connection_layout = QVBoxLayout()
        
        # 両方同時接続ボタン
        both_connect_layout = QHBoxLayout()
        self.both_connect_btn = QPushButton("両方同時に接続")
        self.both_connect_btn.setMinimumHeight(40)
        self.both_connect_btn.setStyleSheet("font-weight: bold;")
        self.both_connect_btn.clicked.connect(self.connect_both_devices)
        both_connect_layout.addWidget(self.both_connect_btn)
        connection_layout.addLayout(both_connect_layout)
        
        # 個別接続ボタン
        individual_connect_layout = QHBoxLayout()
        
        # LEFT EAR接続
        left_layout_conn = QVBoxLayout()
        self.left_connect_btn = QPushButton("LEFT EAR 接続")
        self.left_connect_btn.setMinimumHeight(40)
        self.left_status_label = QLabel("未接続")
        self.left_status_label.setStyleSheet("color: red; font-weight: bold;")
        self.left_connect_btn.clicked.connect(lambda: self.connect_device("LEFT"))
        left_layout_conn.addWidget(self.left_connect_btn)
        left_layout_conn.addWidget(self.left_status_label)
        
        # RIGHT EAR接続
        right_layout_conn = QVBoxLayout()
        self.right_connect_btn = QPushButton("RIGHT EAR 接続")
        self.right_connect_btn.setMinimumHeight(40)
        self.right_status_label = QLabel("未接続")
        self.right_status_label.setStyleSheet("color: red; font-weight: bold;")
        self.right_connect_btn.clicked.connect(lambda: self.connect_device("RIGHT"))
        right_layout_conn.addWidget(self.right_connect_btn)
        right_layout_conn.addWidget(self.right_status_label)
        
        individual_connect_layout.addLayout(left_layout_conn)
        individual_connect_layout.addLayout(right_layout_conn)
        connection_layout.addLayout(individual_connect_layout)
        
        connection_group.setLayout(connection_layout)
        left_layout.addWidget(connection_group)
        
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
        
        # 音楽連動モード設定
        audio_settings_layout = QHBoxLayout()
        audio_settings_layout.addWidget(QLabel("音声連動更新間隔:"))
        self.audio_interval_slider = QSlider(Qt.Horizontal)
        self.audio_interval_slider.setRange(100, 500)  # 0.1秒から0.5秒
        self.audio_interval_slider.setValue(150)  # デフォルト0.2秒
        self.audio_interval_slider.valueChanged.connect(self.update_audio_interval)
        audio_settings_layout.addWidget(self.audio_interval_slider)
        self.audio_interval_label = QLabel("150 ms")
        audio_settings_layout.addWidget(self.audio_interval_label)
        color_layout.addLayout(audio_settings_layout)
        
        # 音声連動遷移時間設定を追加
        audio_transition_layout = QHBoxLayout()
        audio_transition_layout.addWidget(QLabel("音声連動遷移時間:"))
        self.audio_transition_slider = QSlider(Qt.Horizontal)
        self.audio_transition_slider.setRange(50, 300)  # 50msから300ms
        self.audio_transition_slider.setValue(100)  # デフォルト200ms
        self.audio_transition_slider.valueChanged.connect(self.update_audio_transition_time)
        audio_transition_layout.addWidget(self.audio_transition_slider)
        self.audio_transition_label = QLabel("100 ms")
        audio_transition_layout.addWidget(self.audio_transition_label)
        color_layout.addLayout(audio_transition_layout)
        
        # 自動モードのチェックボックスは非表示にする（ラジオボタンに置き換え）
        self.auto_mode_check = QCheckBox("自動色相変化モード")
        self.auto_mode_check.setVisible(False)
        
        color_group.setLayout(color_layout)
        left_layout.addWidget(color_group)
        
        # 色遷移設定
        transition_group = QGroupBox("色遷移設定")
        transition_layout = QVBoxLayout()
        
        # 遷移時間スライダー
        transition_time_layout = QHBoxLayout()
        transition_time_layout.addWidget(QLabel("遷移時間:"))
        self.transition_time_slider = QSlider(Qt.Horizontal)
        self.transition_time_slider.setRange(100, 5000)  # 0.1秒から5秒
        self.transition_time_slider.setValue(1000)       # デフォルト1秒
        self.transition_time_slider.setTickInterval(500)
        self.transition_time_slider.setTickPosition(QSlider.TicksBelow)
        self.transition_time_slider.valueChanged.connect(self.update_transition_time_label)
        transition_time_layout.addWidget(self.transition_time_slider)
        self.transition_time_label = QLabel("1000 ms")
        transition_time_layout.addWidget(self.transition_time_label)
        
        transition_layout.addLayout(transition_time_layout)
        
        # 遷移ボタン
        transition_btn_layout = QHBoxLayout()
        self.transition_left_btn = QPushButton("LEFT EARに遷移")
        self.transition_left_btn.clicked.connect(lambda: self.apply_transition("LEFT"))
        self.transition_left_btn.setEnabled(False)
        
        self.transition_right_btn = QPushButton("RIGHT EARに遷移")
        self.transition_right_btn.clicked.connect(lambda: self.apply_transition("RIGHT"))
        self.transition_right_btn.setEnabled(False)
        
        self.transition_both_btn = QPushButton("両方に遷移")
        self.transition_both_btn.clicked.connect(self.apply_transition_to_both)
        self.transition_both_btn.setEnabled(False)
        
        transition_btn_layout.addWidget(self.transition_left_btn)
        transition_btn_layout.addWidget(self.transition_right_btn)
        transition_btn_layout.addWidget(self.transition_both_btn)
        
        transition_layout.addLayout(transition_btn_layout)
        transition_group.setLayout(transition_layout)
        
        left_layout.addWidget(transition_group)
        
        # 適用ボタン
        apply_group = QGroupBox("設定適用")
        apply_layout = QHBoxLayout()
        
        self.apply_left_btn = QPushButton("LEFT EARに適用")
        self.apply_left_btn.setMinimumHeight(40)
        self.apply_left_btn.clicked.connect(lambda: self.apply_settings("LEFT"))
        self.apply_left_btn.setEnabled(False)
        
        self.apply_right_btn = QPushButton("RIGHT EARに適用")
        self.apply_right_btn.setMinimumHeight(40)
        self.apply_right_btn.clicked.connect(lambda: self.apply_settings("RIGHT"))
        self.apply_right_btn.setEnabled(False)
        
        self.apply_both_btn = QPushButton("両方に適用")
        self.apply_both_btn.setMinimumHeight(40)
        self.apply_both_btn.clicked.connect(self.apply_to_both)
        self.apply_both_btn.setEnabled(False)
        
        apply_layout.addWidget(self.apply_left_btn)
        apply_layout.addWidget(self.apply_right_btn)
        apply_layout.addWidget(self.apply_both_btn)
        
        apply_group.setLayout(apply_layout)
        left_layout.addWidget(apply_group)
        
        # ステータス表示
        status_layout = QHBoxLayout()
        self.status_label = QLabel("準備完了")
        status_layout.addWidget(self.status_label)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        status_layout.addWidget(self.progress_bar)
        
        left_layout.addLayout(status_layout)
        
        # 左ペインに余白を追加
        left_layout.addStretch(1)
        
        # ===== 右ペインのコンテンツ =====
        
        # アニメーション色設定パネル
        animation_color_group = QGroupBox("アニメーション色設定")
        animation_color_layout = QVBoxLayout()
        
        # 色設定テーブル
        self.animation_color_buttons = {}
        
        # アニメーションタイプと表示名の辞書
        animation_display_names = {
            "left_turn": "左ウィンカー",
            "right_turn": "右ウィンカー",
            "lane_change_left": "左車線変更",
            "lane_change_right": "右車線変更",
            "hazard": "ハザード",
            "thank_you": "サンキューハザード",
            "emergency": "緊急",
            "forward": "前進",
            "reverse": "後退"
        }
        
        # グリッドレイアウトで色設定を整理
        grid_layout = QGridLayout()
        row = 0
        
        for animation_type, display_name in animation_display_names.items():
            # ラベル
            name_label = QLabel(display_name)
            grid_layout.addWidget(name_label, row, 0)
            
            # 現在の設定色を取得
            current_color = self.led_animation.get_custom_color(animation_type)
            
            # 色ボタン
            color_btn = ColorPreviewButton(current_color)
            color_btn.color_changed.connect(lambda color, anim_type=animation_type: self.on_animation_color_changed(anim_type, color))
            self.animation_color_buttons[animation_type] = color_btn
            grid_layout.addWidget(color_btn, row, 1)
            
            # デフォルトに戻すボタン
            reset_btn = QPushButton("リセット")
            reset_btn.clicked.connect(lambda checked=False, anim_type=animation_type: self.reset_animation_color(anim_type))
            grid_layout.addWidget(reset_btn, row, 2)
            
            row += 1
        
        animation_color_layout.addLayout(grid_layout)
        
        # すべてのアニメーション色を現在の色に設定するボタン
        set_all_btn = QPushButton("すべてのアニメーション色を現在の色に設定")
        set_all_btn.clicked.connect(self.set_all_animation_colors)
        animation_color_layout.addWidget(set_all_btn)
        
        animation_color_group.setLayout(animation_color_layout)
        right_layout.addWidget(animation_color_group)
        
        # アニメーション後の色設定パネル（新規追加）
        after_animation_group = QGroupBox("アニメーション後の色設定")
        after_animation_layout = QVBoxLayout()
        
        # アニメーション後の色を使用するかどうかのチェックボックス
        self.use_after_color_check = QCheckBox("アニメーション後の色を使用する")
        self.use_after_color_check.setChecked(self.led_animation.is_using_after_animation_color())
        self.use_after_color_check.toggled.connect(self.on_use_after_color_toggled)
        after_animation_layout.addWidget(self.use_after_color_check)
        
        # 色設定部分
        after_color_layout = QHBoxLayout()
        after_color_layout.addWidget(QLabel("アニメーション後の色:"))
        
        # 現在の設定を取得
        current_after_color = self.led_animation.get_after_animation_color()
        
        # 色プレビュー
        self.after_color_preview = ColorPreviewWidget()
        self.after_color_preview.setMinimumHeight(40)
        self.after_color_preview.setColor(current_after_color)
        after_color_layout.addWidget(self.after_color_preview)
        
        after_animation_layout.addLayout(after_color_layout)
        
        # 色選択ボタン
        after_color_btn_layout = QHBoxLayout()
        self.after_color_picker_btn = QPushButton("アニメーション後の色を選択")
        self.after_color_picker_btn.clicked.connect(self.show_after_color_picker)
        self.after_color_picker_btn.setEnabled(self.led_animation.is_using_after_animation_color())
        after_color_btn_layout.addWidget(self.after_color_picker_btn)
        
        # 現在の色をアニメーション後の色に設定するボタン
        self.set_current_as_after_btn = QPushButton("現在の色をアニメーション後の色に設定")
        self.set_current_as_after_btn.clicked.connect(self.set_current_as_after_color)
        self.set_current_as_after_btn.setEnabled(self.led_animation.is_using_after_animation_color())
        after_color_btn_layout.addWidget(self.set_current_as_after_btn)
        
        after_animation_layout.addLayout(after_color_btn_layout)
        
        after_animation_group.setLayout(after_animation_layout)
        right_layout.addWidget(after_animation_group)
        
        # アニメーション制御部分
        animation_group = QGroupBox("ウィンカー・シグナル制御")
        animation_layout = QVBoxLayout()
        
        # ウィンカーコントロール
        turn_layout = QHBoxLayout()
        self.left_turn_btn = QPushButton("左ウィンカー")
        self.left_turn_btn.clicked.connect(lambda: self.start_animation("left_turn"))
        self.left_turn_btn.setMinimumHeight(40)
        
        self.hazard_btn = QPushButton("ハザード")
        self.hazard_btn.clicked.connect(lambda: self.start_animation("hazard"))
        self.hazard_btn.setMinimumHeight(40)
        
        self.right_turn_btn = QPushButton("右ウィンカー")
        self.right_turn_btn.clicked.connect(lambda: self.start_animation("right_turn"))
        self.right_turn_btn.setMinimumHeight(40)
        
        turn_layout.addWidget(self.left_turn_btn)
        turn_layout.addWidget(self.hazard_btn)
        turn_layout.addWidget(self.right_turn_btn)
        animation_layout.addLayout(turn_layout)
        
        # 車線変更コントロール
        lane_layout = QHBoxLayout()
        self.lane_left_btn = QPushButton("左車線変更")
        self.lane_left_btn.clicked.connect(lambda: self.start_animation("lane_change_left"))
        
        self.thank_you_btn = QPushButton("サンキューハザード")
        self.thank_you_btn.clicked.connect(lambda: self.start_animation("thank_you"))
        
        self.lane_right_btn = QPushButton("右車線変更")
        self.lane_right_btn.clicked.connect(lambda: self.start_animation("lane_change_right"))
        
        lane_layout.addWidget(self.lane_left_btn)
        lane_layout.addWidget(self.thank_you_btn)
        lane_layout.addWidget(self.lane_right_btn)
        animation_layout.addLayout(lane_layout)
        
        # 前進・後退コントロール
        move_layout = QHBoxLayout()
        self.forward_btn = QPushButton("前進")
        self.forward_btn.clicked.connect(lambda: self.start_animation("forward"))
        
        self.emergency_btn = QPushButton("緊急")
        self.emergency_btn.clicked.connect(lambda: self.start_animation("emergency"))
        self.emergency_btn.setStyleSheet("background-color: #ff6b6b;")
        
        self.reverse_btn = QPushButton("後退")
        self.reverse_btn.clicked.connect(lambda: self.start_animation("reverse"))
        
        move_layout.addWidget(self.forward_btn)
        move_layout.addWidget(self.emergency_btn)
        move_layout.addWidget(self.reverse_btn)
        animation_layout.addLayout(move_layout)
        
        # アニメーション停止ボタン
        stop_layout = QHBoxLayout()
        self.stop_animation_btn = QPushButton("アニメーション停止")
        self.stop_animation_btn.setMinimumHeight(40)
        self.stop_animation_btn.setStyleSheet("font-weight: bold;")
        self.stop_animation_btn.clicked.connect(self.stop_animation)
        self.stop_animation_btn.setEnabled(False)  # 初期状態は無効
        stop_layout.addWidget(self.stop_animation_btn)
        animation_layout.addLayout(stop_layout)
        
        # アニメーションステータス
        status_layout = QHBoxLayout()
        status_layout.addWidget(QLabel("現在のアニメーション:"))
        self.animation_status = QLabel("なし")
        self.animation_status.setStyleSheet("font-weight: bold;")
        status_layout.addWidget(self.animation_status)
        animation_layout.addLayout(status_layout)
        
        animation_group.setLayout(animation_layout)
        right_layout.addWidget(animation_group)
        
        # ログ表示部分
        log_group = QGroupBox("ログ")
        log_layout = QVBoxLayout()
        
        # LogTextEditクラスのインスタンスを使用
        self.log_text = LogTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setLineWrapMode(QTextEdit.NoWrap)
        log_layout.addWidget(self.log_text)
        
        log_btn_layout = QHBoxLayout()
        self.clear_log_btn = QPushButton("ログをクリア")
        self.clear_log_btn.clicked.connect(self.clear_log)
        log_btn_layout.addWidget(self.clear_log_btn)
        log_layout.addLayout(log_btn_layout)
        
        log_group.setLayout(log_layout)
        right_layout.addWidget(log_group)
        
        # 右ペインに余白を追加
        right_layout.addStretch(1)
        
        # 左右のペインをメインレイアウトに追加
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left_pane)
        splitter.addWidget(right_pane)
        
        # 初期分割比率を設定
        splitter.setSizes([500, 700])
        
        main_layout.addWidget(splitter)
        self.setCentralWidget(main_widget)
    
    # アニメーション色設定関連のメソッド
    def on_animation_color_changed(self, animation_type, color):
        """アニメーション用のカスタム色が変更されたときの処理"""
        self.led_animation.set_custom_color(animation_type, color)
        self.logger.info(f"{animation_type}のカスタム色を変更しました: R={color.red()}, G={color.green()}, B={color.blue()}")
    
    def reset_animation_color(self, animation_type):
        """アニメーションのカスタム色をデフォルトに戻す"""
        default_colors = {
            "left_turn": QColor(255, 191, 0),
            "right_turn": QColor(255, 191, 0),
            "lane_change_left": QColor(255, 191, 0),
            "lane_change_right": QColor(255, 191, 0),
            "hazard": QColor(255, 191, 0),
            "thank_you": QColor(255, 191, 0),
            "emergency": QColor(255, 0, 0),
            "forward": QColor(0, 0, 255),
            "reverse": QColor(255, 255, 255)
        }
        
        if animation_type in default_colors:
            default_color = default_colors[animation_type]
            self.led_animation.set_custom_color(animation_type, default_color)
            self.animation_color_buttons[animation_type].set_color(default_color)
            self.logger.info(f"{animation_type}のカスタム色をデフォルトに戻しました")
    
    def set_all_animation_colors(self):
        """すべてのアニメーション色を現在の色に設定"""
        current_color = self.current_color
        
        for animation_type, button in self.animation_color_buttons.items():
            button.set_color(current_color)
            self.led_animation.set_custom_color(animation_type, current_color)
        
        self.logger.info(f"すべてのアニメーション色を現在の色に設定しました: R={current_color.red()}, G={current_color.green()}, B={current_color.blue()}")
    
    # アニメーション後の色に関するメソッド
    def on_use_after_color_toggled(self, checked):
        """アニメーション後の色を使用するかどうかが変更されたときの処理"""
        self.led_animation.set_use_after_animation_color(checked)
        self.after_color_picker_btn.setEnabled(checked)
        self.set_current_as_after_btn.setEnabled(checked)
        
        # チェックが入ったときに、黒で見えないと困るので、白色を初期設定にする
        if checked and self.led_animation.get_after_animation_color() == QColor(0, 0, 0):
            # 初期値として白色を設定
            self.led_animation.set_after_animation_color(QColor(255, 255, 255))
            self.after_color_preview.setColor(QColor(255, 255, 255))
            self.logger.info("アニメーション後の色を白色に初期設定しました")
        
        # ログ出力
        status = "有効" if checked else "無効"
        self.logger.info(f"アニメーション後の色の使用を{status}にしました")
    
    def show_after_color_picker(self):
        """アニメーション後の色のカラーピッカーダイアログを表示"""
        current_color = self.led_animation.get_after_animation_color()
        color = QColorDialog.getColor(current_color, self, "アニメーション後の色を選択")
        if color.isValid():
            self.led_animation.set_after_animation_color(color)
            self.after_color_preview.setColor(color)
            self.logger.info(f"アニメーション後の色を変更しました: R={color.red()}, G={color.green()}, B={color.blue()}")
    
    def set_current_as_after_color(self):
        """現在の色をアニメーション後の色に設定"""
        self.led_animation.set_after_animation_color(self.current_color)
        self.after_color_preview.setColor(self.current_color)
        self.logger.info(f"現在の色をアニメーション後の色に設定しました: R={self.current_color.red()}, G={self.current_color.green()}, B={self.current_color.blue()}")
    
    # アニメーション関連メソッド
    def start_animation(self, animation_type):
        """指定されたアニメーションを開始する"""
        # 接続状態の確認
        if not (self.ble_controller.connected.get("LEFT", False) or 
                self.ble_controller.connected.get("RIGHT", False)):
            self.logger.warning("デバイスが接続されていません。アニメーションを開始できません。")
            QMessageBox.warning(self, "接続エラー", "少なくとも一つのデバイスを接続してください。")
            return
        
        # 音楽連動モードをオフにする
        if self.audio_mode:
            self.fixed_mode_radio.setChecked(True)
            self.audio_processor.stop()
            self.ble_controller.set_audio_mode(False)
            self.audio_mode = False
        
        # 処理中フラグを設定（二重実行防止）
        if hasattr(self, '_animation_transition_in_progress') and self._animation_transition_in_progress:
            self.logger.warning("アニメーション切り替え処理中です。しばらくお待ちください。")
            return
        
        # アニメーション実行中の確認と停止
        if self.led_animation.running:
            # 同じアニメーションが実行中の場合は停止するだけ
            if self.led_animation.current_animation == animation_type:
                self.logger.info(f"同じアニメーション({animation_type})が実行中のため停止します")
                self.stop_animation()
                return
            
            # 処理中フラグを設定
            self._animation_transition_in_progress = True
            
            # 異なるアニメーションが実行中の場合は一度停止してから新しいアニメーション開始
            self.logger.info(f"別のアニメーション({self.led_animation.current_animation})が実行中のため停止します")
            prev_animation = self.led_animation.current_animation
            
            # アニメーション停止
            self.led_animation.stop_animation()
            
            # UIを更新（停止状態に）
            self.reset_animation_buttons()
            self.animation_status.setText("切り替え中...")
            
            # アニメーション切り替え用タイマー設定
            # タイマーを使って十分な間隔を空ける（安全マージン）
            QTimer.singleShot(500, lambda: self._delayed_start_animation(animation_type, prev_animation))
            return
            
        # 通常のアニメーション開始（既存の処理）
        self._start_animation_impl(animation_type)
    
    def _delayed_start_animation(self, animation_type, prev_animation):
        """アニメーション停止後の遅延付き開始処理"""
        # コマンドキューの処理を待つ
        self.logger.info(f"{prev_animation}アニメーションの停止完了を待機しています...")
        
        # ここで処理中フラグをクリア
        self._animation_transition_in_progress = False
        
        # 新しいアニメーションを開始
        self.logger.info(f"新しいアニメーション({animation_type})を開始します")
        self._start_animation_impl(animation_type)
    
    def _start_animation_impl(self, animation_type):
        """アニメーション開始の実装部分"""
        # アニメーション開始前にUIを更新
        self.reset_animation_buttons()
        self.stop_animation_btn.setEnabled(True)
        
        # クリックされたボタンをハイライト
        if animation_type == "left_turn":
            self.left_turn_btn.setStyleSheet("background-color: #f0ad4e; font-weight: bold;")
        elif animation_type == "right_turn":
            self.right_turn_btn.setStyleSheet("background-color: #f0ad4e; font-weight: bold;")
        elif animation_type == "hazard":
            self.hazard_btn.setStyleSheet("background-color: #f0ad4e; font-weight: bold;")
        elif animation_type == "lane_change_left":
            self.lane_left_btn.setStyleSheet("background-color: #f0ad4e; font-weight: bold;")
        elif animation_type == "lane_change_right":
            self.lane_right_btn.setStyleSheet("background-color: #f0ad4e; font-weight: bold;")
        elif animation_type == "thank_you":
            self.thank_you_btn.setStyleSheet("background-color: #f0ad4e; font-weight: bold;")
        elif animation_type == "emergency":
            self.emergency_btn.setStyleSheet("background-color: #ff0000; color: white; font-weight: bold;")
        elif animation_type == "forward":
            self.forward_btn.setStyleSheet("background-color: #5bc0de; font-weight: bold;")
        elif animation_type == "reverse":
            self.reverse_btn.setStyleSheet("background-color: #5bc0de; font-weight: bold;")
        
        # アニメーション開始
        if not self.led_animation.start_animation(animation_type):
            self.logger.error(f"アニメーション({animation_type})の開始に失敗しました")
            self.reset_animation_buttons()
            self.stop_animation_btn.setEnabled(False)
            return
    
    def stop_animation(self):
        """実行中のアニメーションを停止する"""
        # 処理中フラグをクリア（アニメーション停止は常に可能にする）
        self._animation_transition_in_progress = False
        
        # アニメーションステータスを更新
        self.animation_status.setText("停止中...")
        
        # アニメーション停止
        self.led_animation.stop_animation()
        
        # UI更新
        self.stop_animation_btn.setEnabled(False)
        self.reset_animation_buttons()
        self.animation_status.setText("なし")
        
    def on_animation_started(self, animation_type):
        """アニメーション開始時のコールバック"""
        # 日本語の表示名に変換
        display_names = {
            "right_turn": "右ウィンカー",
            "left_turn": "左ウィンカー", 
            "lane_change_right": "右車線変更",
            "lane_change_left": "左車線変更",
            "hazard": "ハザード",
            "thank_you": "サンキューハザード",
            "emergency": "緊急",
            "forward": "前進",
            "reverse": "後退"
        }
        
        display_name = display_names.get(animation_type, animation_type)
        self.animation_status.setText(display_name)
        self.logger.info(f"{display_name}アニメーションを開始しました")
    
    def on_animation_stopped(self):
        """アニメーション停止時のコールバック"""
        self.stop_animation_btn.setEnabled(False)
        self.reset_animation_buttons()
        self.animation_status.setText("なし")
        self.logger.info("アニメーションを停止しました")
    
    def on_animation_status(self, message):
        """アニメーションステータスメッセージを処理"""
        self.status_label.setText(message)
        self.status_label.setStyleSheet("color: blue;")

    # 既存のメソッド
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
        # 元のコードを実行
        if (device_key == "LEFT"):
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
        
        # 遷移ボタンの状態も更新
        if device_key == "LEFT":
            self.transition_left_btn.setEnabled(connected)
        else:  # RIGHT
            self.transition_right_btn.setEnabled(connected)
        
        # 両方に遷移ボタンの状態を更新
        self.transition_both_btn.setEnabled(
            self.ble_controller.connected.get("LEFT", False) and 
            self.ble_controller.connected.get("RIGHT", False)
        )
        
        # アニメーション実行中の場合は停止
        if self.led_animation.running and not (
            self.ble_controller.connected.get("LEFT", False) or 
            self.ble_controller.connected.get("RIGHT", False)
        ):
            self.stop_animation()

    # その他の既存メソッド
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
            
            # 現在設定されている遷移時間を適用
            self.ble_controller.set_audio_transition_time(self.audio_transition_slider.value())
        
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
    
    def apply_transition(self, device_key):
        """遷移設定をデバイスに適用"""
        if not self.ble_controller.connected.get(device_key, False):
            self.logger.warning(f"{device_key}デバイスは接続されていません")
            return
        
        # 音楽連動モード中でも遷移コマンドは適用可能にする
        if self.audio_mode:
            self.logger.info(f"音楽連動モード中に{device_key}デバイスへ色遷移コマンドを適用します")
            # ステータスメッセージを変更する代わりに、処理を続行
        
        # ボタンを一時的に無効化
        btn = self.transition_left_btn if device_key == "LEFT" else self.transition_right_btn
        btn.setEnabled(False)
        
        # ステータス表示
        self.status_label.setText(f"{device_key}デバイスに色遷移を適用中...")
        self.status_label.setStyleSheet("color: blue;")
        
        # プログレスバーを表示
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)  # 不定のプログレス表示
        
        # 色の値を取得
        r, g, b = self.current_color.red(), self.current_color.green(), self.current_color.blue()
        
        # 遷移時間を取得
        transition_time = self.transition_time_slider.value()
        
        # 設定適用
        def on_transition_complete(success):
            btn.setEnabled(True)
            self.progress_bar.setVisible(False)
            
            if success:
                self.status_label.setText(f"{device_key}デバイスへの色遷移を開始しました（{transition_time}ms）")
                self.status_label.setStyleSheet("color: green;")
            else:
                self.status_label.setText(f"{device_key}デバイスへの色遷移開始に失敗しました")
                self.status_label.setStyleSheet("color: red;")
        
        # 色遷移コマンドを送信
        self.ble_controller.set_transition_color(device_key, r, g, b, transition_time, on_transition_complete)
    
    def apply_transition_to_both(self):
        """両方のデバイスに遷移設定を適用"""
        if not (self.ble_controller.connected.get("LEFT", False) and self.ble_controller.connected.get("RIGHT", False)):
            self.logger.warning("両方のデバイスが接続されていません")
            return
        
        # 音楽連動モード中でも遷移コマンドは適用可能にする
        if self.audio_mode:
            self.logger.info("音楽連動モード中に両方のデバイスへ色遷移コマンドを適用します")
            # ステータスメッセージを変更する代わりに、処理を続行
        
        # ボタンを一時的に無効化
        self.transition_both_btn.setEnabled(False)
        
        # ステータス表示
        self.status_label.setText("両方のデバイスに色遷移を適用中...")
        self.status_label.setStyleSheet("color: blue;")
        
        # プログレスバーを表示
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)  # 不定のプログレス表示
        
        # 色の値を取得
        r, g, b = self.current_color.red(), self.current_color.green(), self.current_color.blue()
        
        # 遷移時間を取得
        transition_time = self.transition_time_slider.value()
        
        # 接続済みのデバイスを確認
        connected_devices = []
        for device_key in ["LEFT", "RIGHT"]:
            if self.ble_controller.connected.get(device_key, False):
                connected_devices.append(device_key)
        
        # 同時に遷移コマンドを送信
        commands = []
        for device_key in connected_devices:
            commands.append((device_key, CMD_TRANSITION, (r, g, b, transition_time)))
        
        # 設定適用
        def on_both_complete(success):
            self.transition_both_btn.setEnabled(
                self.ble_controller.connected.get("LEFT", False) and 
                self.ble_controller.connected.get("RIGHT", False)
            )
            self.progress_bar.setVisible(False)
            
            if success:
                self.status_label.setText(f"両方のデバイスへの色遷移を開始しました（{transition_time}ms）")
                self.status_label.setStyleSheet("color: green;")
            else:
                self.status_label.setText("色遷移開始に一部失敗しました")
                self.status_label.setStyleSheet("color: orange;")
        
        # コマンド送信
        self.ble_controller._send_commands_simultaneously(commands, on_both_complete)
    
    def closeEvent(self, event):
        """アプリケーション終了時の処理"""
        self.logger.info("アプリケーションを終了します")
        
        # アニメーションを停止
        if hasattr(self, 'led_animation') and self.led_animation.running:
            self.led_animation.stop_animation()
        
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

    def update_audio_interval(self, value):
        """音声連動モードの更新間隔を更新"""
        self.audio_interval_label.setText(f"{value} ms")
        if hasattr(self, 'audio_processor'):
            self.audio_processor.update_interval = value

    def update_audio_transition_time(self, value):
        """音声連動モードの遷移時間を更新"""
        self.audio_transition_label.setText(f"{value} ms")
        if hasattr(self, 'ble_controller'):
            self.ble_controller.set_audio_transition_time(value)
    
    def update_transition_time_label(self, value):
        """遷移時間ラベルを更新"""
        self.transition_time_label.setText(f"{value} ms")

    def connect_both_devices(self):
        """両方のデバイスを同時に接続"""
        # 既に接続されているデバイスをチェック
        left_connected = self.ble_controller.connected.get("LEFT", False)
        right_connected = self.ble_controller.connected.get("RIGHT", False)
        
        if left_connected and right_connected:
            # 両方とも接続済みなら切断
            self.logger.info("両方のデバイスを切断します")
            self.ble_controller.disconnect("LEFT")
            self.ble_controller.disconnect("RIGHT")
            return
        
        # ボタンを無効化して接続中表示
        self.both_connect_btn.setEnabled(False)
        self.both_connect_btn.setText("接続中...")
        self.left_connect_btn.setEnabled(False)
        self.right_connect_btn.setEnabled(False)
        
        # プログレスバーを表示
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)  # 不定のプログレス表示
        
        self.logger.info("両方のデバイスの接続を開始します")
        
        # 未接続のデバイスを接続
        connect_tasks = []
        
        if not left_connected:
            left_future = self.ble_controller.scan_and_connect("LEFT")
            connect_tasks.append(left_future)
        
        if not right_connected:
            right_future = self.ble_controller.scan_and_connect("RIGHT")
            connect_tasks.append(right_future)
        
        # 接続完了時の処理
        def on_all_connect_done():
            self.both_connect_btn.setEnabled(True)
            self.both_connect_btn.setText("両方同時に接続")
            self.left_connect_btn.setEnabled(True)
            self.right_connect_btn.setEnabled(True)
            self.progress_bar.setVisible(False)
            
            # 接続結果確認
            left_success = self.ble_controller.connected.get("LEFT", False)
            right_success = self.ble_controller.connected.get("RIGHT", False)
            
            if left_success and right_success:
                self.logger.info("両方のデバイスの接続に成功しました")
                self.both_connect_btn.setText("両方切断")
            elif left_success:
                self.logger.warning("LEFT EARのみ接続しました。RIGHT EARの接続に失敗しました")
            elif right_success:
                self.logger.warning("RIGHT EARのみ接続しました。LEFT EARの接続に失敗しました")
            else:
                self.logger.error("両方のデバイスの接続に失敗しました")
        
        # すべてのタスクが完了したら通知
        pending_count = len(connect_tasks)
        
        def on_connect_done(future):
            nonlocal pending_count
            pending_count -= 1
            if pending_count <= 0:
                on_all_connect_done()
        
        # コールバックを設定
        for future in connect_tasks:
            future.add_done_callback(on_connect_done)
        
        # タスクがない場合（既に両方接続されている場合など）
        if not connect_tasks:
            on_all_connect_done()

    def reset_animation_buttons(self):
        """全てのアニメーションボタンのスタイルをリセット"""
        if hasattr(self, 'left_turn_btn'):
            self.left_turn_btn.setStyleSheet("")
        if hasattr(self, 'right_turn_btn'):
            self.right_turn_btn.setStyleSheet("")
        if hasattr(self, 'hazard_btn'):
            self.hazard_btn.setStyleSheet("")
        if hasattr(self, 'lane_left_btn'):
            self.lane_left_btn.setStyleSheet("")
        if hasattr(self, 'lane_right_btn'):
            self.lane_right_btn.setStyleSheet("")
        if hasattr(self, 'thank_you_btn'):
            self.thank_you_btn.setStyleSheet("")
        if hasattr(self, 'emergency_btn') and hasattr(self, 'emergency_btn.setStyleSheet'):
            self.emergency_btn.setStyleSheet("background-color: #ff6b6b;")
        if hasattr(self, 'forward_btn'):
            self.forward_btn.setStyleSheet("")
        if hasattr(self, 'reverse_btn'):
            self.reverse_btn.setStyleSheet("")
        
        self.logger.debug("アニメーションボタンのスタイルをリセットしました")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())