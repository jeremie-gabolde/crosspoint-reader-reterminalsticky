#pragma once

#include <BoardConfig.h>

#if FREEINK_CAP_BUZZER && !CROSSPOINT_EMULATED
#include <Buzzer.h>
#include <esp_timer.h>
#endif

// Application-owned button beep; the SDK Buzzer owns the LEDC tone output.
class HalBuzzer {
 public:
  static void beep(bool enabled) {
#if FREEINK_CAP_BUZZER && !CROSSPOINT_EMULATED
    if (!enabled) return;
    static Buzzer buzzer;
    // Lazy init: with the option off, no LEDC channel is attached.
    if (!buzzer.begin()) return;
    // Buzzer::tone(freq, ms) blocks in delay(); start the tone and let a
    // one-shot timer silence it so input polling is not stalled.
    static esp_timer_handle_t stopTimer = nullptr;
    if (!stopTimer) {
      esp_timer_create_args_t args{};
      args.callback = [](void* b) { static_cast<Buzzer*>(b)->noTone(); };
      args.arg = &buzzer;
      args.name = "buzzer_stop";
      if (esp_timer_create(&args, &stopTimer) != ESP_OK) return;
    }
    esp_timer_stop(stopTimer);
    buzzer.tone(kBeepHz, 0);
    esp_timer_start_once(stopTimer, kBeepMs * 1000ULL);
#else
    (void)enabled;
#endif
  }

 private:
  static constexpr uint32_t kBeepHz = 2000;
  static constexpr uint32_t kBeepMs = 20;
};
