import sys, csv, re, time, hashlib, random
from dataclasses import dataclass
from pathlib import Path

import serial
from serial.tools import list_ports
from PySide6 import QtCore, QtWidgets


# ---------------- Data models ----------------
@dataclass
class LinPos:
    label: str
    cm: float

@dataclass
class RotCmd:
    token: str
    cm_s: float
    dir: str  # "R" or "L"

@dataclass
class Move:
    pos_label: str
    pos_cm: float
    rot_token: str
    rot_cm_s: float
    rot_dir: str
    repeat_index: int
    step_in_repeat: int
    global_step: int
    interval_s: float = -1.0

    @property
    def move_label(self) -> str:
        pos_label = str(self.pos_label).strip()
        rot_token = str(self.rot_token).strip()
        pos_lower = pos_label.lower()
        rot_lower = rot_token.lower()
        if is_recalibration_label(pos_label):
            return f"r{self.repeat_index}_recalibration"
        no_brushing = pos_lower in {"no_brushing", "null_brushing", "null"}
        no_rotation = (
            rot_lower == "no_rotation"
            or rot_lower.startswith("no_rotation_")
            or rot_lower == "null_rotating"
            or rot_lower.startswith("null_rotating_")
        )
        if no_brushing and no_rotation:
            return f"r{self.repeat_index}_null_stimulus"
        return f"r{self.repeat_index}_{self.pos_label}{self.rot_token}"

# ---------------- Utils ----------------
ROT_RE = re.compile(r"^\s*(\d+(\.\d+)?)\s*([RrLl])\s*$")
POST_HOME_FIRST_STIMULUS_DELAY_S = 10.0
RECALIBRATION_PREFIX = "recalibration"
RECALIBRATION_MIDDLE_LABEL = "recalibration"
RECALIBRATION_END_LABEL = "recalibration"

def is_recalibration_label(label: str) -> bool:
    return str(label or "").strip().lower().startswith(RECALIBRATION_PREFIX)

def parse_rot_token(s: str) -> RotCmd | None:
    if not s.strip():
        return None
    m = ROT_RE.match(s)
    if not m:
        return None
    cm_s = float(m.group(1))
    d = m.group(3).upper()
    token = f"{cm_s:g}{d}"
    return RotCmd(token=token, cm_s=cm_s, dir=d)

def list_serial_ports():
    return [(p.device, p.description) for p in list_ports.comports()]

def seed_to_int(seed16: str) -> int:
    if not re.fullmatch(r"\d{16}", seed16):
        raise ValueError("Seed must be exactly 16 digits.")
    return int(seed16)

def derive_repeat_seed(base_seed: int, repeat_index: int) -> int:
    h = hashlib.sha256(f"{base_seed}-{repeat_index}".encode("utf-8")).digest()
    return int.from_bytes(h[:8], "little", signed=False)

def compute_steps_per_mm_from_halfrev(halfrev_cm: float, lin_microsteps: int = 16) -> float:
    # half rev pulses = 200*microsteps/2
    half_steps = (200 * lin_microsteps) / 2.0
    mm = halfrev_cm * 10.0
    return half_steps / mm

def compute_mode(speed_cm_s: float, hybrid_en: bool, stealth_max: float, spread_min: float) -> str:
    if not hybrid_en:
        return "AUTO"
    if speed_cm_s < stealth_max:
        return "STEALTH"
    if speed_cm_s >= spread_min:
        return "SPREAD"
    return "STEALTH"


# ---------------- Serial worker ----------------
class RunWorker(QtCore.QObject):
    log = QtCore.Signal(str)
    progress = QtCore.Signal(int, int, str)  # done, total, status
    finished = QtCore.Signal(bool, str)

    def __init__(self, port: str, cfg_lines: list[str], moves: list[Move], baud: int = 115200):
        super().__init__()
        self.port = port
        self.cfg_lines = cfg_lines
        self.moves = moves
        self.baud = baud
        self._abort = False
        self._abort_sent = False

    def abort(self):
        self._abort = True

    def _send_abort(self, ser: serial.Serial) -> None:
        if self._abort_sent:
            return
        ser.write(b"ABORT\n")
        ser.flush()
        self._abort_sent = True

    def _readline(self, ser: serial.Serial, timeout_s: float) -> str | None:
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if self._abort:
                self._send_abort(ser)
                return None
            line = ser.readline().decode(errors="ignore").strip()
            if line:
                return line
        return None

    def _read_until(self, ser: serial.Serial, expected: str, timeout_s: float, ignored: tuple[str, ...] = ()) -> str | None:
        t0 = time.time()
        last = None
        while time.time() - t0 < timeout_s:
            if self._abort:
                self._send_abort(ser)
                return None
            line = self._readline(ser, 0.5)
            if line is None:
                continue
            last = line
            self.log.emit(f"ARDUINO: {line}")
            if line == expected:
                return line
            if line in ignored:
                continue
            if line.startswith("ERR") or "FAIL" in line:
                return line
        return last

    def _sequence_chunks(self) -> list[tuple[Move | None, list[Move]]]:
        chunks: list[tuple[Move | None, list[Move]]] = []
        recalibration: Move | None = None
        chunk: list[Move] = []
        for mv in self.moves:
            if is_recalibration_label(mv.pos_label):
                chunks.append((recalibration, chunk))
                recalibration = mv
                chunk = []
            else:
                chunk.append(mv)
        chunks.append((recalibration, chunk))
        return [(recal, moves) for recal, moves in chunks if recal is not None or moves]

    def _wait_after_home(self, ser: serial.Serial, done: int, total: int, label: str) -> bool:
        if POST_HOME_FIRST_STIMULUS_DELAY_S <= 0:
            return True
        self.log.emit(
            f"{label} complete. Waiting {POST_HOME_FIRST_STIMULUS_DELAY_S:g}s before continuing."
        )
        wait_start = time.time()
        last_logged_remaining = None
        while time.time() - wait_start < POST_HOME_FIRST_STIMULUS_DELAY_S:
            if self._abort:
                self._send_abort(ser)
                return False
            remaining = int(round(POST_HOME_FIRST_STIMULUS_DELAY_S - (time.time() - wait_start)))
            if remaining != last_logged_remaining and remaining > 0:
                last_logged_remaining = remaining
                self.progress.emit(done, total, f"Waiting {remaining}s after {label.lower()}")
            time.sleep(0.1)
        return True

    def _begin_arduino_run(
        self,
        ser: serial.Serial,
        move_count: int,
        done: int,
        total: int,
        label: str,
    ) -> bool:
        ser.write(f"RUN_BEGIN {move_count}\n".encode())
        ser.flush()

        line = self._read_until(ser, "RUN_OK", 6.0, ignored=("READY", "CFG_OK"))
        if line != "RUN_OK":
            self.finished.emit(False, f"Expected RUN_OK, got: {line}")
            return False
        self.log.emit(f"RUN_OK received; waiting HOME_OK ({label}).")

        while True:
            line = self._readline(ser, 120.0)
            if line is None:
                if self._abort:
                    self.finished.emit(False, "Aborted by user.")
                else:
                    self.finished.emit(False, "Timeout waiting for HOME_OK.")
                return False
            self.log.emit(f"ARDUINO: {line}")
            if line == "HOME_OK":
                break
            if "FAIL" in line or line.startswith("ERR"):
                self.finished.emit(False, f"Arduino reported: {line}")
                return False

        if not self._wait_after_home(ser, done, total, label):
            self.finished.emit(False, f"Aborted after {label.lower()}.")
            return False
        return True

    def _run_arduino_chunk(
        self,
        ser: serial.Serial,
        moves: list[Move],
        done: int,
        total: int,
    ) -> int | None:
        idx = 0
        while idx < len(moves):
            if self._abort:
                self._send_abort(ser)
                self.finished.emit(False, "Aborted by user.")
                return None

            line = self._readline(ser, 90.0)
            if line is None:
                if self._abort:
                    self.finished.emit(False, "Aborted by user.")
                else:
                    self.finished.emit(False, "Timeout waiting for READY_MOVE.")
                return None

            if line.startswith("MOVE_START"):
                self.log.emit(f"ARDUINO: {line}")
                continue

            self.log.emit(f"ARDUINO: {line}")

            if line.startswith("READY_MOVE"):
                mv = moves[idx]
                next_mv = moves[idx + 1] if idx + 1 < len(moves) else None
                has_next = 1 if next_mv is not None else 0
                next_pos_cm = float(next_mv.pos_cm) if next_mv is not None else float(mv.pos_cm)
                cmd = (
                    f"MOVE {mv.pos_cm:.3f} {mv.rot_cm_s:.3f} {mv.rot_dir} "
                    f"{float(getattr(mv, 'interval_s', -1.0)):.4f} "
                    f"{has_next} {next_pos_cm:.3f} {mv.move_label}"
                )
                self.log.emit(f"PC: {cmd}")
                ser.write((cmd + "\n").encode())
                ser.flush()
                self.progress.emit(done, total, f"Sent {mv.move_label}")
            elif line.startswith("MOVE_DONE"):
                idx += 1
                done += 1
                self.progress.emit(done, total, "Completed")
            elif line.startswith("MOVE_FAIL") or line.startswith("ERR") or "FAIL" in line:
                self.finished.emit(False, f"Arduino error: {line}")
                return None
            elif line == "RUN_DONE":
                self.finished.emit(False, "Arduino ended the block before all moves completed.")
                return None

        while True:
            line = self._readline(ser, 90.0)
            if line is None:
                if self._abort:
                    self.finished.emit(False, "Aborted by user.")
                    return None
                return done
            self.log.emit(f"ARDUINO: {line}")
            if line == "RUN_DONE":
                return done
            if line.startswith("MOVE_FAIL") or line.startswith("ERR") or "FAIL" in line:
                self.finished.emit(False, f"Arduino error: {line}")
                return None

    @QtCore.Slot()
    def run(self):
        try:
            self.log.emit(f"Opening {self.port} @ {self.baud}...")
            with serial.Serial(self.port, self.baud, timeout=0.5) as ser:
                time.sleep(2.2)

                ser.write(b"PING\n")
                ser.flush()

                self.log.emit("Waiting for Arduino READY...")
                ready = False
                t0 = time.time()
                while time.time() - t0 < 15.0:
                    if self._abort:
                        break
                    line = ser.readline().decode(errors="ignore").strip()
                    if line:
                        self.log.emit(f"ARDUINO: {line}")
                    if line == "READY":
                        ready = True
                        break
                if not ready:
                    self.finished.emit(False, "Did not receive READY from Arduino.")
                    return

                try:
                    ser.reset_input_buffer()
                except Exception:
                    pass

                self.log.emit("Sending CFG...")
                for ln in self.cfg_lines:
                    if self._abort:
                        break
                    ser.write((ln + "\n").encode())
                    ser.flush()
                    time.sleep(0.01)
                ser.write(b"CFG_END\n")
                ser.flush()

                line = self._read_until(ser, "CFG_OK", 6.0, ignored=("READY",))
                if line != "CFG_OK":
                    self.finished.emit(False, f"Expected CFG_OK, got: {line}")
                    return
                self.log.emit("CFG_OK received.")

                total = len(self.moves)
                done = 0
                for chunk_index, (recalibration, chunk_moves) in enumerate(self._sequence_chunks(), start=1):
                    label = "Calibration" if recalibration is None else "Recalibration"
                    if recalibration is not None:
                        self.log.emit(f"Starting recalibration at sequence step {recalibration.global_step}.")
                    if not self._begin_arduino_run(ser, len(chunk_moves), done, total, label):
                        return
                    if recalibration is not None:
                        done += 1
                        self.progress.emit(done, total, "Recalibration completed")
                    if chunk_moves:
                        self.log.emit(f"Starting stimulus block {chunk_index}: {len(chunk_moves)} move(s).")
                        next_done = self._run_arduino_chunk(ser, chunk_moves, done, total)
                        if next_done is None:
                            return
                        done = next_done
                    else:
                        next_done = self._run_arduino_chunk(ser, [], done, total)
                        if next_done is None:
                            return

                self.finished.emit(True, "Run completed.")

        except Exception as e:
            self.finished.emit(False, f"Exception: {e}")


# ---------------- GUI ----------------
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Somatosensory stim control")

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)

        # Connection row
        conn = QtWidgets.QHBoxLayout()
        root.addLayout(conn)

        self.port_combo = QtWidgets.QComboBox()
        self.refresh_ports_btn = QtWidgets.QPushButton("Refresh ports")
        self.refresh_ports_btn.clicked.connect(self.refresh_ports)
        conn.addWidget(QtWidgets.QLabel("COM port:"))
        conn.addWidget(self.port_combo, 1)
        conn.addWidget(self.refresh_ports_btn)

        self.dir_edit = QtWidgets.QLineEdit()
        self.dir_btn = QtWidgets.QPushButton("Browse output dir")
        self.dir_btn.clicked.connect(self.browse_dir)
        conn.addWidget(QtWidgets.QLabel("Output dir:"))
        conn.addWidget(self.dir_edit, 2)
        conn.addWidget(self.dir_btn)

        # Split main area
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        root.addWidget(splitter, 1)

        # LEFT: settings in scroll area (no overlap)
        left_panel = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_panel)
        left_layout.setContentsMargins(6, 6, 6, 6)
        left_layout.setSpacing(8)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(left_panel)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(0, 1)

        # RIGHT: order + log
        right_panel = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right_panel)
        right_layout.setContentsMargins(6, 6, 6, 6)
        right_layout.setSpacing(8)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(1, 1)

        # ---- Settings form ----
        settings_box = QtWidgets.QGroupBox("Settings")
        form = QtWidgets.QFormLayout(settings_box)

        self.wheel_d = QtWidgets.QDoubleSpinBox(); self.wheel_d.setRange(1.0, 50.0); self.wheel_d.setValue(5.0); self.wheel_d.setSuffix(" cm")

        self.rot_duration = QtWidgets.QDoubleSpinBox(); self.rot_duration.setRange(0.1, 60.0); self.rot_duration.setValue(2.5); self.rot_duration.setSuffix(" s")
        self.interval = QtWidgets.QDoubleSpinBox(); self.interval.setRange(0.0, 30.0); self.interval.setValue(0.25); self.interval.setSuffix(" s")
        self.repeats = QtWidgets.QSpinBox(); self.repeats.setRange(1, 15); self.repeats.setValue(1)

        # Linear calibration via half-rev travel
        self.lin_halfrev_cm = QtWidgets.QDoubleSpinBox(); self.lin_halfrev_cm.setRange(1.0, 200.0); self.lin_halfrev_cm.setDecimals(2)
        self.lin_halfrev_cm.setValue(11.30); self.lin_halfrev_cm.setSuffix(" cm (per 0.5 rev)")
        self.lin_steps_mm = QtWidgets.QDoubleSpinBox(); self.lin_steps_mm.setRange(1.0, 200.0); self.lin_steps_mm.setDecimals(4)
        self.lin_steps_mm.setValue(compute_steps_per_mm_from_halfrev(self.lin_halfrev_cm.value()))
        self.lin_autocalc = QtWidgets.QCheckBox("Auto-compute steps/mm"); self.lin_autocalc.setChecked(True)
        self.lin_halfrev_cm.valueChanged.connect(self.on_halfrev_changed)
        self.lin_autocalc.toggled.connect(self.on_halfrev_changed)

        # Linear speed controls
        self.lin_home_cm_s = QtWidgets.QDoubleSpinBox(); self.lin_home_cm_s.setRange(0.1, 30.0); self.lin_home_cm_s.setValue(3.0); self.lin_home_cm_s.setSuffix(" cm/s")
        self.lin_move_cm_s = QtWidgets.QDoubleSpinBox(); self.lin_move_cm_s.setRange(0.1, 30.0); self.lin_move_cm_s.setValue(5.0); self.lin_move_cm_s.setSuffix(" cm/s")

        self.lin_offset0 = QtWidgets.QDoubleSpinBox(); self.lin_offset0.setRange(0.0, 5.0); self.lin_offset0.setDecimals(2); self.lin_offset0.setValue(0.5); self.lin_offset0.setSuffix(" cm")

        # Rotary / modes
        self.rot_micro = QtWidgets.QComboBox(); self.rot_micro.addItems(["16","32","64","128"]); self.rot_micro.setCurrentText("128")
        self.hybrid_en = QtWidgets.QCheckBox("Hybrid (Stealth low, Spread high)"); self.hybrid_en.setChecked(True)
        self.stealth_max = QtWidgets.QDoubleSpinBox(); self.stealth_max.setRange(0.1, 30.0); self.stealth_max.setValue(8.0); self.stealth_max.setSuffix(" cm/s")
        self.spread_min  = QtWidgets.QDoubleSpinBox(); self.spread_min.setRange(0.1, 30.0); self.spread_min.setValue(8.0); self.spread_min.setSuffix(" cm/s")

        self.rot_i_stealth = QtWidgets.QSpinBox(); self.rot_i_stealth.setRange(50, 2000); self.rot_i_stealth.setValue(350); self.rot_i_stealth.setSuffix(" mA")
        self.rot_i_spread  = QtWidgets.QSpinBox(); self.rot_i_spread.setRange(50, 2000); self.rot_i_spread.setValue(400); self.rot_i_spread.setSuffix(" mA")
        self.lin_i = QtWidgets.QSpinBox(); self.lin_i.setRange(50, 2000); self.lin_i.setValue(500); self.lin_i.setSuffix(" mA")
        self.spread_preset = QtWidgets.QComboBox(); self.spread_preset.addItems(["0","1","2"]); self.spread_preset.setCurrentText("1")

        # Dither
        self.dither_en = QtWidgets.QCheckBox("Enable dither"); self.dither_en.setChecked(True)
        self.dither_min = QtWidgets.QDoubleSpinBox(); self.dither_min.setRange(0.0, 30.0); self.dither_min.setValue(8.0); self.dither_min.setSuffix(" cm/s")
        self.dither_amp = QtWidgets.QDoubleSpinBox(); self.dither_amp.setRange(0.0, 0.02); self.dither_amp.setDecimals(4); self.dither_amp.setValue(0.0035)
        self.dither_hz  = QtWidgets.QDoubleSpinBox(); self.dither_hz.setRange(1.0, 200.0); self.dither_hz.setValue(30.0); self.dither_hz.setSuffix(" Hz")
        self.dither_us  = QtWidgets.QSpinBox(); self.dither_us.setRange(500, 50000); self.dither_us.setValue(2000); self.dither_us.setSuffix(" us")

        form.addRow("Wheel diameter:", self.wheel_d)
        form.addRow("Rot duration:", self.rot_duration)
        form.addRow("Interval between moves:", self.interval)
        form.addRow("Repeats:", self.repeats)

        form.addRow(QtWidgets.QLabel("— Linear calibration —"), QtWidgets.QLabel(""))
        form.addRow("Half rev travel:", self.lin_halfrev_cm)
        form.addRow("Steps/mm:", self.lin_steps_mm)
        form.addRow("", self.lin_autocalc)
        form.addRow("Linear homing speed:", self.lin_home_cm_s)
        form.addRow("Linear move speed:", self.lin_move_cm_s)
        form.addRow("Ref offset after left:", self.lin_offset0)

        form.addRow(QtWidgets.QLabel("— Rotary / modes —"), QtWidgets.QLabel(""))
        form.addRow("Rot microsteps:", self.rot_micro)
        form.addRow("", self.hybrid_en)
        form.addRow("Stealth <:", self.stealth_max)
        form.addRow("Spread ≥:", self.spread_min)
        form.addRow("Rot current (stealth):", self.rot_i_stealth)
        form.addRow("Rot current (spread):", self.rot_i_spread)
        form.addRow("Lin current:", self.lin_i)
        form.addRow("Spread preset:", self.spread_preset)

        form.addRow(QtWidgets.QLabel("— Dither —"), QtWidgets.QLabel(""))
        form.addRow("", self.dither_en)
        form.addRow("Dither min speed:", self.dither_min)
        form.addRow("Dither amp:", self.dither_amp)
        form.addRow("Dither Hz:", self.dither_hz)
        form.addRow("Dither update:", self.dither_us)

        left_layout.addWidget(settings_box)

        # ---- Positions / rotary inputs ----
        pos_box = QtWidgets.QGroupBox("Linear positions (<=5): label + cm from reference 0")
        pos_grid = QtWidgets.QGridLayout(pos_box)
        self.pos_rows = []
        for i in range(5):
            le = QtWidgets.QLineEdit()
            ds = QtWidgets.QDoubleSpinBox(); ds.setRange(0.0, 200.0); ds.setDecimals(2); ds.setSuffix(" cm")
            self.pos_rows.append((le, ds))
            pos_grid.addWidget(QtWidgets.QLabel(f"{i+1}"), i, 0)
            pos_grid.addWidget(le, i, 1)
            pos_grid.addWidget(ds, i, 2)
        left_layout.addWidget(pos_box)

        rot_box = QtWidgets.QGroupBox("Rotary speeds (<=10): tokens like 1R, 15L (cm/s)")
        rot_grid = QtWidgets.QGridLayout(rot_box)
        self.rot_rows = []
        for i in range(10):
            le = QtWidgets.QLineEdit()
            self.rot_rows.append(le)
            rot_grid.addWidget(QtWidgets.QLabel(f"{i+1}"), i, 0)
            rot_grid.addWidget(le, i, 1)
        left_layout.addWidget(rot_box)
        left_layout.addStretch(1)

        # ---- Order + scramble ----
        order_box = QtWidgets.QGroupBox("Movement order (drag to reorder when scramble OFF)")
        order_layout = QtWidgets.QVBoxLayout(order_box)

        scramble_row = QtWidgets.QHBoxLayout()
        self.scramble = QtWidgets.QCheckBox("Scramble movements")
        self.seed_edit = QtWidgets.QLineEdit(); self.seed_edit.setPlaceholderText("16-digit seed")
        self.seed_edit.setEnabled(False)
        self.scramble.toggled.connect(self.on_scramble_toggled)
        scramble_row.addWidget(self.scramble)
        scramble_row.addWidget(QtWidgets.QLabel("Seed:"))
        scramble_row.addWidget(self.seed_edit, 1)
        order_layout.addLayout(scramble_row)

        self.build_btn = QtWidgets.QPushButton("Build movement list")
        self.build_btn.clicked.connect(self.build_movements_clicked)
        order_layout.addWidget(self.build_btn)

        self.order_list = QtWidgets.QListWidget()
        self.order_list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.order_list.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        order_layout.addWidget(self.order_list, 1)

        btn_row = QtWidgets.QHBoxLayout()
        self.export_btn = QtWidgets.QPushButton("Export CSV")
        self.export_btn.clicked.connect(self.export_csv)
        self.start_btn = QtWidgets.QPushButton("START")
        self.start_btn.clicked.connect(self.start_run)
        self.abort_btn = QtWidgets.QPushButton("ABORT")
        self.abort_btn.setEnabled(False)
        self.abort_btn.clicked.connect(self.abort_run)
        btn_row.addWidget(self.export_btn)
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.abort_btn)
        order_layout.addLayout(btn_row)

        right_layout.addWidget(order_box, 2)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        right_layout.addWidget(self.log, 1)

        self.progress_label = QtWidgets.QLabel("Idle.")
        right_layout.addWidget(self.progress_label)

        self.thread = None
        self.worker = None

        self.refresh_ports()

    # ---------- UI handlers ----------
    def refresh_ports(self):
        self.port_combo.clear()
        for dev, desc in list_serial_ports():
            self.port_combo.addItem(f"{dev}  ({desc})", dev)

    def browse_dir(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose output directory")
        if d:
            self.dir_edit.setText(d)

    def on_scramble_toggled(self, checked: bool):
        self.seed_edit.setEnabled(checked)
        self.order_list.setDragDropMode(QtWidgets.QAbstractItemView.NoDragDrop if checked
                                        else QtWidgets.QAbstractItemView.InternalMove)

    def on_halfrev_changed(self, *_):
        if self.lin_autocalc.isChecked():
            self.lin_steps_mm.setValue(compute_steps_per_mm_from_halfrev(self.lin_halfrev_cm.value(), 16))

    def get_positions(self) -> list[LinPos]:
        out = []
        for le, ds in self.pos_rows:
            lab = le.text().strip()
            if not lab:
                continue
            out.append(LinPos(label=lab, cm=float(ds.value())))
        return out

    def get_rotcmds(self) -> list[RotCmd]:
        out = []
        for le in self.rot_rows:
            rc = parse_rot_token(le.text())
            if rc:
                out.append(rc)
            elif le.text().strip():
                raise ValueError(f"Bad rotary token: '{le.text()}' (use like 10R or 5L)")
        return out

    def build_base_combo(self) -> list[tuple[str,float,str,float,str]]:
        positions = self.get_positions()
        rotcmds = self.get_rotcmds()
        if not positions:
            raise ValueError("Add at least one linear position.")
        if not rotcmds:
            raise ValueError("Add at least one rotary speed token.")

        combos = []
        for p in positions:
            for r in rotcmds:
                combos.append((p.label, p.cm, r.token, r.cm_s, r.dir))
        return combos

    def base_moves_from_current_order(self) -> list[tuple[str,float,str,float,str]]:
        moves = []
        for i in range(self.order_list.count()):
            it = self.order_list.item(i)
            payload = it.data(QtCore.Qt.UserRole)
            if isinstance(payload, (tuple, list)) and len(payload) >= 5 and not is_recalibration_label(payload[0]):
                moves.append(tuple(payload[:5]))
        return moves

    def generate_full_plan(self) -> tuple[list[Move], str]:
        base = self.build_base_combo() if self.scramble.isChecked() or not self.order_list.count() else self.base_moves_from_current_order()

        repeats = int(self.repeats.value())
        scramble = self.scramble.isChecked()
        seed_used = self.seed_edit.text().strip() if scramble else ""
        base_seed = seed_to_int(seed_used) if scramble else 0

        full: list[Move] = []
        global_step = 1
        prev_order = None

        for rep in range(1, repeats + 1):
            order = list(base)
            if scramble:
                rs = random.Random(derive_repeat_seed(base_seed, rep))
                rs.shuffle(order)
                # enforce different order than previous repeat if possible
                if prev_order is not None and order == prev_order:
                    for _ in range(10):
                        rs.shuffle(order)
                        if order != prev_order:
                            break
                prev_order = list(order)

            middle_after = (len(order) + 1) // 2
            step_in_repeat = 1
            for j, (pl, pcm, rt, rcm, rd) in enumerate(order, start=1):
                full.append(Move(
                    pos_label=pl,
                    pos_cm=float(pcm),
                    rot_token=rt,
                    rot_cm_s=float(rcm),
                    rot_dir=rd,
                    repeat_index=rep,
                    step_in_repeat=step_in_repeat,
                    global_step=global_step,
                    interval_s=float(self.interval.value()),
                ))
                global_step += 1
                step_in_repeat += 1
                if j == middle_after:
                    full.append(Move(
                        pos_label=RECALIBRATION_MIDDLE_LABEL,
                        pos_cm=0.0,
                        rot_token="",
                        rot_cm_s=0.0,
                        rot_dir="R",
                        repeat_index=rep,
                        step_in_repeat=step_in_repeat,
                        global_step=global_step,
                        interval_s=POST_HOME_FIRST_STIMULUS_DELAY_S,
                    ))
                    global_step += 1
                    step_in_repeat += 1

            full.append(Move(
                pos_label=RECALIBRATION_END_LABEL,
                pos_cm=0.0,
                rot_token="",
                rot_cm_s=0.0,
                rot_dir="R",
                repeat_index=rep,
                step_in_repeat=step_in_repeat,
                global_step=global_step,
                interval_s=POST_HOME_FIRST_STIMULUS_DELAY_S,
            ))
            global_step += 1

        return full, seed_used

    def build_movements_clicked(self):
        try:
            self.order_list.clear()
            moves, seed_used = self.generate_full_plan()
            for mv in moves:
                if is_recalibration_label(mv.pos_label):
                    text = f"Repeat {mv.repeat_index} | Step {mv.step_in_repeat} | recalibration"
                    payload = None
                else:
                    text = (
                        f"Repeat {mv.repeat_index} | Step {mv.step_in_repeat} | "
                        f"{mv.move_label}   (pos {mv.pos_cm:g} cm, rot {mv.rot_cm_s:g}{mv.rot_dir} cm/s)"
                    )
                    payload = (
                        mv.pos_label,
                        mv.pos_cm,
                        mv.rot_token,
                        mv.rot_cm_s,
                        mv.rot_dir,
                    ) if mv.repeat_index == 1 else None
                it = QtWidgets.QListWidgetItem(text)
                it.setData(QtCore.Qt.UserRole, payload)
                self.order_list.addItem(it)

            self.log.appendPlainText(f"Built {len(moves)} movement items.")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Error", str(e))

    def export_csv(self):
        try:
            outdir = self.dir_edit.text().strip()
            if not outdir:
                raise ValueError("Choose an output directory first.")
            outdir = Path(outdir)
            outdir.mkdir(parents=True, exist_ok=True)

            moves, seed_used = self.generate_full_plan()
            ts = time.strftime("%Y%m%d_%H%M%S")
            fn = outdir / f"somato_plan_{ts}.csv"

            settings = {
                "wheel_d_cm": self.wheel_d.value(),
                "rot_duration_s": self.rot_duration.value(),
                "interval_s": self.interval.value(),
                "repeats": self.repeats.value(),
                "scramble": self.scramble.isChecked(),
                "seed": seed_used,
                "rot_microsteps": int(self.rot_micro.currentText()),
                "hybrid_en": self.hybrid_en.isChecked(),
                "stealth_max": self.stealth_max.value(),
                "spread_min": self.spread_min.value(),
                "rot_i_stealth_mA": self.rot_i_stealth.value(),
                "rot_i_spread_mA": self.rot_i_spread.value(),
                "lin_i_mA": self.lin_i.value(),
                "spread_preset": int(self.spread_preset.currentText()),
                "dither_en": self.dither_en.isChecked(),
                "dither_min": self.dither_min.value(),
                "dither_amp": self.dither_amp.value(),
                "dither_hz": self.dither_hz.value(),
                "dither_us": self.dither_us.value(),
                "lin_steps_mm": self.lin_steps_mm.value(),
                "lin_home_cm_s": self.lin_home_cm_s.value(),
                "lin_move_cm_s": self.lin_move_cm_s.value(),
                "lin_offset_cm0": self.lin_offset0.value(),
                "lin_halfrev_cm": self.lin_halfrev_cm.value(),
            }

            with fn.open("w", newline="", encoding="utf-8") as f:
                f.write("# Somatosensory stim plan export\n")
                for k, v in settings.items():
                    f.write(f"# {k}={v}\n")

                w = csv.writer(f)
                w.writerow(["global_step","repeat","step_in_repeat","move_label",
                            "pos_label","pos_cm","rot_token","rot_cm_s","rot_dir",
                            "rot_duration_s","interval_s","mode","dither"])
                for mv in moves:
                    mode = compute_mode(mv.rot_cm_s, self.hybrid_en.isChecked(),
                                        self.stealth_max.value(), self.spread_min.value())
                    dither = self.dither_en.isChecked() and (mv.rot_cm_s >= self.dither_min.value())
                    w.writerow([mv.global_step, mv.repeat_index, mv.step_in_repeat, mv.move_label,
                                mv.pos_label, mv.pos_cm, mv.rot_token, mv.rot_cm_s, mv.rot_dir,
                                self.rot_duration.value(), float(getattr(mv, "interval_s", self.interval.value())),
                                mode, int(dither)])

            self.log.appendPlainText(f"Exported CSV: {fn}")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Export error", str(e))

    def build_cfg_lines(self) -> list[str]:
        lines = []
        lines.append(f"CFG WHEEL_D_CM {self.wheel_d.value():.4f}")
        lines.append(f"CFG LIN_STEPS_PER_MM {self.lin_steps_mm.value():.6f}")
        lines.append(f"CFG LIN_HOME_CM_S {self.lin_home_cm_s.value():.4f}")
        lines.append(f"CFG LIN_MOVE_CM_S {self.lin_move_cm_s.value():.4f}")
        lines.append(f"CFG LIN_OFFSET_CM0 {self.lin_offset0.value():.3f}")

        lines.append(f"CFG ROT_MICROSTEPS {int(self.rot_micro.currentText())}")
        lines.append(f"CFG LIN_MICROSTEPS 16")

        lines.append(f"CFG ROT_DUR_S {self.rot_duration.value():.4f}")
        lines.append(f"CFG INTERVAL_S {self.interval.value():.4f}")

        lines.append(f"CFG HYBRID_EN {1 if self.hybrid_en.isChecked() else 0}")
        lines.append(f"CFG STEALTH_MAX {self.stealth_max.value():.4f}")
        lines.append(f"CFG SPREAD_MIN {self.spread_min.value():.4f}")

        lines.append(f"CFG ROT_I_STEALTH {self.rot_i_stealth.value()}")
        lines.append(f"CFG ROT_I_SPREAD {self.rot_i_spread.value()}")
        lines.append(f"CFG LIN_I {self.lin_i.value()}")

        lines.append(f"CFG SPREAD_PRESET {int(self.spread_preset.currentText())}")

        lines.append(f"CFG DITHER_EN {1 if self.dither_en.isChecked() else 0}")
        lines.append(f"CFG DITHER_MIN {self.dither_min.value():.4f}")
        lines.append(f"CFG DITHER_AMP {self.dither_amp.value():.6f}")
        lines.append(f"CFG DITHER_HZ {self.dither_hz.value():.3f}")
        lines.append(f"CFG DITHER_US {self.dither_us.value()}")

        return lines

    # ---------- Run control ----------
    def start_run(self):
        try:
            if self.port_combo.currentIndex() < 0:
                raise ValueError("Select a COM port.")

            port = self.port_combo.currentData()
            moves, seed_used = self.generate_full_plan()
            if not moves:
                raise ValueError("No moves generated. Build movement list first.")

            cfg_lines = self.build_cfg_lines()

            self.log.appendPlainText(f"Starting run: {len(moves)} moves. seed='{seed_used}'")
            self.log.appendPlainText("First 10 moves:")
            for mv in moves[:10]:
                if is_recalibration_label(mv.pos_label):
                    self.log.appendPlainText(f"  {mv.global_step}: recalibration")
                else:
                    self.log.appendPlainText(f"  {mv.global_step}: {mv.move_label}  lin={mv.pos_cm}  rot={mv.rot_cm_s}{mv.rot_dir}")

            self.start_btn.setEnabled(False)
            self.abort_btn.setEnabled(True)
            self.export_btn.setEnabled(False)
            self.build_btn.setEnabled(False)

            self.thread = QtCore.QThread()
            self.worker = RunWorker(port, cfg_lines, moves)
            self.worker.moveToThread(self.thread)

            self.worker.log.connect(self.log.appendPlainText)
            self.worker.progress.connect(self.on_progress)
            self.worker.finished.connect(self.on_finished)
            self.thread.started.connect(self.worker.run)

            self.thread.start()

        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Start error", str(e))

    def abort_run(self):
        if self.worker:
            self.worker.abort()
            self.log.appendPlainText("Abort requested...")

    def on_progress(self, done: int, total: int, status: str):
        self.progress_label.setText(f"{done}/{total}  {status}")

    def on_finished(self, ok: bool, msg: str):
        self.log.appendPlainText(msg)
        self.progress_label.setText(msg)

        self.start_btn.setEnabled(True)
        self.abort_btn.setEnabled(False)
        self.export_btn.setEnabled(True)
        self.build_btn.setEnabled(True)

        if self.thread:
            self.thread.quit()
            self.thread.wait(2000)
        self.thread = None
        self.worker = None


def main():
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    w.resize(1300, 900)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
