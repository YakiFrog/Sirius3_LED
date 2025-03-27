import threading
import time
import logging
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor

class AnimationSignals(QObject):
    """アニメーション状態を通知するためのシグナル"""
    animation_started = Signal(str)  # アニメーション開始時（アニメーション名）
    animation_stopped = Signal()     # アニメーション停止時
    status_message = Signal(str)     # ステータスメッセージ

class LEDAnimation:
    """LEDアニメーションを管理するクラス"""
    
    def __init__(self, ble_controller):
        self.ble_controller = ble_controller
        self.running = False
        self.current_animation = None
        self.stop_event = threading.Event()
        self.signals = AnimationSignals()
        self.logger = logging.getLogger("sirius3.animation")
        
        # デフォルトの色とタイミング設定
        self.default_speed = 0.5        # 標準スピード（秒）
        self.fast_speed = 0.25          # 速いスピード（秒）
        self.slow_speed = 0.8           # 遅いスピード（秒）
        self.default_cycles = 6         # デフォルトの繰り返し回数
        self.default_transition = 300   # デフォルト遷移時間（ミリ秒）
        
        # 色の定義
        self.color_off = QColor(0, 0, 0)       # 消灯
        self.color_amber = QColor(255, 191, 0)  # アンバー色（ウィンカー）
        self.color_red = QColor(255, 0, 0)     # 赤（ブレーキ/緊急）
        self.color_white = QColor(255, 255, 255) # 白（バックランプ）
        self.color_blue = QColor(0, 0, 255)    # 青（特殊用途）
        
        # アニメーション用のカスタム色設定（ユーザーが変更可能）
        self.custom_colors = {
            "left_turn": QColor(255, 191, 0),      # 左ウィンカー
            "right_turn": QColor(255, 191, 0),     # 右ウィンカー
            "lane_change_left": QColor(255, 191, 0), # 左車線変更
            "lane_change_right": QColor(255, 191, 0), # 右車線変更
            "hazard": QColor(255, 191, 0),         # ハザード
            "thank_you": QColor(255, 191, 0),      # サンキューハザード
            "emergency": QColor(255, 0, 0),        # 緊急
            "forward": QColor(0, 0, 255),          # 前進
            "reverse": QColor(255, 255, 255)       # 後退
        }
        
        # アニメーション後の色設定（新規追加）
        self.after_animation_color = QColor(0, 0, 0)  # デフォルトは消灯（黒）
        self.use_after_animation_color = False        # アニメーション後の色を使用するかどうか
    
    def set_custom_color(self, animation_type, color):
        """アニメーション用のカスタム色を設定する"""
        if animation_type in self.custom_colors:
            self.custom_colors[animation_type] = color
            self.logger.debug(f"{animation_type}のカスタム色を設定: R={color.red()}, G={color.green()}, B={color.blue()}")
            return True
        return False
    
    def get_custom_color(self, animation_type):
        """アニメーション用のカスタム色を取得する"""
        return self.custom_colors.get(animation_type)
    
    def set_after_animation_color(self, color):
        """アニメーション後の色を設定する"""
        self.after_animation_color = color
        self.logger.debug(f"アニメーション後の色を設定: R={color.red()}, G={color.green()}, B={color.blue()}")
    
    def get_after_animation_color(self):
        """アニメーション後の色を取得する"""
        return self.after_animation_color
    
    def set_use_after_animation_color(self, use):
        """アニメーション後の色を使用するかどうかを設定する"""
        self.use_after_animation_color = use
        status = "有効" if use else "無効"
        self.logger.debug(f"アニメーション後の色の使用: {status}")
    
    def is_using_after_animation_color(self):
        """アニメーション後の色を使用するかどうかを取得する"""
        return self.use_after_animation_color
    
    def start_animation(self, animation_type, **kwargs):
        """指定されたアニメーションを開始する"""
        # アニメーションがすでに実行中の場合は先に停止する
        if self.running:
            self.logger.info(f"新しいアニメーション({animation_type})のため、実行中のアニメーションを停止します")
            # 現在のアニメーション名を一時保存
            previous_animation = self.current_animation
            # 停止処理
            self.stop_animation()
            # 確実に停止処理が完了するまで少し待機
            time.sleep(0.1)
            # 停止していない場合は強制的にフラグをリセット
            if self.running:
                self.logger.warning("前のアニメーションの停止処理が完了していないため強制的に停止します")
                self.running = False
                self.stop_event.set()
    
        # 新しいアニメーションの開始
        self.running = True
        self.current_animation = animation_type
        self.stop_event.clear()
        
        self.signals.animation_started.emit(animation_type)
        self.signals.status_message.emit(f"{animation_type}アニメーションを開始しました")
        self.logger.info(f"アニメーション開始: {animation_type}")
        
        # アニメーションタイプに応じて処理を分岐
        if animation_type == "right_turn":
            threading.Thread(target=self._turn_signal_animation, 
                          args=("RIGHT",), kwargs=kwargs, daemon=True).start()
        
        elif animation_type == "left_turn":
            threading.Thread(target=self._turn_signal_animation, 
                          args=("LEFT",), kwargs=kwargs, daemon=True).start()
        
        elif animation_type == "lane_change_right":
            kwargs['cycles'] = kwargs.get('cycles', 3)  # 車線変更は3回点滅がデフォルト
            threading.Thread(target=self._turn_signal_animation, 
                          args=("RIGHT",), kwargs=kwargs, daemon=True).start()
        
        elif animation_type == "lane_change_left":
            kwargs['cycles'] = kwargs.get('cycles', 3)  # 車線変更は3回点滅がデフォルト
            threading.Thread(target=self._turn_signal_animation, 
                          args=("LEFT",), kwargs=kwargs, daemon=True).start()
        
        elif animation_type == "hazard":
            threading.Thread(target=self._hazard_animation, kwargs=kwargs, daemon=True).start()
        
        elif animation_type == "thank_you":
            kwargs['cycles'] = kwargs.get('cycles', 3)  # サンキューハザードは3回点滅
            threading.Thread(target=self._hazard_animation, kwargs=kwargs, daemon=True).start()
        
        elif animation_type == "emergency":
            threading.Thread(target=self._emergency_animation, kwargs=kwargs, daemon=True).start()
        
        elif animation_type == "forward":
            threading.Thread(target=self._move_animation, 
                          args=("forward",), kwargs=kwargs, daemon=True).start()
        
        elif animation_type == "reverse":
            threading.Thread(target=self._move_animation, 
                          args=("reverse",), kwargs=kwargs, daemon=True).start()
        
        else:
            self.logger.warning(f"未知のアニメーションタイプ: {animation_type}")
            self.running = False
            return False
        
        return True
    
    def stop_animation(self):
        """実行中のアニメーションを停止する"""
        if not self.running:
            return
            
        self.stop_event.set()
        self.running = False
        self.logger.info("アニメーションを停止しました")
        self.signals.animation_stopped.emit()
        self.signals.status_message.emit("アニメーションを停止しました")
        
        # すべてのデバイスにコマンドを送信するためにリストを作成
        devices_to_update = []
        for device_key in ["LEFT", "RIGHT"]:
            if self.ble_controller.connected.get(device_key, False):
                devices_to_update.append(device_key)
        
        if not devices_to_update:
            self.logger.warning("接続されているデバイスがありません。アニメーション後の色を設定できません。")
            return
        
        self.logger.info(f"アニメーション後の色設定を適用します。接続デバイス: {', '.join(devices_to_update)}")
        
        # コマンドを複数のデバイスに同時に送信するための準備
        color_commands = []
        mode_commands = []
        
        # アニメーション後の色設定に基づいてコマンドを準備
        self.logger.debug(f"アニメーション後の色の使用設定: {self.use_after_animation_color}")
        
        if self.use_after_animation_color:
            # アニメーション後の色を使用する場合
            r, g, b = self.after_animation_color.red(), self.after_animation_color.green(), self.after_animation_color.blue()
            self.logger.info(f"アニメーション後の色を適用: R={r}, G={g}, B={b}")
            
            # 各デバイスに適用するコマンドを追加
            for device_key in devices_to_update:
                # まず色設定コマンドを用意
                color_commands.append((device_key, "C", (r, g, b)))
                # 次にモード設定コマンドを用意（固定色モード=0）
                mode_commands.append((device_key, "M", 0))
        else:
            # 使用しない場合は消灯（黒だとArduino側で特別扱いされる可能性があるため非常に暗い色を使用）
            self.logger.info("アニメーション後の色を使用しないため消灯します")
            for device_key in devices_to_update:
                # まず色設定コマンドを用意（非常に暗い色）
                color_commands.append((device_key, "C", (1, 1, 1)))
                # 次にモード設定コマンドを用意（固定色モード=0）
                mode_commands.append((device_key, "M", 0))
        
        # まず色設定コマンドを送信
        if color_commands:
            self.logger.debug(f"色設定コマンドを送信: {color_commands}")
            self.ble_controller._send_commands_simultaneously(color_commands)
            # コマンド間に少し間隔を空ける
            time.sleep(0.1)
        
        # 次にモード設定コマンドを送信
        if mode_commands:
            self.logger.debug(f"モード設定コマンドを送信: {mode_commands}")
            self.ble_controller._send_commands_simultaneously(mode_commands)
        
        self.logger.info("アニメーション後の設定適用完了")
    
    def _turn_signal_animation(self, side, speed=None, cycles=None, transition_time=None):
        """ウィンカーアニメーション（右折/左折/車線変更）
        
        Args:
            side: "LEFT" または "RIGHT"
            speed: 点滅の間隔（秒）
            cycles: 点滅の回数
            transition_time: 色の遷移時間（ミリ秒）
        """
        speed = speed or self.default_speed
        cycles = cycles or self.default_cycles
        transition_time = transition_time or self.default_transition
        
        # 点滅させるデバイス
        target_device = side
        
        # デバイスが接続されているか確認
        if not self.ble_controller.connected.get(target_device, False):
            self.logger.warning(f"{target_device}デバイスが接続されていません")
            self.signals.status_message.emit(f"{target_device}デバイスが接続されていません")
            self.running = False
            return
            
        try:
            # アニメーションタイプに合わせたカスタム色を取得
            animation_type = "left_turn" if side == "LEFT" else "right_turn"
            if self.current_animation:
                animation_type = self.current_animation
            
            # カスタム色がある場合はそれを使用、なければデフォルト
            color = self.custom_colors.get(animation_type, self.color_amber)
            r, g, b = color.red(), color.green(), color.blue()
            
            # 反対側のデバイスを特定
            opposite_device = "RIGHT" if side == "LEFT" else "LEFT"
            opposite_connected = self.ble_controller.connected.get(opposite_device, False)
            
            # 反対側のデバイスが接続されている場合、消灯状態にする
            if opposite_connected:
                self.ble_controller.set_rgb_color(opposite_device, 1, 1, 1)  # 非常に暗い色で事実上消灯
                self.ble_controller.set_mode(opposite_device, False)  # 固定色モードに設定
                self.logger.debug(f"{opposite_device}デバイスを消灯状態に設定")
            
            count = 0
            while not self.stop_event.is_set() and count < cycles:
                # 点灯
                self.ble_controller.set_transition_color(
                    target_device, r, g, b, transition_time)
                
                # 点灯状態を保持
                time.sleep(speed)
                
                if self.stop_event.is_set():
                    break
                
                # 消灯
                self.ble_controller.set_transition_color(
                    target_device, 0, 0, 0, transition_time)
                
                # 消灯状態を保持
                time.sleep(speed)
                
                count += 1
                
            # アニメーション終了、消灯状態に
            if not self.stop_event.is_set():
                # 両方のデバイスに対して消灯コマンドを送信
                commands = []
                for device_key in ["LEFT", "RIGHT"]:
                    if self.ble_controller.connected.get(device_key, False):
                        commands.append((device_key, "C", (1, 1, 1)))  # 非常に暗い色
                
                if commands:
                    self.ble_controller._send_commands_simultaneously(commands)
                    self.logger.debug("アニメーション終了時に両方のデバイスを消灯状態に設定")
                
                self.running = False
                self.signals.animation_stopped.emit()
                
        except Exception as e:
            self.logger.error(f"ウィンカーアニメーション実行中にエラー: {str(e)}")
            self.running = False
    
    def _hazard_animation(self, speed=None, cycles=None, transition_time=None):
        """ハザードランプアニメーション（両方同時点滅）
        
        Args:
            speed: 点滅の間隔（秒）
            cycles: 点滅の回数
            transition_time: 色の遷移時間（ミリ秒）
        """
        speed = speed or self.default_speed
        cycles = cycles or self.default_cycles
        transition_time = transition_time or self.default_transition
        
        # 両方のデバイスが接続されているか確認
        left_connected = self.ble_controller.connected.get("LEFT", False)
        right_connected = self.ble_controller.connected.get("RIGHT", False)
        
        if not (left_connected or right_connected):
            self.logger.warning("デバイスが接続されていません")
            self.signals.status_message.emit("デバイスが接続されていません")
            self.running = False
            return
            
        try:
            # カスタム色を取得（サンキューハザードかハザードか）
            animation_type = self.current_animation or "hazard"
            color = self.custom_colors.get(animation_type, self.color_amber)
            r, g, b = color.red(), color.green(), color.blue()
            
            count = 0
            while not self.stop_event.is_set() and count < cycles:
                # 両方点灯
                commands = []
                if left_connected:
                    commands.append(("LEFT", "T", (r, g, b, transition_time)))
                if right_connected:
                    commands.append(("RIGHT", "T", (r, g, b, transition_time)))
                
                self.ble_controller._send_commands_simultaneously(commands)
                
                # 点灯状態を保持
                time.sleep(speed)
                
                if self.stop_event.is_set():
                    break
                
                # 両方消灯
                commands = []
                if left_connected:
                    commands.append(("LEFT", "T", (0, 0, 0, transition_time)))
                if right_connected:
                    commands.append(("RIGHT", "T", (0, 0, 0, transition_time)))
                
                self.ble_controller._send_commands_simultaneously(commands)
                
                # 消灯状態を保持
                time.sleep(speed)
                
                count += 1
                
            # アニメーション終了、消灯状態に
            if not self.stop_event.is_set():
                # 両方のデバイスに対して消灯コマンドを送信（元のコードと同様）
                commands = []
                if left_connected:
                    commands.append(("LEFT", "C", (1, 1, 1)))  # 非常に暗い色
                if right_connected:
                    commands.append(("RIGHT", "C", (1, 1, 1)))  # 非常に暗い色
                
                if commands:
                    self.ble_controller._send_commands_simultaneously(commands)
                    self.logger.debug("アニメーション終了時に両方のデバイスを消灯状態に設定")
                
                self.running = False
                self.signals.animation_stopped.emit()
                
        except Exception as e:
            self.logger.error(f"ハザードアニメーション実行中にエラー: {str(e)}")
            self.running = False
    
    def _emergency_animation(self, speed=None, cycles=None, transition_time=None):
        """緊急時アニメーション（赤色で速く点滅）
        
        Args:
            speed: 点滅の間隔（秒）
            cycles: 点滅の回数
            transition_time: 色の遷移時間（ミリ秒）
        """
        speed = speed or self.fast_speed  # 緊急時は速い点滅
        cycles = cycles or self.default_cycles * 2  # 回数を多く
        transition_time = transition_time or int(self.default_transition / 2)  # 遷移も速く
        
        # 両方のデバイスが接続されているか確認
        left_connected = self.ble_controller.connected.get("LEFT", False)
        right_connected = self.ble_controller.connected.get("RIGHT", False)
        
        if not (left_connected or right_connected):
            self.logger.warning("デバイスが接続されていません")
            self.signals.status_message.emit("デバイスが接続されていません")
            self.running = False
            return
            
        try:
            # カスタム色を取得
            color = self.custom_colors.get("emergency", self.color_red)
            r, g, b = color.red(), color.green(), color.blue()
            
            count = 0
            while not self.stop_event.is_set() and count < cycles:
                # 両方点灯
                commands = []
                if left_connected:
                    commands.append(("LEFT", "T", (r, g, b, transition_time)))
                if right_connected:
                    commands.append(("RIGHT", "T", (r, g, b, transition_time)))
                
                self.ble_controller._send_commands_simultaneously(commands)
                
                # 点灯状態を保持
                time.sleep(speed)
                
                if self.stop_event.is_set():
                    break
                
                # 両方消灯
                commands = []
                if left_connected:
                    commands.append(("LEFT", "T", (0, 0, 0, transition_time)))
                if right_connected:
                    commands.append(("RIGHT", "T", (0, 0, 0, transition_time)))
                
                self.ble_controller._send_commands_simultaneously(commands)
                
                # 消灯状態を保持
                time.sleep(speed)
                
                count += 1
                
            # アニメーション終了、消灯状態に
            if not self.stop_event.is_set():
                # 両方のデバイスに対して消灯コマンドを送信（元のコードと同様）
                commands = []
                if left_connected:
                    commands.append(("LEFT", "C", (1, 1, 1)))  # 非常に暗い色
                if right_connected:
                    commands.append(("RIGHT", "C", (1, 1, 1)))  # 非常に暗い色
                
                if commands:
                    self.ble_controller._send_commands_simultaneously(commands)
                    self.logger.debug("アニメーション終了時に両方のデバイスを消灯状態に設定")
                
                self.running = False
                self.signals.animation_stopped.emit()
                
        except Exception as e:
            self.logger.error(f"緊急アニメーション実行中にエラー: {str(e)}")
            self.running = False
    
    def _move_animation(self, direction, speed=None, transition_time=None):
        """移動アニメーション（発進/後退）
        
        Args:
            direction: "forward" または "reverse"
            speed: アニメーションの速度（秒）
            transition_time: 色の遷移時間（ミリ秒）
        """
        speed = speed or self.slow_speed
        transition_time = transition_time or self.default_transition
        
        # 両方のデバイスが接続されているか確認
        left_connected = self.ble_controller.connected.get("LEFT", False)
        right_connected = self.ble_controller.connected.get("RIGHT", False)
        
        if not (left_connected or right_connected):
            self.logger.warning("デバイスが接続されていません")
            self.signals.status_message.emit("デバイスが接続されていません")
            self.running = False
            return
        
        try:
            # カスタム色を取得
            animation_type = direction  # "forward" または "reverse"
            if direction == "forward":
                color = self.custom_colors.get("forward", self.color_blue)
            else:  # reverse
                color = self.custom_colors.get("reverse", self.color_white)
                
            r, g, b = color.red(), color.green(), color.blue()
            
            # 一回のアニメーション（フェードイン・フェードアウト）
            # フェードイン
            commands = []
            if left_connected:
                commands.append(("LEFT", "T", (r, g, b, transition_time * 2)))
            if right_connected:
                commands.append(("RIGHT", "T", (r, g, b, transition_time * 2)))
            
            self.ble_controller._send_commands_simultaneously(commands)
            
            # フェードイン待機
            time.sleep(speed * 2)
            
            if self.stop_event.is_set():
                return
            
            # フェードアウト
            commands = []
            if left_connected:
                commands.append(("LEFT", "T", (0, 0, 0, transition_time * 3)))
            if right_connected:
                commands.append(("RIGHT", "T", (0, 0, 0, transition_time * 3)))
            
            self.ble_controller._send_commands_simultaneously(commands)
            
            # フェードアウト待機
            time.sleep(speed * 3)
            
            # アニメーション終了
            if not self.stop_event.is_set():
                # 両方のデバイスに対して消灯コマンドを送信
                commands = []
                if left_connected:
                    commands.append(("LEFT", "C", (1, 1, 1)))  # 非常に暗い色
                if right_connected:
                    commands.append(("RIGHT", "C", (1, 1, 1)))  # 非常に暗い色
                
                if commands:
                    self.ble_controller._send_commands_simultaneously(commands)
                    self.logger.debug("アニメーション終了時に両方のデバイスを消灯状態に設定")
                
                self.running = False
                self.signals.animation_stopped.emit()
                
        except Exception as e:
            self.logger.error(f"移動アニメーション実行中にエラー: {str(e)}")
            self.running = False
