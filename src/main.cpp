#include <Arduino.h>
#include <FastLED.h>

// LEDの設定
#define LED_PIN     D10      // データピン
#define NUM_LEDS    144     // LEDの数
#define LED_TYPE    WS2812B  // LEDの種類
#define COLOR_ORDER GRB    // カラー順序
#define BRIGHTNESS  255    // 明るさ (0-255)

// LEDアレイの定義
CRGB leds[NUM_LEDS];

// グローバル変数
uint8_t gHue = 0; // 色相の変化用

void setup() {
  // FastLEDの初期化
  FastLED.addLeds<LED_TYPE, LED_PIN, COLOR_ORDER>(leds, NUM_LEDS).setCorrection(TypicalLEDStrip);
  FastLED.setBrightness(BRIGHTNESS);
  
  // デバッグ用シリアル通信の開始
  Serial.begin(115200);
  Serial.println("RGB LEDテープ制御プログラム起動");
}

// 虹色のエフェクトを表示する関数
void rainbow() {
  // すべてのLEDに対して、色相を少しずつずらして虹色を表現
  fill_rainbow(leds, NUM_LEDS, gHue, 7);
}

// 流れるエフェクトを表示する関数
void sinelon() {
  // すべてのLEDを薄暗くする
  fadeToBlackBy(leds, NUM_LEDS, 20);
  // サイン波を使って位置を計算し、1つのLEDを点灯させる
  int pos = beatsin16(13, 0, NUM_LEDS-1);
  leds[pos] += CHSV(gHue, 255, 192);
}

// 明滅するエフェクトを表示する関数
void bpm() {
  // bpmベースのビートに合わせて色と明るさを変化させる
  uint8_t beat = beatsin8(62, 64, 255);
  for(int i = 0; i < NUM_LEDS; i++) {
    leds[i] = ColorFromPalette(PartyColors_p, gHue+(i*2), beat-gHue+(i*10));
  }
}

// 色の点滅エフェクトを表示する関数
void juggle() {
  // 8つのドットがジャグリングするように動く
  fadeToBlackBy(leds, NUM_LEDS, 20);
  uint8_t dothue = 0;
  for(int i = 0; i < 8; i++) {
    leds[beatsin16(i+7, 0, NUM_LEDS-1)] |= CHSV(dothue, 200, 255);
    dothue += 32;
  }
}

void loop() {
  // 時間経過で色相を変化させる
  EVERY_N_MILLISECONDS(20) { gHue++; } // 色相を緩やかに変化

  // エフェクトを切り替える (各エフェクトを10秒ずつ)
  EVERY_N_SECONDS(10) {
    static uint8_t currentEffect = 0;
    currentEffect = (currentEffect + 1) % 4;  // 4種類のエフェクト
    Serial.print("エフェクト変更: ");
    Serial.println(currentEffect);
  }

  // 現在のエフェクトによって関数を実行
  static uint8_t currentEffect = 0;
  switch(currentEffect) {
    case 0: rainbow(); break;
    case 1: sinelon(); break;
    case 2: bpm(); break;
    case 3: juggle(); break;
  }

  // LEDを更新
  FastLED.show();
  // フレームレートの調整
  FastLED.delay(1000/60); // 約60fps
}