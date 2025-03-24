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

// LEDアレイの定義
CRGB leds[NUM_LEDS];

// グローバル変数
uint8_t gHue = 0; // 色相の変化用
bool autoHueChange = true; // 自動色相変化モード
bool deviceConnected = false;
bool oldDeviceConnected = false;
CRGB currentColor = CRGB::White; // 初期色は白

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
          Serial.printf("色を設定: R=%d, G=%d, B=%d\n", r, g, b);
        } 
        else if (value[0] == 'H' && value[1] == ':') {
          // 色相値を設定（例: H:128）
          int hue;
          sscanf(value.c_str(), "H:%d", &hue);
          gHue = hue;
          autoHueChange = false;
          Serial.printf("色相を設定: %d\n", hue);
        }
        else if (value[0] == 'M' && value[1] == ':') {
          // モード切替（例: M:1で自動色相変化、M:0で固定色）
          int mode;
          sscanf(value.c_str(), "M:%d", &mode);
          autoHueChange = (mode == 1);
          Serial.printf("モードを設定: %s\n", autoHueChange ? "自動色相変化" : "固定色");
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

  // 自動色相変化モードの場合
  if (autoHueChange) {
    EVERY_N_MILLISECONDS(20) { gHue++; } // 色相を緩やかに変化
    // すべてのLEDを同じ色にする
    fill_solid(leds, NUM_LEDS, CHSV(gHue, 255, 255));
  } else {
    // 固定色モードの場合
    if (currentColor.r == 0 && currentColor.g == 0 && currentColor.b == 0) {
      // 特定の色相の色を使用
      fill_solid(leds, NUM_LEDS, CHSV(gHue, 255, 255));
    } else {
      // RGB指定の色を使用
      fill_solid(leds, NUM_LEDS, currentColor);
    }
  }
  
  // LEDを更新
  FastLED.show();
  // フレームレートの調整
  FastLED.delay(1000/60); // 約60fps
}