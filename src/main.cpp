#include <Arduino.h>
#include <FastLED.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

// 各基板で変更するデバイスID（基板1には1、基板2には2を設定）
#define DEVICE_ID 1

// デバイス固有の設定
#if DEVICE_ID == 1
  #define DEVICE_NAME "Sirius3_LEFT_EAR"
  // 基板1固有の他の設定があれば追加
#elif DEVICE_ID == 2
  #define DEVICE_NAME "Sirius3_RIGHT_EAR"
  // 基板2固有の他の設定があれば追加
#else
  #error "DEVICE_ID must be set to 1 or 2"
#endif

// LEDの設定
#define LED_PIN     D10      // データピン
#define NUM_LEDS    int(4*12)     // LEDの数
#define LED_TYPE    WS2812B  // LEDの種類
#define COLOR_ORDER GRB    // カラー順序
#define BRIGHTNESS  255    // 明るさ (0-255)

// BLE設定
#define SERVICE_UUID        "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
#define CHARACTERISTIC_UUID "beb5483e-36e1-4688-b7f5-ea07361b26a8"

// 色遷移のデフォルト時間（ミリ秒）
#define DEFAULT_TRANSITION_TIME 1000

// 色設定モードの定義
enum ColorMode {
  MODE_AUTO,      // 自動色相変化モード
  MODE_FIXED,     // 固定色モード（C:コマンド）
  MODE_TRANSITION // 遷移モード（T:コマンド）
};

// LEDアレイの定義
CRGB leds[NUM_LEDS];

// グローバル変数
uint8_t gHue = 0; // 色相の変化用
bool autoHueChange = true; // 自動色相変化モード
bool deviceConnected = false;
bool oldDeviceConnected = false;
CRGB currentColor = CRGB::White; // 初期色は白
ColorMode colorMode = MODE_AUTO; // 初期モードは自動色相変化

// 色遷移関連の変数
bool isTransitioning = false; // 色遷移中かどうか
CRGB startColor;             // 遷移開始色
CRGB targetColor;            // 遷移目標色
unsigned long transitionStartTime = 0; // 遷移開始時刻
unsigned long transitionDuration = DEFAULT_TRANSITION_TIME; // 遷移時間

BLEServer* pServer = NULL;
BLECharacteristic* pCharacteristic = NULL;

// BLE接続状態のコールバッククラス
class MyServerCallbacks: public BLEServerCallbacks {
    void onConnect(BLEServer* pServer) {
      deviceConnected = true;
      Serial.println("デバイスが接続されました");
    }

    void onDisconnect(BLEServer* pServer) {
      deviceConnected = false;
      Serial.println("デバイスが切断されました");
    }
};

// BLEからのデータ受信コールバッククラス
class MyCallbacks: public BLECharacteristicCallbacks {
    void onWrite(BLECharacteristic *pCharacteristic) {
      String arduinoValue = pCharacteristic->getValue();
      std::string value(arduinoValue.c_str(), arduinoValue.length());
      if (value.length() > 0) {
        Serial.print("受信データ: ");
        Serial.println(value.c_str());
        
        // コマンドの処理
        if (value[0] == 'C' && value[1] == ':') {
          // RGB値で色を設定（例: C:255,0,0）
          int r, g, b;
          sscanf(value.c_str(), "C:%d,%d,%d", &r, &g, &b);
          currentColor = CRGB(r, g, b);
          autoHueChange = false;
          isTransitioning = false; // 即時変更なので遷移をキャンセル
          colorMode = MODE_FIXED;  // 固定色モードに設定
          Serial.printf("色を設定: R=%d, G=%d, B=%d\n", r, g, b);
        } 
        else if (value[0] == 'H' && value[1] == ':') {
          // 色相値を設定（例: H:128）
          int hue;
          sscanf(value.c_str(), "H:%d", &hue);
          gHue = hue;
          autoHueChange = false;
          isTransitioning = false; // 即時変更なので遷移をキャンセル
          colorMode = MODE_FIXED;  // 固定色モードに設定（H:は固定色の一種）
          Serial.printf("色相を設定: %d\n", hue);
        }
        else if (value[0] == 'M' && value[1] == ':') {
          // モード切替（例: M:1で自動色相変化、M:0で固定色）
          int mode;
          sscanf(value.c_str(), "M:%d", &mode);
          autoHueChange = (mode == 1);
          isTransitioning = false; // モード変更時は遷移をキャンセル
          colorMode = autoHueChange ? MODE_AUTO : MODE_FIXED;  // 自動モードと固定モードを切り替え
          Serial.printf("モードを設定: %s\n", autoHueChange ? "自動色相変化" : "固定色");
        }
        else if (value[0] == 'T' && value[1] == ':') {
          // 色遷移コマンド（例: T:255,0,0,2000）
          // T:R,G,B,TIME で、現在の色から指定色に TIME ミリ秒かけて遷移
          int r, g, b, time = DEFAULT_TRANSITION_TIME;
          int parsed = sscanf(value.c_str(), "T:%d,%d,%d,%d", &r, &g, &b, &time);
          
          // 必須のRGB値が解析できたか確認
          if (parsed >= 3) {
            // 遷移パラメータを設定 - 常に現在の色から開始（遷移中でも）
            startColor = currentColor; // 現在の色を開始色に（遷移中の色も含む）
            targetColor = CRGB(r, g, b); // 目標色を設定
            transitionDuration = (parsed == 4) ? time : DEFAULT_TRANSITION_TIME; // 時間が省略されていればデフォルト値を使用
            transitionStartTime = millis(); // 現在時刻を記録
            isTransitioning = true; // 遷移モードを有効に
            autoHueChange = false; // 自動色相変化を無効に
            colorMode = MODE_TRANSITION;  // 遷移モードに設定
            
            if (startColor.r == targetColor.r && startColor.g == targetColor.g && startColor.b == targetColor.b) {
              // 開始色と目標色が同じ場合は遷移不要
              isTransitioning = false;
              Serial.println("開始色と目標色が同じため、遷移はスキップされます");
            } else {
              Serial.printf("色遷移開始: 現在色(R=%d,G=%d,B=%d)から目標色(R=%d,G=%d,B=%d)へ %dミリ秒で遷移\n", 
                         startColor.r, startColor.g, startColor.b,
                         targetColor.r, targetColor.g, targetColor.b,
                         transitionDuration);
            }
          }
        }
      }
    }
};

void setup() {
  // FastLEDの初期化
  FastLED.addLeds<LED_TYPE, LED_PIN, COLOR_ORDER>(leds, NUM_LEDS).setCorrection(TypicalLEDStrip);
  FastLED.setBrightness(BRIGHTNESS);
  
  // デバッグ用シリアル通信の開始
  Serial.begin(115200);
  Serial.println("RGB LEDテープ制御プログラム起動");

  // BLEの初期化
  BLEDevice::init(DEVICE_NAME);
  esp_ble_tx_power_set(ESP_BLE_PWR_TYPE_DEFAULT, ESP_PWR_LVL_P9); // 出力パワーを最大(+9dBm)に設定
  esp_ble_tx_power_set(ESP_BLE_PWR_TYPE_ADV, ESP_PWR_LVL_P9);     // アドバタイジングの出力も最大に
  pServer = BLEDevice::createServer();  // ここでGATTServerを作成
  pServer->setCallbacks(new MyServerCallbacks());
  
  BLEService *pService = pServer->createService(SERVICE_UUID);  // サービス作成
  
  pCharacteristic = pService->createCharacteristic(
                      CHARACTERISTIC_UUID,
                      BLECharacteristic::PROPERTY_READ |
                      BLECharacteristic::PROPERTY_WRITE |
                      BLECharacteristic::PROPERTY_NOTIFY
                    );
  
  pCharacteristic->setCallbacks(new MyCallbacks());
  pCharacteristic->addDescriptor(new BLE2902());
  
  pService->start();
  
  BLEAdvertising *pAdvertising = pServer->getAdvertising();
  pAdvertising->start();
  Serial.println("BLEサーバーが起動しました");
}

void loop() {
  // BLE接続管理
  if (deviceConnected != oldDeviceConnected) {
    if (deviceConnected) {
      Serial.println("BLE接続開始");
    } else {
      Serial.println("BLE接続終了");
      delay(500); // 接続終了を安定させるため
      pServer->startAdvertising(); // 再度アドバタイズを開始
      Serial.println("BLEアドバタイズを再開");
    }
    oldDeviceConnected = deviceConnected;
  }

  // 色遷移処理
  if (isTransitioning) {
    unsigned long currentTime = millis();
    unsigned long elapsedTime = currentTime - transitionStartTime;
    
    if (elapsedTime >= transitionDuration) {
      // 遷移完了
      currentColor = targetColor;
      isTransitioning = false;
      // ここでモードは変更しない（colorMode = MODE_TRANSITIONのまま）
      Serial.println("色遷移完了");
    } else {
      // 遷移中
      float progress = (float)elapsedTime / transitionDuration; // 0.0 から 1.0 の進行度
      
      // 線形補間で現在の色を計算
      currentColor.r = startColor.r + (targetColor.r - startColor.r) * progress;
      currentColor.g = startColor.g + (targetColor.g - startColor.g) * progress;
      currentColor.b = startColor.b + (targetColor.b - startColor.b) * progress;
    }
    
    // 遷移中の色で全LEDを設定
    fill_solid(leds, NUM_LEDS, currentColor);
  }
  // モードに応じたLED制御
  else if (colorMode == MODE_AUTO) {
    // 自動色相変化モード
    EVERY_N_MILLISECONDS(20) { gHue++; } // 色相を緩やかに変化
    fill_solid(leds, NUM_LEDS, CHSV(gHue, 255, 255));
  } 
  else if (colorMode == MODE_FIXED || colorMode == MODE_TRANSITION) {
    // 固定色モードまたは遷移完了後（現在のcolorModeを維持）
    // 色相による色の使用は廃止し、常に指定されたRGB値を使用する
    fill_solid(leds, NUM_LEDS, currentColor);
  }
  
  // LEDを更新
  FastLED.show();
  // フレームレートの調整tLED.delay(1000/60); // 約60fps
  FastLED.delay(1000/60); // 約60fps
}