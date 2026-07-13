#include <Arduino.h>
#include <ctype.h>

// Multi-output camera hardware trigger generator.
//
// Serial protocol:
//   "<freq_hz> <pulse_ms> <count>\n"
// Example:
//   "30 4 10000\n"
//
// Optional commands:
//   PING    -> PONG
//   STATUS  -> IDLE / RUNNING / DONE / ABORTED
//   STOP    -> abort the current train
//   HELP    -> prints a short help line
//
// This sketch is intentionally compatible with the existing Python trigger code
// in GUI_for_DeepEcoHab.py / GUI_for_Neuropixels.py, which sends one ASCII line
// containing: frequency, pulse width in ms, and pulse count, then waits for DONE.

static const unsigned long SERIAL_BAUD = 115200;

// Edit these pins to match your wiring.
// The sketch will emit the same TTL pulse train on every pin listed here.
// Add or remove pins as needed; TRIGGER_PIN_COUNT is derived automatically.
//
// Important: the Arduino cannot auto-detect how many physical output jacks are
// present on your breakout. It can only drive the pins you explicitly list.
static const uint8_t TRIGGER_PINS[] = {2, 3, 4, 5, 6};
static const size_t TRIGGER_PIN_COUNT = sizeof(TRIGGER_PINS) / sizeof(TRIGGER_PINS[0]);

// Set true for standard active-high TTL pulses.
// Set false only if your downstream electronics expects inverted logic.
static const bool ACTIVE_HIGH = true;

// LED mirrors the pulse train for quick visual confirmation.
static const uint8_t STATUS_LED_PIN = LED_BUILTIN;

static const size_t RX_BUF_LEN = 64;
char rxBuf[RX_BUF_LEN];
size_t rxLen = 0;

enum RunState : uint8_t {
  STATE_IDLE = 0,
  STATE_RUNNING,
  STATE_DONE,
  STATE_ABORTED
};

struct TriggerTrain {
  RunState state = STATE_IDLE;
  bool line_is_high = false;
  unsigned long freq_hz = 0;
  unsigned long pulse_us = 0;
  unsigned long period_us = 0;
  unsigned long low_us = 0;
  unsigned long requested_count = 0;
  unsigned long emitted_count = 0;
  uint32_t next_toggle_us = 0;
} train;

static inline bool timeReached(uint32_t now, uint32_t target) {
  return (int32_t)(now - target) >= 0;
}

static bool equalsIgnoreCase(const char* a, const char* b) {
  while (*a && *b) {
    const char ca = (char)tolower((unsigned char)*a);
    const char cb = (char)tolower((unsigned char)*b);
    if (ca != cb) {
      return false;
    }
    ++a;
    ++b;
  }
  return (*a == '\0') && (*b == '\0');
}

static inline uint8_t levelForState(bool active) {
  if (ACTIVE_HIGH) {
    return active ? HIGH : LOW;
  }
  return active ? LOW : HIGH;
}

static void setAllTriggerPins(bool active) {
  const uint8_t level = levelForState(active);
  for (size_t i = 0; i < TRIGGER_PIN_COUNT; ++i) {
    digitalWrite(TRIGGER_PINS[i], level);
  }
  digitalWrite(STATUS_LED_PIN, active ? HIGH : LOW);
}

static void stopTrain(RunState final_state, const __FlashStringHelper* message) {
  setAllTriggerPins(false);
  train.state = final_state;
  train.line_is_high = false;
  train.freq_hz = 0;
  train.pulse_us = 0;
  train.period_us = 0;
  train.low_us = 0;
  train.requested_count = 0;
  train.emitted_count = 0;
  train.next_toggle_us = micros();
  Serial.println(message);
}

static bool startTrain(unsigned long freq_hz, unsigned long pulse_ms, unsigned long count) {
  if (freq_hz == 0UL) {
    Serial.println(F("ERR freq_hz must be > 0"));
    return false;
  }
  if (pulse_ms == 0UL) {
    Serial.println(F("ERR pulse_ms must be > 0"));
    return false;
  }
  if (count == 0UL) {
    Serial.println(F("ERR count must be > 0"));
    return false;
  }

  const unsigned long period_us = 1000000UL / freq_hz;
  const unsigned long pulse_us = pulse_ms * 1000UL;
  if (period_us == 0UL) {
    Serial.println(F("ERR period underflow; lower freq_hz"));
    return false;
  }
  if (pulse_us >= period_us) {
    Serial.println(F("ERR pulse_ms must be shorter than the period"));
    return false;
  }

  if (train.state == STATE_RUNNING) {
    Serial.println(F("ERR busy; send STOP first"));
    return false;
  }

  train.state = STATE_RUNNING;
  train.line_is_high = false;
  train.freq_hz = freq_hz;
  train.pulse_us = pulse_us;
  train.period_us = period_us;
  train.low_us = period_us - pulse_us;
  train.requested_count = count;
  train.emitted_count = 0;
  train.next_toggle_us = micros();

  setAllTriggerPins(false);

  Serial.print(F("RUNNING "));
  Serial.print(freq_hz);
  Serial.print(F("Hz "));
  Serial.print(pulse_ms);
  Serial.print(F("ms "));
  Serial.print(count);
  Serial.println(F(" pulses"));
  return true;
}

static void printStatus() {
  switch (train.state) {
    case STATE_IDLE:
      Serial.println(F("IDLE"));
      break;
    case STATE_RUNNING:
      Serial.print(F("RUNNING "));
      Serial.print(train.emitted_count);
      Serial.print(F("/"));
      Serial.println(train.requested_count);
      break;
    case STATE_DONE:
      Serial.println(F("DONE"));
      break;
    case STATE_ABORTED:
      Serial.println(F("ABORTED"));
      break;
  }
}

static void handleLine(char* line) {
  while (*line == ' ' || *line == '\t') {
    ++line;
  }
  if (*line == '\0') {
    return;
  }

  if (equalsIgnoreCase(line, "PING")) {
    Serial.println(F("PONG"));
    return;
  }
  if (equalsIgnoreCase(line, "STATUS")) {
    printStatus();
    return;
  }
  if (equalsIgnoreCase(line, "STOP")) {
    if (train.state == STATE_RUNNING) {
      stopTrain(STATE_ABORTED, F("ABORTED"));
    } else {
      Serial.println(F("IDLE"));
    }
    return;
  }
  if (equalsIgnoreCase(line, "HELP")) {
    Serial.println(F("Use: <freq_hz> <pulse_ms> <count> | PING | STATUS | STOP"));
    return;
  }

  unsigned long freq_hz = 0;
  unsigned long pulse_ms = 0;
  unsigned long count = 0;
  if (sscanf(line, "%lu %lu %lu", &freq_hz, &pulse_ms, &count) == 3) {
    startTrain(freq_hz, pulse_ms, count);
    return;
  }

  Serial.println(F("ERR unknown command"));
}

static void pollSerial() {
  while (Serial.available() > 0) {
    const char c = (char)Serial.read();
    if (c == '\r') {
      continue;
    }
    if (c == '\n') {
      rxBuf[rxLen] = '\0';
      handleLine(rxBuf);
      rxLen = 0;
      continue;
    }
    if (rxLen + 1 < RX_BUF_LEN) {
      rxBuf[rxLen++] = c;
    } else {
      rxLen = 0;
      Serial.println(F("ERR command too long"));
    }
  }
}

static void updateTrain() {
  if (train.state != STATE_RUNNING) {
    return;
  }

  const uint32_t now = micros();
  if (!timeReached(now, train.next_toggle_us)) {
    return;
  }

  if (!train.line_is_high) {
    if (train.emitted_count >= train.requested_count) {
      stopTrain(STATE_DONE, F("DONE"));
      return;
    }

    setAllTriggerPins(true);
    train.line_is_high = true;
    train.emitted_count += 1;
    train.next_toggle_us += train.pulse_us;
    return;
  }

  setAllTriggerPins(false);
  train.line_is_high = false;

  if (train.emitted_count >= train.requested_count) {
    stopTrain(STATE_DONE, F("DONE"));
    return;
  }

  train.next_toggle_us += train.low_us;
}

void setup() {
  for (size_t i = 0; i < TRIGGER_PIN_COUNT; ++i) {
    pinMode(TRIGGER_PINS[i], OUTPUT);
  }
  pinMode(STATUS_LED_PIN, OUTPUT);
  setAllTriggerPins(false);

  Serial.begin(SERIAL_BAUD);
  while (!Serial) {
    delay(10);
  }

  Serial.println(F("CAM_TRIGGER_READY"));
  Serial.println(F("Use: <freq_hz> <pulse_ms> <count>"));
}

void loop() {
  pollSerial();
  updateTrain();
}
