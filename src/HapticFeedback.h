#pragma once

#include <HalBuzzer.h>
#include <HalGPIO.h>
#include <HalHaptics.h>

#include "CrossPointSettings.h"

// Touch feedback (vibration + beep). Taps on content call touchAction() only
// after the application accepts the action, so a tap on an empty area stays
// silent. The header back arrow and swipes are recognised centrally in
// MappedInputManager::update(). One gesture gives one pulse per input frame.
namespace haptic_feedback {
namespace detail {
inline bool& givenThisFrame() {
  static bool given = false;
  return given;
}
}  // namespace detail

// Call once per input frame, right after the GPIO update.
inline void beginFrame() { detail::givenThisFrame() = false; }

inline void touchAction(bool longPress = false) {
  if (detail::givenThisFrame()) return;
  detail::givenThisFrame() = true;
  HalBuzzer::beep(SETTINGS.touchBeep != 0);
  const bool enabled = SETTINGS.vibration != CrossPointSettings::VIBRATION_OFF;
  if (longPress) {
    HalHaptics::longPress(enabled, SETTINGS.hapticIntensity);
  } else {
    HalHaptics::feedback(enabled, true, SETTINGS.hapticIntensity);
  }
}

// A swipe is a deliberate gesture, whichever activity consumes it.
inline void swipeGesture(const HalGPIO& gpio) {
  float a = 0.0f;
  float b = 0.0f;
  float c = 0.0f;
  float d = 0.0f;
  if (gpio.hasTouch() && gpio.wasSwipe(a, b, c, d)) touchAction();
}
}  // namespace haptic_feedback
