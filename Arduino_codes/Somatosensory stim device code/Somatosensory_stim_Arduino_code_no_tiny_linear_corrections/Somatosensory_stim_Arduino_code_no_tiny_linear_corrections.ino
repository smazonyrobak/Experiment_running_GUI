#include <Arduino.h>
#include <Wire.h>
#include <U8g2lib.h>
#include <TMCStepper.h>
#include <GPT_Stepper.h>
#include <math.h>

// ===================== PINS (your wiring) =====================
static const uint8_t STEP_A = 2, DIR_A = 3, EN_A = 4, CS_A = 10;
static const uint8_t STEP_B = 5, DIR_B = 6, EN_B = A2, CS_B = 9;

static const uint8_t BNC_LINEAR = 7;
static const uint8_t BNC_ROTARY = 8;

static const uint8_t LIM_LEFT  = A0;
static const uint8_t LIM_RIGHT = A1;

static const uint8_t SPI_MOSI_PIN = 11;
static const uint8_t SPI_MISO_PIN = 12;
static const uint8_t SPI_SCK_PIN  = 13;

// ===================== OLED =====================
U8G2_SH1106_128X64_NONAME_F_HW_I2C oled(U8G2_R0, U8X8_PIN_NONE);

// ===================== DRIVERS =====================
static constexpr float R_SENSE = 0.075f;
TMC5160Stepper drvRot(CS_A, R_SENSE, SPI_MOSI_PIN, SPI_MISO_PIN, SPI_SCK_PIN);
TMC5160Stepper drvLin(CS_B, R_SENSE, SPI_MOSI_PIN, SPI_MISO_PIN, SPI_SCK_PIN);

// ===================== TIMER STEPPERS =====================
static constexpr bool ROT_INVERT_DIR = false;
static constexpr bool LIN_INVERT_DIR = false;
GPT_Stepper rot(STEP_A, DIR_A, 20000.0f, ROT_INVERT_DIR);
GPT_Stepper lin(STEP_B, DIR_B, 12000.0f, LIN_INVERT_DIR);

// ===================== CONFIG =====================
struct Config {
  float wheel_d_cm = 5.0f;
  float linear_steps_per_mm = 14.1593f; // from 0.5 rev -> 11.3 cm
  uint16_t lin_microsteps = 16;
  uint16_t rot_microsteps = 128;

  float lin_home_cm_s = 3.0f;
  float lin_move_cm_s = 5.0f;
  float lin_offset_cm_after_left = 0.5f;

  float rot_duration_s = 2.5f;
  float interval_s = 0.25f;

  bool hybrid_enable = true;
  float stealth_max_cm_s = 8.0f;
  float spread_min_cm_s  = 8.0f;
  uint16_t rot_current_stealth_mA = 350;
  uint16_t rot_current_spread_mA  = 400;
  uint16_t lin_current_mA = 500;

  bool dither_enable = true;
  float dither_min_cm_s = 8.0f;
  float dither_amp = 0.0035f;
  float dither_hz  = 30.0f;
  uint32_t dither_update_us = 2000;

  uint8_t spread_preset = 1;
} cfg;

// ===================== RUNTIME =====================
enum State { IDLE_WAIT_CFG, WAIT_RUN_BEGIN, DO_HOMING, REQUEST_MOVE, WAITING_MOVE_LINE, RUNNING_MOVE, RUN_DONE, ERROR_STATE };
State state = IDLE_WAIT_CFG;

static uint32_t total_moves = 0;
static uint32_t current_move_index = 0;
static uint32_t lastReadyMs = 0;
static bool request_sent = false;

// ✅ SINGLE definition of MoveCmd
struct MoveCmd {
  float pos_cm = 0.0f;
  float speed_cm_s = 0.0f;
  float interval_s = -1.0f;
  bool has_next_pos = false;
  float next_pos_cm = 0.0f;
  int8_t dir = +1;
  char label[32] = {0};
} curMove;

// ===================== HELPERS =====================
static inline bool leftPressed()  { return digitalRead(LIM_LEFT)  == LOW; }
static inline bool rightPressed() { return digitalRead(LIM_RIGHT) == LOW; }

static inline void bncOn(uint8_t pin)  { digitalWrite(pin, LOW); }
static inline void bncOff(uint8_t pin) { digitalWrite(pin, HIGH); }

static inline void enableMotor(uint8_t enPin)  { digitalWrite(enPin, LOW); }
static inline void disableMotor(uint8_t enPin) { digitalWrite(enPin, HIGH); }

static bool i2cProbe(uint8_t addr7) {
  Wire.beginTransmission(addr7);
  return (Wire.endTransmission() == 0);
}

static void oledStage(const char* l1, const char* l2 = "", const char* l3 = "") {
  oled.clearBuffer();
  oled.setFont(u8g2_font_6x13_tf);
  oled.drawStr(0, 14, l1);
  oled.drawStr(0, 32, l2);
  oled.drawStr(0, 48, l3);
  char buf[24];
  snprintf(buf, sizeof(buf), "L:%d R:%d", digitalRead(LIM_LEFT), digitalRead(LIM_RIGHT));
  oled.drawStr(0, 64, buf);
  oled.sendBuffer();
}

static bool waitStopped(GPT_Stepper& s, uint32_t timeoutMs) {
  uint32_t t0 = millis();
  while (millis() - t0 < timeoutMs) {
    if (fabs(s.getCurrentSpeed()) < 0.1f) return true;
  }
  return false;
}

static bool confirmPressed(bool (*pressedFn)()) {
  if (!pressedFn()) return false;
  delay(3);
  return pressedFn();
}

// ===================== DRIVER CONFIG =====================
static void linDriverConfig() {
  drvLin.begin();
  drvLin.toff(5);
  drvLin.blank_time(24);
  drvLin.rms_current(cfg.lin_current_mA);
  drvLin.microsteps(cfg.lin_microsteps);
  drvLin.intpol(true);
  drvLin.en_pwm_mode(true);
  drvLin.pwm_autoscale(true);
  drvLin.pwm_autograd(true);
  drvLin.TPWMTHRS(0xFFFFF);
  drvLin.semin(0);
  drvLin.semax(0);
}

static void rotaryApplySpreadPreset() {
  drvRot.toff(5);
  drvRot.blank_time(24);
  if (cfg.spread_preset == 1) {
    drvRot.hysteresis_start(6);
    drvRot.hysteresis_end(4);
  } else if (cfg.spread_preset == 2) {
    drvRot.blank_time(36);
    drvRot.hysteresis_start(7);
    drvRot.hysteresis_end(5);
  }
  drvRot.semin(0);
  drvRot.semax(0);
}

static void rotarySetStealth() {
  drvRot.rms_current(cfg.rot_current_stealth_mA);
  drvRot.en_pwm_mode(true);
  drvRot.pwm_autoscale(true);
  drvRot.pwm_autograd(true);
  drvRot.TPWMTHRS(0xFFFFF);
}

static void rotarySetSpread() {
  drvRot.rms_current(cfg.rot_current_spread_mA);
  drvRot.en_pwm_mode(false);
  rotaryApplySpreadPreset();
}

static void rotDriverBaseConfig() {
  drvRot.begin();
  drvRot.microsteps(cfg.rot_microsteps);
  drvRot.intpol(true);
  rotarySetStealth();
}

static void rotarySelectModeForSpeed(float cm_s) {
  if (!cfg.hybrid_enable) {
    if (cfg.spread_preset > 0) rotarySetSpread();
    else rotarySetStealth();
    return;
  }
  if (cm_s < cfg.stealth_max_cm_s) rotarySetStealth();
  else if (cm_s >= cfg.spread_min_cm_s) rotarySetSpread();
  else rotarySetStealth();
}

// ===================== LINEAR MOVES =====================
static bool moveLinearRelativeCm(float delta_cm, bool useLimits = true) {
  long deltaSteps = lroundf(delta_cm * 10.0f * cfg.linear_steps_per_mm);
  if (deltaSteps == 0) return true;

  float v = (cfg.lin_move_cm_s * 10.0f) * cfg.linear_steps_per_mm;
  float a = 12000.0f;
  lin.setAcceleration(a);

  const float vv = fabs(v);
  long decelSteps = (long)ceil((vv * vv) / (2.0f * a));
  if (decelSteps < 1) decelSteps = 1;

  long startPos = lin.getPosition();
  long targetPos = startPos + deltaSteps;

  bncOn(BNC_LINEAR);
  lin.setSpeed(deltaSteps > 0 ? +vv : -vv);

  uint32_t t0 = millis();
  bool startedDecel = false;
  while (millis() - t0 < 30000) {
    if (useLimits && deltaSteps > 0 && rightPressed()) { lin.stop(); bncOff(BNC_LINEAR); return true; }
    if (useLimits && deltaSteps < 0 && leftPressed())  { lin.stop(); bncOff(BNC_LINEAR); return true; }

    long cur = lin.getPosition();
    long remaining = labs(targetPos - cur);
    if (!startedDecel && remaining <= decelSteps) {
      startedDecel = true;
      lin.setSpeed(0.0f);
      break;
    }
  }

  bool stopped = waitStopped(lin, 15000);
  bncOff(BNC_LINEAR);
  return stopped;
}

static bool moveLinearToAbsCm(float target_cm) {
  long targetSteps = lroundf(target_cm * 10.0f * cfg.linear_steps_per_mm);
  long curSteps = lin.getPosition();
  long deltaSteps = targetSteps - curSteps;
  float delta_cm = (float)deltaSteps / (cfg.linear_steps_per_mm * 10.0f);
  return moveLinearRelativeCm(delta_cm);
}

static bool homeFastToSwitch(bool goRight) {
  const float homeStepsPerS = (cfg.lin_home_cm_s * 10.0f) * cfg.linear_steps_per_mm;

  bncOn(BNC_LINEAR);
  lin.setSpeed(goRight ? +homeStepsPerS : -homeStepsPerS);

  bool hit = false;
  uint32_t t0 = millis();
  while (millis() - t0 < 60000) {
    if (goRight) { if (confirmPressed(rightPressed)) { hit = true; break; } }
    else         { if (confirmPressed(leftPressed))  { hit = true; break; } }
  }

  lin.stop();
  bncOff(BNC_LINEAR);

  return hit;
}

static bool homeTwoStage(bool goRight) {
  if (!homeFastToSwitch(goRight)) return false;

  float backoff = 0.2f;
  if (goRight) {
    if (!moveLinearRelativeCm(-backoff, false)) return false;
    if (rightPressed()) return false;
  } else {
    if (!moveLinearRelativeCm(+backoff, false)) return false;
    if (leftPressed()) return false;
  }

  float prev = cfg.lin_home_cm_s;
  cfg.lin_home_cm_s = 1.0f;
  bool ok = homeFastToSwitch(goRight);
  cfg.lin_home_cm_s = prev;

  return ok;
}

// ===================== ROTARY =====================
static float rotaryStepsPerSecondFromCmPerS(float cm_s) {
  const float circumference_cm = 3.1415926535f * cfg.wheel_d_cm;
  const float rps = cm_s / circumference_cm;
  const long stepsPerRev = (long)200 * (long)cfg.rot_microsteps;
  return rps * (float)stepsPerRev;
}

static void rotaryHoldWithOptionalDither(float baseStepsPerS, uint32_t holdMs, bool ditherEnabled) {
  if (!ditherEnabled) {
    rot.setSpeed(baseStepsPerS);
    uint32_t t0 = millis();
    while (millis() - t0 < holdMs) {}
    return;
  }
  const uint32_t startUs = micros();
  uint32_t nextUs = startUs;
  while ((uint32_t)(micros() - startUs) < holdMs * 1000UL) {
    uint32_t now = micros();
    if ((int32_t)(now - nextUs) >= 0) {
      float t = (now - startUs) * 1e-6f;
      float s = sinf(2.0f * 3.1415926535f * cfg.dither_hz * t);
      float sp = baseStepsPerS * (1.0f + cfg.dither_amp * s);
      rot.setSpeed(sp);
      nextUs += cfg.dither_update_us;
    }
  }
}

static bool runRotary(float speed_cm_s, int8_t dir) {
  rot.setSpeed(0.0f);
  (void)waitStopped(rot, 5000);

  rotarySelectModeForSpeed(speed_cm_s);

  enableMotor(EN_A);

  const float baseStepsPerS = rotaryStepsPerSecondFromCmPerS(speed_cm_s) * (dir >= 0 ? +1.0f : -1.0f);
  const bool ditherThis = cfg.dither_enable && (speed_cm_s >= cfg.dither_min_cm_s);

  bncOn(BNC_ROTARY);

  rot.setAcceleration(20000.0f);
  rot.setSpeed(baseStepsPerS);
  delay(60);

  uint32_t holdMs = (uint32_t)lroundf(cfg.rot_duration_s * 1000.0f);
  rotaryHoldWithOptionalDither(baseStepsPerS, holdMs, ditherThis);

  rot.setSpeed(0.0f);
  bool stopped = waitStopped(rot, 20000);

  bncOff(BNC_ROTARY);
  disableMotor(EN_A);

  return stopped;
}

// ===================== SERIAL =====================
static String readLineNonBlocking() {
  static String buf;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      String out = buf; buf = "";
      out.trim();
      return out;
    }
    buf += c;
    if (buf.length() > 260) buf = "";
  }
  return String();
}

static bool handleCFG(const String& line) {
  int s1 = line.indexOf(' ');
  int s2 = line.indexOf(' ', s1 + 1);
  if (s1 < 0 || s2 < 0) return false;
  String key = line.substring(s1 + 1, s2);
  String val = line.substring(s2 + 1);
  key.trim(); val.trim();

  float f = val.toFloat();
  int i = (int)val.toInt();

  if (key == "WHEEL_D_CM") cfg.wheel_d_cm = f;
  else if (key == "LIN_STEPS_PER_MM") cfg.linear_steps_per_mm = f;
  else if (key == "LIN_HOME_CM_S") cfg.lin_home_cm_s = f;
  else if (key == "LIN_MOVE_CM_S") cfg.lin_move_cm_s = f;
  else if (key == "LIN_OFFSET_CM0") cfg.lin_offset_cm_after_left = f;

  else if (key == "ROT_MICROSTEPS") cfg.rot_microsteps = (uint16_t)i;
  else if (key == "LIN_MICROSTEPS") cfg.lin_microsteps = (uint16_t)i;

  else if (key == "ROT_DUR_S") cfg.rot_duration_s = f;
  else if (key == "INTERVAL_S") cfg.interval_s = f;

  else if (key == "HYBRID_EN") cfg.hybrid_enable = (i != 0);
  else if (key == "STEALTH_MAX") cfg.stealth_max_cm_s = f;
  else if (key == "SPREAD_MIN") cfg.spread_min_cm_s = f;

  else if (key == "ROT_I_STEALTH") cfg.rot_current_stealth_mA = (uint16_t)i;
  else if (key == "ROT_I_SPREAD")  cfg.rot_current_spread_mA  = (uint16_t)i;
  else if (key == "LIN_I")         cfg.lin_current_mA          = (uint16_t)i;

  else if (key == "DITHER_EN") cfg.dither_enable = (i != 0);
  else if (key == "DITHER_MIN") cfg.dither_min_cm_s = f;
  else if (key == "DITHER_AMP") cfg.dither_amp = f;
  else if (key == "DITHER_HZ")  cfg.dither_hz  = f;
  else if (key == "DITHER_US")  cfg.dither_update_us = (uint32_t)i;

  else if (key == "SPREAD_PRESET") cfg.spread_preset = (uint8_t)i;
  else return false;

  return true;
}

static bool handleRunBegin(const String& line) {
  int sp = line.indexOf(' ');
  if (sp < 0) return false;
  total_moves = (uint32_t)line.substring(sp + 1).toInt();
  current_move_index = 0;
  request_sent = false;
  return true;
}

static bool handleMove(const String& line, MoveCmd& out) {
  int p1 = line.indexOf(' ');
  int p2 = line.indexOf(' ', p1 + 1);
  int p3 = line.indexOf(' ', p2 + 1);
  int p4 = line.indexOf(' ', p3 + 1);
  if (p1 < 0 || p2 < 0 || p3 < 0 || p4 < 0) return false;
  int p5 = line.indexOf(' ', p4 + 1);
  int p6 = p5 > 0 ? line.indexOf(' ', p5 + 1) : -1;
  int p7 = p6 > 0 ? line.indexOf(' ', p6 + 1) : -1;

  out.pos_cm = line.substring(p1 + 1, p2).toFloat();
  out.speed_cm_s = line.substring(p2 + 1, p3).toFloat();
  String sDir = line.substring(p3 + 1, p4);
  String sLab;
  out.interval_s = -1.0f;
  out.has_next_pos = false;
  out.next_pos_cm = out.pos_cm;
  if (p5 > 0 && p6 > 0 && p7 > 0) {
    out.interval_s = line.substring(p4 + 1, p5).toFloat();
    out.has_next_pos = line.substring(p5 + 1, p6).toInt() != 0;
    out.next_pos_cm = line.substring(p6 + 1, p7).toFloat();
    sLab = line.substring(p7 + 1);
  } else {
    sLab = line.substring(p4 + 1);
  }
  sLab.trim();

  out.dir = (sDir.length() && (sDir[0] == 'L' || sDir[0] == 'l')) ? -1 : +1;
  memset(out.label, 0, sizeof(out.label));
  sLab.toCharArray(out.label, sizeof(out.label));
  return true;
}

// ===================== HOMING + REF 0 =====================
static bool doHomingAndSetZero() {
  enableMotor(EN_B);
  disableMotor(EN_A);

  bool ok = homeTwoStage(false);
  if (ok) lin.setHome();
  disableMotor(EN_B);
  return ok;
}

// ===================== MAIN =====================
void setup() {
  pinMode(EN_A, OUTPUT);
  pinMode(EN_B, OUTPUT);
  pinMode(CS_A, OUTPUT); digitalWrite(CS_A, HIGH);
  pinMode(CS_B, OUTPUT); digitalWrite(CS_B, HIGH);

  pinMode(BNC_LINEAR, OUTPUT);
  pinMode(BNC_ROTARY, OUTPUT);
  bncOff(BNC_LINEAR);
  bncOff(BNC_ROTARY);

  pinMode(LIM_LEFT, INPUT);
  pinMode(LIM_RIGHT, INPUT);

  Serial.begin(115200);

  Wire.begin();
  Wire.setClock(400000);
  if (i2cProbe(0x3D)) oled.setI2CAddress(0x3D << 1);
  else               oled.setI2CAddress(0x3C << 1);
  oled.begin();

  bool ok1 = rot.init();
  bool ok2 = lin.init();

  linDriverConfig();
  rotDriverBaseConfig();

  disableMotor(EN_A);
  disableMotor(EN_B);

  oledStage("WAIT PC", ok1 && ok2 ? "Stepper timers OK" : "TIMER INIT FAIL");
  Serial.println("READY");
  lastReadyMs = millis();
  state = IDLE_WAIT_CFG;
}

static void applyAllDriverConfig() {
  linDriverConfig();
  rotDriverBaseConfig();
}

void loop() {
  if (state == IDLE_WAIT_CFG && millis() - lastReadyMs > 500) {
    Serial.println("READY");
    lastReadyMs = millis();
  }

  String line = readLineNonBlocking();
  if (line.length()) {
    if (line == "PING") {
      Serial.println("READY");
    } else if (line.startsWith("CFG ")) {
      if (!handleCFG(line)) Serial.println("ERR CFG");
    } else if (line == "CFG_END") {
      applyAllDriverConfig();
      Serial.println("CFG_OK");
      state = WAIT_RUN_BEGIN;
      oledStage("CFG OK", "Waiting RUN_BEGIN");
    } else if (line.startsWith("RUN_BEGIN ")) {
      if (!handleRunBegin(line)) Serial.println("ERR RUN_BEGIN");
      else { Serial.println("RUN_OK"); state = DO_HOMING; }
    } else if (line == "ABORT") {
      lin.stop(); rot.stop();
      bncOff(BNC_LINEAR); bncOff(BNC_ROTARY);
      disableMotor(EN_A); disableMotor(EN_B);
      state = ERROR_STATE;
      Serial.println("ABORTED");
      oledStage("ABORTED");
    } else if (line.startsWith("MOVE ") && state == WAITING_MOVE_LINE) {
      if (handleMove(line, curMove)) state = RUNNING_MOVE;
      else Serial.println("ERR MOVE");
    }
  }

  switch (state) {
    case IDLE_WAIT_CFG: break;
    case WAIT_RUN_BEGIN: break;

    case DO_HOMING: {
      oledStage("HOMING...", "2-stage LEFT only", "left switch -> 0");
      bool ok = doHomingAndSetZero();
      if (!ok) { Serial.println("HOME_FAIL"); oledStage("HOME FAIL"); state = ERROR_STATE; break; }
      Serial.println("HOME_OK");
      oledStage("HOME OK", "Ready for moves");
      current_move_index = 0;
      request_sent = false;
      state = REQUEST_MOVE;
      break;
    }

    case REQUEST_MOVE: {
      if (current_move_index >= total_moves) {
        Serial.println("RUN_DONE");
        oledStage("RUN DONE");
        state = RUN_DONE;
        break;
      }
      if (!request_sent) {
        Serial.print("READY_MOVE "); Serial.print(current_move_index + 1);
        Serial.print(" "); Serial.println(total_moves);
        oledStage("Waiting MOVE...");
        request_sent = true;
        state = WAITING_MOVE_LINE;
      }
      break;
    }

    case WAITING_MOVE_LINE: break;

    case RUNNING_MOVE: {
      char l1[24], l2[32], l3[32];
      snprintf(l1, sizeof(l1), "MOVE %lu/%lu", (unsigned long)(current_move_index + 1), (unsigned long)total_moves);
      snprintf(l2, sizeof(l2), "%s", curMove.label);
      snprintf(l3, sizeof(l3), "Lin %.2f Rot %.1f%c", curMove.pos_cm, curMove.speed_cm_s, (curMove.dir > 0 ? 'R' : 'L'));
      oledStage(l1, l2, l3);

      Serial.print("MOVE_START "); Serial.print(current_move_index + 1);
      Serial.print("/"); Serial.print(total_moves);
      Serial.print(" "); Serial.println(curMove.label);

      enableMotor(EN_B);
      bool ok = moveLinearToAbsCm(curMove.pos_cm);
      disableMotor(EN_B);
      if (!ok) { Serial.println("MOVE_FAIL_LINEAR"); oledStage("MOVE FAIL", curMove.label, "linear"); state = ERROR_STATE; break; }

      ok = runRotary(curMove.speed_cm_s, curMove.dir);
      if (!ok) { Serial.println("MOVE_FAIL_ROTARY"); oledStage("MOVE FAIL", curMove.label, "rotary"); state = ERROR_STATE; break; }

      if (curMove.has_next_pos) {
        if (fabs(curMove.next_pos_cm - curMove.pos_cm) > 0.0005f) {
          enableMotor(EN_B);
          ok = moveLinearToAbsCm(curMove.next_pos_cm);
          disableMotor(EN_B);
          if (!ok) { Serial.println("MOVE_FAIL_LINEAR_NEXT"); oledStage("MOVE FAIL", curMove.label, "next linear"); state = ERROR_STATE; break; }
        }

        float interval_s = curMove.interval_s >= 0.0f ? curMove.interval_s : cfg.interval_s;
        if (interval_s > 0) delay((uint32_t)lroundf(interval_s * 1000.0f));
      }

      current_move_index++;
      Serial.print("MOVE_DONE "); Serial.print(current_move_index);
      Serial.print(" "); Serial.println(curMove.label);

      request_sent = false;
      state = REQUEST_MOVE;
      break;
    }

    case RUN_DONE: break;
    case ERROR_STATE: break;
  }
}
