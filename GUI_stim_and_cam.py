from __future__ import annotations

import csv
import ctypes
import datetime as dt
import hashlib
import json
import os
import queue
import random
import re
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any

from PySide6 import QtCore, QtWidgets

import GUI_for_smyrator as stim_gui
import NeuropixelsGUI as cam_gui


APP_DIR = Path(__file__).resolve().parent
ROOT_STORAGE_DIR = Path(os.environ.get("NEUROPIXELS_GUI_STORAGE_DIR", Path.home() / "Neuropixels_GUI_data"))
DEFAULT_SESSIONS_DIR = ROOT_STORAGE_DIR / "Recording_sessions"
DEFAULT_SETTINGS_DIR = ROOT_STORAGE_DIR / "Saved_GUI_settings"
CONFIG_BASENAME = "gui_stim_and_cam_config"
DIRECTORY_STATE_PATH = APP_DIR / "startup_directories.json"
SESSION_NAME_SUFFIX = "stim_and_cam"
RUN_SESSION_RE = re.compile(r"^Run_(\d+)_stim_and_cam$")
MOVE_START_RE = re.compile(r"\bMOVE_START\s+(\d+)/")
MOVE_DONE_RE = re.compile(r"\bMOVE_DONE\s+(\d+)\b")
EXPECTED_CAMERA_FPS = 50.0
EXPECTED_CAMERA_FPS_TOLERANCE = 5.0
CAMERA_SETTLE_DELAY_S = 1.0
DEFAULT_TTL_CALIBRATION_PPM = -1387
FPS_ESTIMATE_MIN_FRAMES = 24
FPS_ESTIMATE_MIN_DURATION_S = 0.75
CAMERA_FRAME_BUFFER_SIZE = 450
CAMERA_QUEUE_PUT_TIMEOUT_S = 0.25
CAMERA_THREAD_JOIN_TIMEOUT_S = 60.0
CAMERA_STOP_DRAIN_IDLE_S = 1.0
CAMERA_STOP_DRAIN_MAX_S = 5.0
ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
NORMAL_PRIORITY_CLASS = 0x00000020
RECORDING_FORMAT_FFV1_GRAY_MKV = "ffv1_gray_mkv"
RECORDING_FORMAT_HEVC_YUV420P_MP4 = "hevc_nvenc_yuv420p_mp4"
DEFAULT_RECORDING_FORMAT = RECORDING_FORMAT_FFV1_GRAY_MKV
RECORDING_FORMAT_OPTIONS = [
    (
        RECORDING_FORMAT_FFV1_GRAY_MKV,
        "FFV1 lossless grayscale MKV (default; scientific, VLC-compatible)",
    ),
    (
        RECORDING_FORMAT_HEVC_YUV420P_MP4,
        "Legacy HEVC MP4/yuv420p (smaller viewing file; heavier live encoding)",
    ),
]
NO_BRUSHING_LABEL = "no_brushing"
LEGACY_NO_BRUSHING_LABELS = {"null", "null_brushing"}
NO_ROTATION_PREFIX = "no_rotation_"
NO_ROTATION_TOKEN = "no_rotation"
LEGACY_NO_ROTATION_PREFIX = "null_rotating_"
LEGACY_NO_ROTATION_TOKEN = "null_rotating"
BRUSH_ORDER_POLICY = "first_entry_is_lowest_cm_earliest_linear_position"
DEFAULT_RESUME_GLOBAL_STEP = 1


def combo_text(combo: QtWidgets.QComboBox) -> str:
    data = combo.currentData()
    if data is not None:
        return str(data)
    return combo.currentText().strip()


def set_combo(combo: QtWidgets.QComboBox, value: str) -> None:
    value = str(value or "").strip()
    if not value:
        return
    for idx in range(combo.count()):
        if str(combo.itemData(idx)) == value or combo.itemText(idx).strip() == value:
            combo.setCurrentIndex(idx)
            return
        if combo.itemText(idx).strip().startswith(value):
            combo.setCurrentIndex(idx)
            return
    if combo.isEditable():
        combo.setEditText(value)
    else:
        combo.addItem(value, value)
        combo.setCurrentIndex(combo.count() - 1)


def infer_resume_global_step_from_events(path: Path) -> int:
    last_started = 0
    last_done = 0
    try:
        with Path(path).open("r", newline="", encoding="utf-8", errors="replace") as handle:
            for row in csv.DictReader(handle):
                message = str(row.get("message", ""))
                start_match = MOVE_START_RE.search(message)
                if start_match:
                    last_started = max(last_started, int(start_match.group(1)))
                    continue
                done_match = MOVE_DONE_RE.search(message)
                if done_match:
                    last_done = max(last_done, int(done_match.group(1)))
                    continue
                if str(row.get("event_type", "")) == "progress":
                    try:
                        done = int(float(str(row.get("done", "") or "0")))
                    except ValueError:
                        done = 0
                    last_done = max(last_done, done)
    except Exception:
        return DEFAULT_RESUME_GLOBAL_STEP

    if last_started > last_done:
        return max(DEFAULT_RESUME_GLOBAL_STEP, last_started)
    if last_done > 0:
        return max(DEFAULT_RESUME_GLOBAL_STEP, last_done + 1)
    return DEFAULT_RESUME_GLOBAL_STEP


def normalize_recording_format(value: Any) -> str:
    text = str(value or "").strip()
    valid = {format_id for format_id, _ in RECORDING_FORMAT_OPTIONS}
    if text in valid:
        return text
    for format_id, label in RECORDING_FORMAT_OPTIONS:
        if text == label or label.startswith(text):
            return format_id
    return DEFAULT_RECORDING_FORMAT


def recording_format_label(value: Any) -> str:
    recording_format = normalize_recording_format(value)
    for format_id, label in RECORDING_FORMAT_OPTIONS:
        if format_id == recording_format:
            return label
    return recording_format


def selected_recording_format(combo: Any) -> str:
    if isinstance(combo, QtWidgets.QComboBox):
        data = combo.currentData()
        if data is not None:
            return normalize_recording_format(data)
        return normalize_recording_format(combo.currentText())
    return DEFAULT_RECORDING_FORMAT


def ttl_calibration_ppm_value(source: Any) -> int:
    value = getattr(source, "ttl_calibration_ppm", source)
    if hasattr(value, "value"):
        value = value.value()
    try:
        return int(value)
    except (TypeError, ValueError):
        return DEFAULT_TTL_CALIBRATION_PPM


def ttl_calibration_enabled_value(source: Any) -> bool:
    value = getattr(source, "ttl_calibration_enabled", False)
    if hasattr(value, "isChecked"):
        return bool(value.isChecked())
    return bool(value)


def recording_format_extension(recording_format: Any) -> str:
    if normalize_recording_format(recording_format) == RECORDING_FORMAT_FFV1_GRAY_MKV:
        return ".mkv"
    return ".mp4"


def set_writer_recording_format(writer: Any, recording_format: Any) -> None:
    fmt = normalize_recording_format(recording_format)
    writer.recording_format = fmt
    writer.path = writer.folder / f"{writer.stem}{recording_format_extension(fmt)}"


def normalize_position_label(label: str) -> str:
    text = str(label or "").strip()
    if text.lower() == NO_BRUSHING_LABEL or text.lower() in LEGACY_NO_BRUSHING_LABELS:
        return NO_BRUSHING_LABEL
    return text


def is_null_brushing_label(label: str) -> bool:
    return normalize_position_label(label).lower() == NO_BRUSHING_LABEL


def null_rotating_token(source_rot_token: str) -> str:
    source = str(source_rot_token).strip()
    return f"{NO_ROTATION_PREFIX}{source}" if source else NO_ROTATION_TOKEN


def normalize_rot_token(rot_token: str) -> str:
    token = str(rot_token or "").strip()
    lower = token.lower()
    if lower == NO_ROTATION_TOKEN:
        return NO_ROTATION_TOKEN
    if lower.startswith(NO_ROTATION_PREFIX):
        return f"{NO_ROTATION_PREFIX}{token[len(NO_ROTATION_PREFIX):]}"
    if lower == LEGACY_NO_ROTATION_TOKEN:
        return NO_ROTATION_TOKEN
    if lower.startswith(LEGACY_NO_ROTATION_PREFIX):
        return f"{NO_ROTATION_PREFIX}{token[len(LEGACY_NO_ROTATION_PREFIX):]}"
    return token


def is_null_rotating_token(rot_token: str) -> bool:
    token = normalize_rot_token(rot_token).lower()
    return token == NO_ROTATION_TOKEN or token.startswith(NO_ROTATION_PREFIX)


def paired_rot_token(rot_token: str) -> str:
    token = normalize_rot_token(rot_token)
    if not is_null_rotating_token(token):
        return ""
    if token.lower() == NO_ROTATION_TOKEN:
        return ""
    return token[len(NO_ROTATION_PREFIX):]


def move_condition(move: stim_gui.Move) -> str:
    if stim_gui.is_recalibration_label(move.pos_label):
        return "recalibration"
    null_brushing = is_null_brushing_label(move.pos_label)
    null_rotating = is_null_rotating_token(move.rot_token)
    if null_brushing and null_rotating:
        return "null_stimulus"
    if null_brushing:
        return "no_brushing"
    if null_rotating:
        return "no_rotation"
    return "brushing"


def move_display_label(move: stim_gui.Move) -> str:
    if stim_gui.is_recalibration_label(move.pos_label):
        return "recalibration"
    if is_null_rotating_token(move.rot_token):
        if is_null_brushing_label(move.pos_label):
            return "null_stimulus"
        source = paired_rot_token(move.rot_token)
        suffix = f" paired with {source}" if source else ""
        return f"{normalize_position_label(move.pos_label)} no_rotation{suffix}"
    return f"{normalize_position_label(move.pos_label)}{normalize_rot_token(move.rot_token)}"


def move_saved_label(move: stim_gui.Move) -> str:
    if stim_gui.is_recalibration_label(move.pos_label):
        return f"r{move.repeat_index}_recalibration"
    if is_null_brushing_label(move.pos_label) and is_null_rotating_token(move.rot_token):
        return f"r{move.repeat_index}_null_stimulus"
    return f"r{move.repeat_index}_{normalize_position_label(move.pos_label)}{normalize_rot_token(move.rot_token)}"


def move_interval_s(move: stim_gui.Move, fallback: float = 0.0) -> float:
    try:
        value = float(getattr(move, "interval_s", fallback))
    except Exception:
        return float(fallback)
    return float(fallback) if value < 0.0 else value


def next_default_session_name(output_root: Path) -> str:
    highest = -1
    try:
        for child in output_root.iterdir():
            if not child.is_dir():
                continue
            match = RUN_SESSION_RE.match(child.name)
            if match:
                highest = max(highest, int(match.group(1)))
    except Exception:
        pass
    return f"Run_{highest + 1}_{SESSION_NAME_SUFFIX}"


def take_central(window: QtWidgets.QMainWindow) -> QtWidgets.QWidget:
    if hasattr(window, "takeCentralWidget"):
        widget = window.takeCentralWidget()
    else:
        widget = window.centralWidget()
        if widget is not None:
            widget.setParent(None)
    if widget is None:
        raise RuntimeError(f"Could not take central widget from {type(window).__name__}")
    return widget


def apply_dark_theme(app: QtWidgets.QApplication) -> None:
    app.setStyleSheet(
        """
        QWidget { background:#161b22; color:#d7e7f5; font-size:10pt; }
        QTabWidget::pane { border:1px solid #2d3a4c; border-radius:6px; }
        QTabBar::tab { background:#111820; border:1px solid #2d3a4c; padding:8px 14px; }
        QTabBar::tab:selected { background:#24415a; }
        QGroupBox { border:1px solid #2d3a4c; border-radius:7px; margin-top:8px; padding:8px; }
        QGroupBox::title { subcontrol-origin: margin; left:10px; padding:0 4px; }
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QListWidget, QTableWidget {
            background:#0f131a; border:1px solid #2d3a4c; border-radius:5px; padding:4px;
        }
        QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {
            background:#1b222d; color:#718092; border-color:#263241;
        }
        QLabel:disabled { color:#718092; }
        QTableWidget { gridline-color:#2d3a4c; selection-background-color:#24415a; }
        QHeaderView::section {
            background:#111820; color:#d7e7f5; border:1px solid #2d3a4c; padding:5px 6px;
        }
        QPushButton { background:#24415a; border:1px solid #41627f; border-radius:6px; padding:7px 12px; }
        QPushButton:hover { background:#2d526f; }
        QPushButton:disabled { color:#718092; background:#1b222d; }
        QCheckBox, QRadioButton { spacing:8px; }
        """
    )


_CAMERA_PATCHED = False


def read_float_node(nodemap: Any, name: str) -> float | None:
    try:
        node = cam_gui.PySpin.CFloatPtr(nodemap.GetNode(name))
        if cam_gui.PySpin.IsReadable(node):
            return float(node.GetValue())
    except Exception:
        pass
    return None


def read_bool_node(nodemap: Any, name: str) -> bool | None:
    try:
        node = cam_gui.PySpin.CBooleanPtr(nodemap.GetNode(name))
        if cam_gui.PySpin.IsReadable(node):
            return bool(node.GetValue())
    except Exception:
        pass
    return None


def read_camera_saved_fps(nodemap: Any) -> float:
    for name in ("AcquisitionFrameRate", "AcquisitionResultingFrameRate"):
        value = read_float_node(nodemap, name)
        if value and value > 0:
            return float(value)
    return 50.0


def set_camera_acquisition_fps_for_ttl_run(nodemap: Any, requested_fps: float) -> dict[str, Any]:
    requested_fps = float(requested_fps)
    saved = {
        "acquisition_frame_rate_enable": read_bool_node(nodemap, "AcquisitionFrameRateEnable"),
        "acquisition_frame_rate": read_float_node(nodemap, "AcquisitionFrameRate"),
        "acquisition_resulting_frame_rate": read_float_node(nodemap, "AcquisitionResultingFrameRate"),
    }

    cam_gui.try_set_enum(nodemap, "TriggerMode", "Off")
    cam_gui.try_set_bool(nodemap, "AcquisitionFrameRateEnable", True)

    node = cam_gui.PySpin.CFloatPtr(nodemap.GetNode("AcquisitionFrameRate"))
    if not cam_gui.PySpin.IsReadable(node) or not cam_gui.PySpin.IsWritable(node):
        raise RuntimeError("Camera AcquisitionFrameRate is not writable; cannot apply UI FPS.")

    fps_min = float(node.GetMin())
    fps_max = float(node.GetMax())
    if requested_fps < fps_min - 0.01 or requested_fps > fps_max + 0.01:
        raise RuntimeError(
            f"Requested UI FPS {requested_fps:.3f} is outside this camera's writable range "
            f"{fps_min:.3f}-{fps_max:.3f}. Reduce exposure/ROI in SpinView or choose a valid FPS."
        )

    node.SetValue(requested_fps)
    actual_fps = float(node.GetValue())
    resulting_fps = read_float_node(nodemap, "AcquisitionResultingFrameRate") or actual_fps
    if abs(actual_fps - requested_fps) > 0.25:
        raise RuntimeError(
            f"Camera accepted AcquisitionFrameRate={actual_fps:.3f}, not requested UI FPS {requested_fps:.3f}."
        )
    if resulting_fps + 0.25 < requested_fps:
        raise RuntimeError(
            f"Camera resulting FPS is {resulting_fps:.3f}, below requested TTL/UI FPS {requested_fps:.3f}. "
            "Reduce exposure/ROI in SpinView or lower the GUI frequency."
        )

    return {
        **saved,
        "requested_fps": requested_fps,
        "actual_fps": actual_fps,
        "resulting_fps": resulting_fps,
        "min_fps": fps_min,
        "max_fps": fps_max,
    }


def restore_camera_acquisition_fps_state(nodemap: Any, saved_state: dict[str, Any]) -> None:
    cam_gui.try_set_enum(nodemap, "TriggerMode", "Off")
    saved_fps = saved_state.get("acquisition_frame_rate")
    if saved_fps is not None:
        try:
            cam_gui.try_set_bool(nodemap, "AcquisitionFrameRateEnable", True)
            cam_gui.try_set_float(nodemap, "AcquisitionFrameRate", float(saved_fps))
        except Exception:
            pass
    saved_enable = saved_state.get("acquisition_frame_rate_enable")
    if saved_enable is not None:
        try:
            cam_gui.try_set_bool(nodemap, "AcquisitionFrameRateEnable", bool(saved_enable))
        except Exception:
            pass


def read_int_node(nodemap: Any, name: str) -> int | None:
    try:
        node = cam_gui.PySpin.CIntegerPtr(nodemap.GetNode(name))
        if cam_gui.PySpin.IsReadable(node):
            return int(node.GetValue())
    except Exception:
        pass
    return None


def configure_camera_stream_buffering(cam: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "python_frame_queue_size": CAMERA_FRAME_BUFFER_SIZE,
        "stream_buffer_target": CAMERA_FRAME_BUFFER_SIZE,
    }
    try:
        nodemap = cam.GetTLStreamNodeMap()
        state["stream_buffer_handling_before"] = cam_gui.get_enum_value(nodemap, "StreamBufferHandlingMode")
        state["stream_buffer_count_before"] = read_int_node(nodemap, "StreamBufferCountManual")
        state["stream_buffer_count_mode_manual"] = cam_gui.try_set_enum(nodemap, "StreamBufferCountMode", "Manual")
        state["stream_buffer_count_set"] = cam_gui.try_set_int(
            nodemap,
            "StreamBufferCountManual",
            CAMERA_FRAME_BUFFER_SIZE,
        )
        state["stream_buffer_handling_oldest_first"] = cam_gui.try_set_enum(
            nodemap,
            "StreamBufferHandlingMode",
            "OldestFirst",
        )
        state["stream_buffer_handling_after"] = cam_gui.get_enum_value(nodemap, "StreamBufferHandlingMode")
        state["stream_buffer_count_after"] = read_int_node(nodemap, "StreamBufferCountManual")
    except Exception as exc:
        state["stream_buffer_error"] = str(exc)
    return state


def camera_state_snapshot(nodemap: Any) -> dict[str, Any]:
    return {
        "acquisition_mode": cam_gui.get_enum_value(nodemap, "AcquisitionMode") or "-",
        "trigger_selector": cam_gui.get_enum_value(nodemap, "TriggerSelector") or "-",
        "trigger_mode": cam_gui.get_enum_value(nodemap, "TriggerMode") or "-",
        "trigger_source": cam_gui.get_enum_value(nodemap, "TriggerSource") or "-",
        "trigger_activation": cam_gui.get_enum_value(nodemap, "TriggerActivation") or "-",
        "trigger_overlap": cam_gui.get_enum_value(nodemap, "TriggerOverlap") or "-",
        "line_selector": cam_gui.get_enum_value(nodemap, "LineSelector") or "-",
        "line_mode": cam_gui.get_enum_value(nodemap, "LineMode") or "-",
        "pixel_format": cam_gui.get_enum_value(nodemap, "PixelFormat") or "-",
        "fps": read_camera_saved_fps(nodemap),
        "width": read_int_node(nodemap, "Width") or 0,
        "height": read_int_node(nodemap, "Height") or 0,
        "offset_x": read_int_node(nodemap, "OffsetX") or 0,
        "offset_y": read_int_node(nodemap, "OffsetY") or 0,
    }


def validate_preserved_camera_state(state: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    fps = float(state.get("fps") or 0.0)
    if fps <= 0:
        problems.append("Acquisition FPS could not be read from the active camera state")

    return problems


def estimate_capture_fps_from_timestamps(timestamps_ns: list[int], fallback_fps: float) -> float:
    if len(timestamps_ns) >= 2:
        duration_s = (int(timestamps_ns[-1]) - int(timestamps_ns[0])) / 1e9
        if duration_s > 0:
            estimated = (len(timestamps_ns) - 1) / duration_s
            if estimated > 0:
                return float(estimated)
    return float(fallback_fps)


def tail_text(path: Path, max_lines: int = 18, max_chars: int = 1800) -> str:
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    text = " | ".join(lines[-max_lines:])
    if len(text) > max_chars:
        text = "..." + text[-max_chars:]
    return text


def get_windows_process_priority_class() -> int | None:
    if sys.platform != "win32":
        return None
    try:
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        value = int(ctypes.windll.kernel32.GetPriorityClass(handle))
        return value or None
    except Exception:
        return None


def set_windows_process_priority_class(priority_class: int) -> bool:
    if sys.platform != "win32":
        return False
    try:
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        return bool(ctypes.windll.kernel32.SetPriorityClass(handle, int(priority_class)))
    except Exception:
        return False


def normalize_camera_trigger_for_ttl_run(nodemap: Any, saved_state: dict[str, Any]) -> dict[str, Any]:
    desired_activation = str(saved_state.get("trigger_activation") or "").strip() or "RisingEdge"

    cam_gui.try_set_enum(nodemap, "TriggerMode", "Off")
    cam_gui.try_set_enum(nodemap, "TriggerSelector", "FrameStart")
    cam_gui.try_set_enum(nodemap, "LineSelector", "Line0")
    cam_gui.try_set_enum(nodemap, "LineMode", "Input")
    cam_gui.try_set_enum(nodemap, "TriggerOverlap", "ReadOut")

    if not cam_gui.try_set_enum(nodemap, "TriggerSource", "Line0"):
        raise RuntimeError("Could not set camera TriggerSource=Line0 for TTL run")
    if not cam_gui.try_set_enum(nodemap, "TriggerActivation", desired_activation):
        raise RuntimeError(f"Could not set camera TriggerActivation={desired_activation!r} for TTL run")
    if not cam_gui.try_set_enum(nodemap, "TriggerMode", "On"):
        raise RuntimeError("Could not re-enable camera TriggerMode=On for TTL run")

    state = camera_state_snapshot(nodemap)
    if (
        state.get("trigger_mode") != "On"
        or state.get("trigger_source") != "Line0"
        or state.get("trigger_selector") != "FrameStart"
    ):
        raise RuntimeError(
            "Trigger normalization failed: "
            f"TriggerSelector={state.get('trigger_selector') or '-'} "
            f"TriggerMode={state.get('trigger_mode') or '-'} "
            f"TriggerSource={state.get('trigger_source') or '-'}"
        )
    return state


def restore_camera_trigger_state(nodemap: Any, saved_state: dict[str, Any]) -> None:
    saved_selector = str(saved_state.get("trigger_selector") or "").strip()
    saved_mode = str(saved_state.get("trigger_mode") or "").strip()
    saved_source = str(saved_state.get("trigger_source") or "").strip()
    saved_activation = str(saved_state.get("trigger_activation") or "").strip()
    saved_overlap = str(saved_state.get("trigger_overlap") or "").strip()
    saved_line_selector = str(saved_state.get("line_selector") or "").strip()
    saved_line_mode = str(saved_state.get("line_mode") or "").strip()

    cam_gui.try_set_enum(nodemap, "TriggerMode", "Off")
    if saved_selector and saved_selector != "-":
        cam_gui.try_set_enum(nodemap, "TriggerSelector", saved_selector)
    if saved_line_selector and saved_line_selector != "-":
        cam_gui.try_set_enum(nodemap, "LineSelector", saved_line_selector)
    if saved_line_mode and saved_line_mode != "-":
        cam_gui.try_set_enum(nodemap, "LineMode", saved_line_mode)
    if saved_overlap and saved_overlap != "-":
        cam_gui.try_set_enum(nodemap, "TriggerOverlap", saved_overlap)
    if saved_source and saved_source != "-":
        cam_gui.try_set_enum(nodemap, "TriggerSource", saved_source)
    if saved_activation and saved_activation != "-":
        cam_gui.try_set_enum(nodemap, "TriggerActivation", saved_activation)
    if saved_mode and saved_mode != "-":
        cam_gui.try_set_enum(nodemap, "TriggerMode", saved_mode)


def configure_preview_free_run_preserve_internal(nodemap: Any, preview_fps: float) -> dict[str, Any]:
    saved_state = {
        "camera_state": camera_state_snapshot(nodemap),
        "fps_state": {
            "acquisition_frame_rate_enable": read_bool_node(nodemap, "AcquisitionFrameRateEnable"),
            "acquisition_frame_rate": read_float_node(nodemap, "AcquisitionFrameRate"),
        },
    }

    cam_gui.try_set_enum(nodemap, "TriggerMode", "Off")
    cam_gui.try_set_enum(nodemap, "AcquisitionMode", "Continuous")
    cam_gui.try_set_bool(nodemap, "AcquisitionFrameRateEnable", True)
    cam_gui.try_set_float(nodemap, "AcquisitionFrameRate", float(preview_fps))

    preview_state = camera_state_snapshot(nodemap)
    if preview_state.get("trigger_mode") != "Off":
        raise RuntimeError(
            "Preview could not disable camera trigger mode. Live preview needs TriggerMode=Off so it can acquire without TTL pulses."
        )
    return saved_state


def restore_preview_camera_state(nodemap: Any, saved_state: dict[str, Any]) -> None:
    camera_state = saved_state.get("camera_state", {}) if isinstance(saved_state, dict) else {}
    fps_state = saved_state.get("fps_state", {}) if isinstance(saved_state, dict) else {}

    restore_camera_acquisition_fps_state(nodemap, fps_state)
    acquisition_mode = str(camera_state.get("acquisition_mode") or "").strip()
    if acquisition_mode and acquisition_mode != "-":
        cam_gui.try_set_enum(nodemap, "AcquisitionMode", acquisition_mode)
    restore_camera_trigger_state(nodemap, camera_state)


def patch_camera_runtime_for_internal_camera_settings() -> None:
    global _CAMERA_PATCHED
    if _CAMERA_PATCHED:
        return

    def configure_free_run_preserve_internal(
        nodemap: Any,
        preview_fps: int,
        width: int,
        height: int,
    ) -> None:
        del width, height
        configure_preview_free_run_preserve_internal(nodemap, preview_fps)

    def configure_hardware_trigger_preserve_internal(
        nodemap: Any,
        width: int,
        height: int,
        trigger_source: str,
        trigger_activation: str,
    ) -> None:
        del nodemap, width, height, trigger_source, trigger_activation

    def release_camera_preserve_current_state(cam: Any, nodemap: Any) -> None:
        del nodemap
        try:
            cam.EndAcquisition()
        except Exception:
            pass
        try:
            cam.DeInit()
        except Exception:
            pass

    def ttl_run_match_working_notebook(self: Any) -> None:
        if cam_gui.serial is None:
            self.finished.emit(False, f"pyserial unavailable: {cam_gui.SERIAL_IMPORT_ERROR}")
            return

        try:
            arm_delay_s = float(getattr(self, "arm_delay_s", 0.0) or 0.0)
            if arm_delay_s > 0:
                self.log.emit(f"TTL settle delay: waiting {arm_delay_s:.1f}s after camera arm.")
                deadline = cam_gui.time.monotonic() + arm_delay_s
                while not self._stop.is_set() and cam_gui.time.monotonic() < deadline:
                    cam_gui.time.sleep(0.05)
                if self._stop.is_set():
                    self.finished.emit(False, "Stopped")
                    return

            with cam_gui.serial.Serial(self.port, self.baud, timeout=1.0) as ser:
                self._ser = ser
                cam_gui.time.sleep(2.0)

                command_count = self.count
                cmd = f"{self.freq_hz} {self.pulse_ms} {command_count}"
                ttl_calibration_ppm = ttl_calibration_ppm_value(self)
                ttl_calibration_enabled = ttl_calibration_enabled_value(self)

                expected_s = (command_count / max(1, self.freq_hz)) + 5.0
                deadline = cam_gui.time.time() + (expected_s * 2.0)
                acknowledged = False
                completed = False
                attempts = 0
                max_attempts = 3

                def drain_serial(seconds: float) -> None:
                    drain_deadline = cam_gui.time.time() + seconds
                    while not self._stop.is_set() and cam_gui.time.time() < drain_deadline:
                        line = ser.readline().decode(errors="ignore").strip()
                        if line:
                            self.log.emit(f"TTL Arduino: {line}")

                def stop_ttl_output() -> None:
                    ser.write(b"STOP\n")
                    try:
                        ser.flush()
                    except Exception:
                        pass
                    drain_serial(0.75)
                    try:
                        ser.reset_input_buffer()
                    except Exception:
                        pass

                def send_calibration_command(ppm: int) -> bool:
                    cal_cmd = f"CAL {int(ppm)}"
                    expected_cal_reply = f"CAL {int(ppm)}"
                    self.log.emit(f"TTL calibration: {int(ppm)} ppm")
                    ser.write((cal_cmd + "\n").encode("ascii"))
                    try:
                        ser.flush()
                    except Exception:
                        pass

                    cal_deadline = cam_gui.time.time() + 2.0
                    while not self._stop.is_set() and cam_gui.time.time() < cal_deadline:
                        line = ser.readline().decode(errors="ignore").strip()
                        if not line:
                            continue
                        self.log.emit(f"TTL Arduino: {line}")
                        if line == expected_cal_reply:
                            return True
                        if line.startswith("ERR"):
                            self.finished.emit(False, f"TTL calibration failed: {line}")
                            return False

                    self.finished.emit(False, "TTL Arduino did not acknowledge the calibration command")
                    return False

                def send_ttl_command() -> None:
                    self.log.emit(f"TTL command: {cmd}")
                    ser.write((cmd + "\n").encode("ascii"))
                    try:
                        ser.flush()
                    except Exception:
                        pass

                try:
                    ser.reset_input_buffer()
                except Exception:
                    pass
                stop_ttl_output()
                if ttl_calibration_enabled and not send_calibration_command(ttl_calibration_ppm):
                    return
                send_ttl_command()
                ack_deadline = cam_gui.time.time() + 5.0

                while not self._stop.is_set() and cam_gui.time.time() < deadline:
                    line = ser.readline().decode(errors="ignore").strip()
                    if not line:
                        if not acknowledged and cam_gui.time.time() >= ack_deadline:
                            if attempts < max_attempts:
                                attempts += 1
                                self.log.emit(
                                    "TTL Arduino did not acknowledge command; sending STOP and retrying TTL command."
                                )
                                stop_ttl_output()
                                send_ttl_command()
                                ack_deadline = cam_gui.time.time() + 5.0
                                continue
                            self.finished.emit(
                                False,
                                f"TTL Arduino on {self.port} did not acknowledge the frame TTL command; no camera frame TTLs were generated.",
                            )
                            return
                        continue
                    self.log.emit(f"TTL Arduino: {line}")
                    if line.startswith("ERR"):
                        if "busy" in line.lower() and attempts < max_attempts:
                            attempts += 1
                            self.log.emit("TTL Arduino busy; sending STOP and retrying TTL command.")
                            stop_ttl_output()
                            send_ttl_command()
                            ack_deadline = cam_gui.time.time() + 5.0
                            continue
                        self.finished.emit(False, line)
                        return
                    if line.startswith("RUNNING"):
                        acknowledged = True
                        continue
                    if line == "DONE":
                        acknowledged = True
                        completed = True
                        break

                if not acknowledged:
                    self.finished.emit(False, "Stopped" if self._stop.is_set() else "Timeout waiting for TTL Arduino acknowledgement")
                    return
                if self._stop.is_set():
                    self.finished.emit(False, "Stopped")
                    return
                if not completed:
                    self.finished.emit(False, "Timeout waiting for DONE from TTL Arduino")
                    return

                self.finished.emit(True, "DONE from Arduino serial; frame-trigger TTL train completed")

        except Exception as exc:
            self.finished.emit(False, str(exc))

        finally:
            self._ser = None

    def maybe_start_ttl_match_working_notebook(self: Any) -> None:
        if self.ttl_started or not (self.camera_armed and self.stim_ready):
            return

        port = cam_gui.combo_value(self.trigger_port)
        if not port:
            self.log("No trigger COM port selected.")
            self.stop_all()
            return
        if port not in cam_gui.available_ports():
            self.log(f"Trigger COM port {port} is not available.")
            self.stop_all()
            return

        self.ttl_started = True
        self.ttl_thread = cam_gui.TTLThread(port, self.freq.value(), self.pulse_ms.value(), self.ttl_count())
        self.ttl_thread.arm_delay_s = CAMERA_SETTLE_DELAY_S if self.use_cameras.isChecked() else 0.0
        self.ttl_thread.ttl_calibration_ppm = ttl_calibration_ppm_value(self)
        self.ttl_thread.ttl_calibration_enabled = ttl_calibration_enabled_value(self)
        self.ttl_thread.log.connect(self.log)
        self.ttl_thread.finished.connect(self.on_ttl_finished)
        self.ttl_thread.start()
        self.write_metadata("running")
        if self.ttl_thread.arm_delay_s > 0:
            self.log(f"TTL started with {self.ttl_thread.arm_delay_s:.1f}s camera settle delay.")
        else:
            self.log("TTL started.")

    original_on_ttl_finished = cam_gui.MainWindow.on_ttl_finished

    def on_ttl_finished_after_start_pulse(self: Any, ok: bool, message: str) -> None:
        owner = getattr(self, "_combined_owner", None)
        if owner is not None and getattr(self, "session_dir", None):
            owner.record_camera_ttl_finished(Path(self.session_dir), ok, message)
        self.log(f"TTL finished: ok={ok} {message}")
        if self.camera_thread and self.camera_thread.isRunning():
            self.camera_thread.stop()
        if owner is not None and getattr(self, "session_dir", None):
            session_dir = Path(self.session_dir)
            owner.request_active_stim_stop(
                "Camera TTL finished; stopping somatosensory stimulation for the same session.",
                session_dir=session_dir,
                stop_target="camera_ttl",
            )
        if not ok and self.stim_thread and self.stim_thread.isRunning():
            self.stim_thread.stop()
        if not self.use_stim.isChecked():
            self.finish_if_idle()

    def toggle_preview_disabled(self: Any) -> None:
        thread = getattr(self, "preview_thread", None)
        if thread is not None and thread.isRunning():
            thread.stop()
            thread.wait(5000)
        self.log("Live preview is disabled in the combined camera + stimulation GUI.")

    original_ffmpeg_start = cam_gui.FfmpegWriter.start
    original_on_cameras_finished = cam_gui.MainWindow.on_cameras_finished
    original_stop_all = cam_gui.MainWindow.stop_all
    original_finish_if_idle = cam_gui.MainWindow.finish_if_idle

    def on_cameras_finished_recording(self: Any, result: dict[str, Any]) -> None:
        owner = getattr(self, "_combined_owner", None)
        if owner is not None and getattr(self, "session_dir", None):
            session_dir = Path(self.session_dir)
            owner.record_camera_finished(session_dir, result)
            owner.request_active_stim_stop(
                "Camera recording finished; stopping somatosensory stimulation for the same session.",
                session_dir=session_dir,
                stop_target="camera",
            )
        original_on_cameras_finished(self, result)

    def stop_all_recording(self: Any) -> None:
        owner = getattr(self, "_combined_owner", None)
        if (
            owner is not None
            and not getattr(owner, "_closing", False)
            and getattr(self, "session_dir", None)
            and self.stop_btn.isEnabled()
        ):
            session_dir = Path(self.session_dir)
            owner.record_camera_stop_requested(session_dir)
            owner.request_active_stim_stop(
                "User requested camera stop; stopping somatosensory stimulation for the same session.",
                session_dir=session_dir,
                stop_target="camera",
            )
        original_stop_all(self)

    def ffmpeg_start_with_above_normal_priority(self: Any) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        ffmpeg_exe = cam_gui.shutil.which("ffmpeg") or "ffmpeg"
        recording_format = normalize_recording_format(getattr(self, "recording_format", DEFAULT_RECORDING_FORMAT))
        set_writer_recording_format(self, recording_format)
        input_args = [
            ffmpeg_exe,
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-s",
            f"{self.width}x{self.height}",
            "-framerate",
            str(self.fps),
            "-i",
            "-",
        ]
        if recording_format == RECORDING_FORMAT_FFV1_GRAY_MKV:
            cmd = [
                *input_args,
                "-c:v",
                "ffv1",
                "-level",
                "3",
                "-g",
                "1",
                "-coder",
                "0",
                "-context",
                "0",
                "-slices",
                "24",
                "-slicecrc",
                "0",
                "-threads",
                "8",
                "-pix_fmt",
                "gray",
                "-an",
                str(self.path),
            ]
        else:
            cmd = [
                *input_args,
                "-vf",
                "format=yuv420p",
                "-c:v",
                "hevc_nvenc",
                "-preset",
                "fast",
                "-tune",
                "hq",
                "-rc:v",
                "vbr",
                "-cq:v",
                "22",
                "-b:v",
                "0",
                "-spatial_aq",
                "1",
                "-aq-strength",
                "4",
                "-force_key_frames",
                "expr:gte(t,n_forced*6)",
                "-an",
                str(self.path),
            ]
        self.log_file = self.log_path.open("a", encoding="utf-8", buffering=1)
        self.log_file.write(f"----- ffmpeg start {dt.datetime.now().isoformat()} -----\n")
        self.log_file.write(f"ffmpeg executable: {ffmpeg_exe}\n")
        self.log_file.write(f"recording format: {recording_format_label(recording_format)}\n")
        self.log_file.write(" ".join(cmd) + "\n")
        self.proc = cam_gui.subprocess.Popen(
            cmd,
            stdin=cam_gui.subprocess.PIPE,
            stdout=self.log_file,
            stderr=self.log_file,
            bufsize=8 * 1024 * 1024,
        )
        cam_gui.time.sleep(0.2)
        if self.proc.poll() is not None:
            log_tail = tail_text(self.log_path)
            raise RuntimeError(f"FFmpeg exited early. See {self.log_path}. {log_tail}")

    def ffmpeg_write_memoryview(self: Any, frame: Any) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("FFmpeg writer is not open")
        contiguous = cam_gui.np.ascontiguousarray(frame)
        self.proc.stdin.write(memoryview(contiguous))

    def start_run_with_above_normal_priority(self: Any) -> None:
        boosted = False
        if sys.platform == "win32" and not getattr(self, "_priority_boost_active", False):
            previous = get_windows_process_priority_class()
            if previous is not None and set_windows_process_priority_class(ABOVE_NORMAL_PRIORITY_CLASS):
                self._priority_boost_active = True
                self._priority_previous_class = previous
                boosted = True
                try:
                    self.log("Process priority set to Above Normal for acquisition.")
                except Exception:
                    pass
        try:
            if self.preview_thread and self.preview_thread.isRunning():
                self.preview_thread.stop()
                self.preview_thread.wait(5000)

            if self.use_cameras.isChecked() and cam_gui.shutil.which("ffmpeg") is None:
                QtWidgets.QMessageBox.critical(self, "Missing FFmpeg", "ffmpeg is not on PATH.")
                return

            if cam_gui.serial is None:
                QtWidgets.QMessageBox.critical(self, "Missing serial", str(cam_gui.SERIAL_IMPORT_ERROR))
                return

            if not self.use_cameras.isChecked() and not self.use_stim.isChecked():
                QtWidgets.QMessageBox.warning(self, "Nothing enabled", "Enable cameras, sensory stimulation, or both.")
                return

            if self.use_cameras.isChecked():
                trigger_port = cam_gui.combo_value(self.trigger_port)
                available = cam_gui.available_ports()
                if not trigger_port or trigger_port not in available:
                    self.refresh_ports()
                    QtWidgets.QMessageBox.critical(
                        self,
                        "Trigger port not available",
                        f"Selected camera trigger port is '{trigger_port or '(none)'}', but available ports are: "
                        f"{', '.join(available) if available else '(none)'}.\n\n"
                        "Select the Arduino/TTL trigger port before starting the camera run.",
                    )
                    return

            owner = getattr(self, "_combined_owner", None)
            session_label = "camera_only"
            if owner is not None:
                session_label = owner.camera_session_label()
            elif self.use_cameras.isChecked() and self.use_stim.isChecked():
                session_label = "stim_and_cam"

            if owner is not None:
                session_name = owner.consume_camera_session_name_for_run()
            else:
                session_name = f"{cam_gui.now_stamp()}_{session_label}"

            self.session_dir = Path(self.output_root.text().strip() or cam_gui.RESULTS_DIR) / session_name
            if self.session_dir.exists():
                QtWidgets.QMessageBox.critical(
                    self,
                    "Session folder exists",
                    f"Choose a different session name. This folder already exists:\n{self.session_dir}",
                )
                return
            self.session_dir.mkdir(parents=True, exist_ok=True)

            self.log_box.clear()
            self.preview_label.setText("Preview")
            self.preview_label.setPixmap(cam_gui.QtGui.QPixmap())

            self.camera_armed = not self.use_cameras.isChecked()
            self.stim_ready = not self.use_stim.isChecked()
            self.ttl_started = False

            self.run_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
            self.preview_btn.setEnabled(False)

            self.write_metadata("starting")
            self.log(f"Session: {self.session_dir}")

            if self.use_cameras.isChecked():
                self.camera_thread = cam_gui.CameraRunThread(
                    self.session_dir,
                    fps=self.freq.value(),
                    width=self.width.value(),
                    height=self.height.value(),
                    preview_fps=self.preview_fps.value(),
                    trigger_source=self.trigger_source,
                    trigger_activation=self.trigger_activation,
                )
                self.camera_thread.recording_format = selected_recording_format(getattr(self, "recording_format", None))
                self.camera_thread.armed.connect(self.on_cameras_armed)
                self.camera_thread.preview.connect(self.set_preview)
                self.camera_thread.log.connect(self.log)
                self.camera_thread.status.connect(lambda s: self.log(f"Camera frames: {s}"))
                if owner is not None:
                    owner.begin_camera_session_tracking(self.session_dir, self.camera_thread)
                self.camera_thread.finished.connect(self.on_cameras_finished)
                self.camera_thread.start()

            if self.use_stim.isChecked():
                try:
                    moves = cam_gui.parse_moves(self.stim_moves.toPlainText())
                except Exception as exc:
                    self.log(f"Stim moves invalid: {exc}")
                    self.stop_all()
                    return

                self.stim_thread = cam_gui.StimThread(
                    cam_gui.combo_value(self.stim_port),
                    cam_gui.clean_lines(self.stim_cfg.toPlainText()),
                    moves,
                )
                self.stim_thread.log.connect(self.log)
                self.stim_thread.ready_for_ttl.connect(self.on_stim_ready)
                self.stim_thread.progress.connect(lambda done, total, status: self.log(f"Stim {done}/{total}: {status}"))
                self.stim_thread.finished.connect(self.on_stim_finished)
                self.stim_thread.start()

            self.maybe_start_ttl()
        except Exception:
            if boosted and getattr(self, "_priority_boost_active", False):
                previous = getattr(self, "_priority_previous_class", None)
                if previous is not None:
                    set_windows_process_priority_class(previous)
                self._priority_boost_active = False
                self._priority_previous_class = None
            raise
        if boosted:
            stop_btn = getattr(self, "stop_btn", None)
            if stop_btn is None or not stop_btn.isEnabled():
                previous = getattr(self, "_priority_previous_class", None)
                if previous is not None:
                    set_windows_process_priority_class(previous)
                self._priority_boost_active = False
                self._priority_previous_class = None
        owner = getattr(self, "_combined_owner", None)
        if owner is not None and getattr(self, "stop_btn", None) and self.stop_btn.isEnabled():
            QtCore.QTimer.singleShot(0, owner.after_camera_run_clicked)

    def finish_if_idle_with_priority_restore(self: Any) -> None:
        original_finish_if_idle(self)
        busy = any(t and t.isRunning() for t in (self.camera_thread, self.ttl_thread, self.stim_thread))
        if busy:
            return
        if getattr(self, "_priority_boost_active", False):
            previous = getattr(self, "_priority_previous_class", None)
            restored = previous is not None and set_windows_process_priority_class(previous)
            self._priority_boost_active = False
            self._priority_previous_class = None
            if restored:
                try:
                    self.log("Process priority restored after acquisition.")
                except Exception:
                    pass

    def preview_run_preserve_current_camera_state(self: Any) -> None:
        if cam_gui.PySpin is None:
            self.log.emit(f"PySpin unavailable: {cam_gui.PYSPIN_IMPORT_ERROR}")
            self.stopped.emit()
            return

        system = cam_gui.PySpin.System.GetInstance()
        cam_list = None
        active: list[tuple[Any, Any, dict[str, Any], Optional[Any], dict[str, Any]]] = []

        try:
            cam_list = system.GetCameras()
            detected = int(cam_list.GetSize())
            self.log.emit(f"Preview: PySpin detected {detected} camera(s).")

            for idx in range(detected):
                cam = cam_list.GetByIndex(idx)
                tl = cam.GetTLDeviceNodeMap()
                info = {
                    "index": idx,
                    "serial": cam_gui.get_string_node(tl, "DeviceSerialNumber"),
                    "model": cam_gui.get_string_node(tl, "DeviceModelName"),
                }
                cam.Init()
                nodemap = cam.GetNodeMap()
                state = camera_state_snapshot(nodemap)
                self.log.emit(
                    f"Preview camera {idx + 1}: "
                    f"serial={info['serial'] or '-'} "
                    f"model={info['model'] or '-'} "
                    f"TriggerSelector={state['trigger_selector']} "
                    f"TriggerMode={state['trigger_mode']} "
                    f"TriggerSource={state['trigger_source']} "
                    f"TriggerActivation={state['trigger_activation']} "
                    f"TriggerOverlap={state['trigger_overlap']} "
                    f"Line={state['line_selector']}:{state['line_mode']} "
                    f"Fps={state['fps']:.3f} "
                    f"ROI={state['width']}x{state['height']} "
                    f"Offset=({state['offset_x']},{state['offset_y']})"
                )
                preview_saved_state = configure_preview_free_run_preserve_internal(nodemap, self.fps)
                preview_state = camera_state_snapshot(nodemap)
                self.log.emit(
                    f"Preview camera {idx + 1} free-run: "
                    f"TriggerMode={preview_state['trigger_mode']} "
                    f"Fps={preview_state['fps']:.3f} "
                    "TTL not required for live preview."
                )
                cam.BeginAcquisition()
                active.append((cam, nodemap, info, None, preview_saved_state))

            if not active:
                self.log.emit("Preview: no cameras available.")
                return

            delay = 1.0 / max(1, self.fps)
            last_emit = 0.0

            while not self._stop.is_set():
                for i, (cam, nodemap, info, last, preview_saved_state) in enumerate(active):
                    image = None
                    try:
                        image = cam.GetNextImage(100)
                        if not image.IsIncomplete():
                            last = cam_gui.image_to_gray(image).copy()
                            active[i] = (cam, nodemap, info, last, preview_saved_state)
                    except cam_gui.PySpin.SpinnakerException as exc:
                        if not cam_gui.is_timeout(exc):
                            self.log.emit(f"Preview {info.get('serial') or info.get('model')}: {exc}")
                    finally:
                        if image is not None:
                            image.Release()

                if cam_gui.time.monotonic() - last_emit >= delay:
                    last_emit = cam_gui.time.monotonic()
                    frame = cam_gui.grid_image(
                        [x[3] for x in active],
                        [f"Cam {x[2]['index'] + 1} {x[2].get('serial') or x[2].get('model')}" for x in active],
                    )
                    if frame is not None:
                        self.preview.emit(cam_gui.gray_to_qimage(frame))

        except Exception as exc:
            self.log.emit(f"Preview error: {exc}")

        finally:
            while active:
                cam, nodemap, info, last, preview_saved_state = active.pop()
                try:
                    cam.EndAcquisition()
                except Exception:
                    pass
                try:
                    restore_preview_camera_state(nodemap, preview_saved_state)
                except Exception as exc:
                    self.log.emit(f"Preview camera restore warning: {exc}")
                try:
                    cam.DeInit()
                except Exception:
                    pass
                cam = None
                nodemap = None
                info = None
                last = None

            try:
                cam_gui.release_system(cam_list, system)
            except Exception as exc:
                self.log.emit(f"Preview cleanup error: {exc}")

            self.stopped.emit()

    def run_preserve_internal_camera_settings(self: Any) -> None:
        if cam_gui.PySpin is None:
            self.log.emit(f"PySpin unavailable: {cam_gui.PYSPIN_IMPORT_ERROR}")
            self.finished.emit({"ok": False, "error": str(cam_gui.PYSPIN_IMPORT_ERROR), "cameras": []})
            return

        system = cam_gui.PySpin.System.GetInstance()
        cam_list = None
        active: list[tuple[Any, Any, cam_gui.CameraRuntime]] = []
        result: dict[str, Any] = {"ok": True, "cameras": []}

        try:
            cam_root = self.session_dir / "videos"
            cam_root.mkdir(parents=True, exist_ok=True)

            cam_list = system.GetCameras()
            detected = int(cam_list.GetSize())
            self.log.emit(f"Run: PySpin detected {detected} camera(s).")

            for idx in range(detected):
                cam = None
                try:
                    cam = cam_list.GetByIndex(idx)
                    tl = cam.GetTLDeviceNodeMap()
                    info = {
                        "index": idx,
                        "serial": cam_gui.get_string_node(tl, "DeviceSerialNumber"),
                        "model": cam_gui.get_string_node(tl, "DeviceModelName"),
                    }
                    label = f"cam_{idx + 1:02d}_{cam_gui.safe_name(info['serial'] or info['model'], 'camera')}"
                    runtime = cam_gui.CameraRuntime(
                        info=info,
                        folder=cam_root / label,
                        stem=f"{self.session_dir.name}_cam{idx + 1:02d}",
                    )
                    runtime.folder.mkdir(parents=True, exist_ok=True)
                    runtime.ts_file = (runtime.folder / f"{runtime.stem}_timestamps.txt").open("w", encoding="utf-8", buffering=1)

                    cam.Init()
                    nodemap = cam.GetNodeMap()
                    saved_state = camera_state_snapshot(nodemap)
                    problems = validate_preserved_camera_state(saved_state)
                    if problems:
                        raise RuntimeError("; ".join(problems))
                    fps_state = set_camera_acquisition_fps_for_ttl_run(nodemap, float(self.fps))
                    commanded_state = camera_state_snapshot(nodemap)
                    runtime.camera_saved_fps = float(saved_state["fps"])
                    runtime.camera_requested_fps_state = dict(fps_state)
                    runtime.record_fps = float(self.fps)
                    runtime.saved_trigger_state = dict(saved_state)
                    runtime.capture_timestamps = []
                    runtime.measured_fps = 0.0

                    self.log.emit(
                        f"Camera {idx + 1} saved state: "
                        f"serial={info['serial'] or '-'} "
                        f"model={info['model'] or '-'} "
                        f"AcquisitionMode={saved_state['acquisition_mode']} "
                        f"TriggerSelector={saved_state['trigger_selector']} "
                        f"TriggerMode={saved_state['trigger_mode']} "
                        f"TriggerSource={saved_state['trigger_source']} "
                        f"TriggerActivation={saved_state['trigger_activation']} "
                        f"TriggerOverlap={saved_state['trigger_overlap']} "
                        f"Line={saved_state['line_selector']}:{saved_state['line_mode']} "
                        f"CameraFpsBefore={runtime.camera_saved_fps:.3f} "
                        f"ROI={saved_state['width']}x{saved_state['height']} "
                        f"Offset=({saved_state['offset_x']},{saved_state['offset_y']})"
                    )

                    self.log.emit(
                        f"Camera {idx + 1} UI FPS applied: requested={fps_state['requested_fps']:.3f} "
                        f"actual={fps_state['actual_fps']:.3f} "
                        f"resulting={fps_state['resulting_fps']:.3f} "
                        f"range={fps_state['min_fps']:.3f}-{fps_state['max_fps']:.3f}"
                    )

                    armed_state = normalize_camera_trigger_for_ttl_run(nodemap, commanded_state)
                    stream_buffer_state = configure_camera_stream_buffering(cam)
                    runtime.stream_buffer_state = dict(stream_buffer_state)

                    self.log.emit(
                        f"Camera {idx + 1} armed: "
                        f"serial={info['serial'] or '-'} "
                        f"model={info['model'] or '-'} "
                        f"TriggerSelector={armed_state['trigger_selector']} "
                        f"TriggerMode={armed_state['trigger_mode']} "
                        f"TriggerSource={armed_state['trigger_source']} "
                        f"TriggerActivation={armed_state['trigger_activation']} "
                        f"TriggerOverlap={armed_state['trigger_overlap']} "
                        f"Line={armed_state['line_selector']}:{armed_state['line_mode']} "
                        f"TriggerFps={runtime.record_fps:.3f} "
                        f"CameraFpsNode={armed_state['fps']:.3f} "
                        f"ROI={armed_state['width']}x{armed_state['height']} "
                        f"Offset=({armed_state['offset_x']},{armed_state['offset_y']})"
                    )
                    self.log.emit(
                        f"Camera {idx + 1} buffers: "
                        f"PySpin={stream_buffer_state.get('stream_buffer_count_after') or '-'} "
                        f"{stream_buffer_state.get('stream_buffer_handling_after') or '-'}; "
                        f"Python={CAMERA_FRAME_BUFFER_SIZE} frames"
                    )

                    cam.BeginAcquisition()
                    active.append((cam, nodemap, runtime))
                    cam = None

                except Exception as exc:
                    if cam is not None:
                        try:
                            cam.DeInit()
                        except Exception:
                            pass
                    self.log.emit(f"Camera {idx + 1} skipped: {exc}")

            self.armed.emit(len(active), [rt.info for _, _, rt in active])

            if not active:
                result["ok"] = False
                result["error"] = "No cameras could be armed."
                return

            last_preview = 0.0
            last_status = 0.0
            first_frame_seen = False
            first_frame_lock = threading.Lock()
            fatal_lock = threading.Lock()
            fatal_errors: list[str] = []
            acquisition_threads: list[threading.Thread] = []
            writer_threads: list[threading.Thread] = []

            for _, _, rt in active:
                rt.frame_queue = queue.Queue(maxsize=CAMERA_FRAME_BUFFER_SIZE)
                rt.acquired_frames = 0
                rt.incomplete_frames = 0
                rt.queue_peak = 0
                rt.queue_overflows = 0
                rt.stop_drain_frames = 0
                rt.acquisition_done = False
                rt.writer_done = False

            def mark_fatal(detail: str) -> None:
                with fatal_lock:
                    if not fatal_errors:
                        fatal_errors.append(detail)
                        result["ok"] = False
                        result["error"] = detail
                self._stop.set()

            def emit_first_frame_once() -> None:
                nonlocal first_frame_seen
                payload = None
                with first_frame_lock:
                    if not first_frame_seen:
                        first_frame_seen = True
                        payload = {
                            "_event": "first_frame",
                            "_first_frame_at": dt.datetime.now().isoformat(),
                            **{str(rt2.info["index"]): getattr(rt2, "acquired_frames", 0) for _, _, rt2 in active},
                        }
                if payload is not None:
                    self.status.emit(payload)

            def start_writer_if_needed(rt: Any, frame: Any) -> None:
                if rt.writer is not None:
                    return
                h, w = frame.shape
                writer_fps = max(1, int(round(getattr(rt, "record_fps", 0.0) or self.fps or 50.0)))
                rt.writer = cam_gui.FfmpegWriter(rt.folder, rt.stem, w, h, writer_fps)
                set_writer_recording_format(
                    rt.writer,
                    getattr(self, "recording_format", DEFAULT_RECORDING_FORMAT),
                )
                rt.writer.start()
                self.log.emit(
                    f"Recording started: cam {rt.info['index'] + 1} {w}x{h} "
                    f"@ {writer_fps} fps from external frame TTL; "
                    f"{recording_format_label(getattr(rt.writer, 'recording_format', DEFAULT_RECORDING_FORMAT))} "
                    f"(camera FPS node {rt.camera_saved_fps:.3f}; "
                    f"buffer={CAMERA_FRAME_BUFFER_SIZE} frames) -> {rt.folder}"
                )

            def camera_acquisition_loop(cam: Any, rt: Any) -> None:
                drain_started_at: float | None = None
                last_frame_at = time.monotonic()
                try:
                    while True:
                        if self._stop.is_set():
                            now = time.monotonic()
                            if drain_started_at is None:
                                drain_started_at = now
                                self.log.emit(
                                    f"Camera {rt.info['index'] + 1} stop requested; draining pending camera frames."
                                )
                            elif (
                                now - last_frame_at >= CAMERA_STOP_DRAIN_IDLE_S
                                or now - drain_started_at >= CAMERA_STOP_DRAIN_MAX_S
                            ):
                                break

                        image = None
                        try:
                            image = cam.GetNextImage(100)
                            if image.IsIncomplete():
                                rt.incomplete_frames += 1
                                continue
                            ts = int(image.GetTimeStamp())
                            frame = cam_gui.image_to_gray(image).copy()
                        except cam_gui.PySpin.SpinnakerException as exc:
                            if cam_gui.is_timeout(exc):
                                if (
                                    drain_started_at is not None
                                    and time.monotonic() - last_frame_at >= CAMERA_STOP_DRAIN_IDLE_S
                                ):
                                    break
                                continue
                            else:
                                detail = f"Camera {rt.info['index'] + 1} acquisition error: {exc}"
                                rt.error = detail
                                self.log.emit(detail)
                                mark_fatal(detail)
                                break
                        except Exception as exc:
                            detail = f"Camera {rt.info['index'] + 1} acquisition error: {exc}"
                            rt.error = detail
                            self.log.emit(detail)
                            mark_fatal(detail)
                            break
                        finally:
                            if image is not None:
                                image.Release()

                        emit_first_frame_once()
                        last_frame_at = time.monotonic()

                        try:
                            rt.frame_queue.put((frame, ts), timeout=CAMERA_QUEUE_PUT_TIMEOUT_S)
                        except queue.Full:
                            rt.queue_overflows += 1
                            detail = (
                                f"Camera {rt.info['index'] + 1} frame buffer filled "
                                f"({CAMERA_FRAME_BUFFER_SIZE} frames). Disk/encoder did not keep up; "
                                "acquisition is stopping and the queued frame backlog will be drained before shutdown."
                            )
                            rt.error = detail
                            self.log.emit(detail)
                            mark_fatal(detail)
                            break

                        rt.acquired_frames += 1
                        if drain_started_at is not None:
                            rt.stop_drain_frames += 1
                        qsize = rt.frame_queue.qsize()
                        if qsize > rt.queue_peak:
                            rt.queue_peak = qsize
                finally:
                    rt.acquisition_done = True

            def camera_writer_loop(rt: Any) -> None:
                try:
                    while True:
                        if getattr(rt, "acquisition_done", False) and rt.frame_queue.empty():
                            break
                        try:
                            frame, ts = rt.frame_queue.get(timeout=0.05)
                        except queue.Empty:
                            continue

                        try:
                            start_writer_if_needed(rt, frame)
                            rt.writer.write(frame)
                            if rt.ts_file:
                                rt.ts_file.write(f"{ts}\n")
                            rt.capture_timestamps.append(ts)
                            if len(rt.capture_timestamps) >= 2:
                                rt.measured_fps = estimate_capture_fps_from_timestamps(
                                    rt.capture_timestamps,
                                    getattr(rt, "record_fps", 0.0) or 50.0,
                                )
                            rt.frames += 1
                            rt.last_frame = frame
                        except Exception as exc:
                            writer = getattr(rt, "writer", None)
                            returncode = None
                            if writer is not None and getattr(writer, "proc", None) is not None:
                                try:
                                    returncode = writer.proc.poll()
                                except Exception:
                                    returncode = None
                            detail = f"Camera {rt.info['index'] + 1} write error: {exc}"
                            if returncode is not None:
                                detail += f" | ffmpeg exited with code {returncode}. See {writer.log_path}"
                                log_tail = tail_text(writer.log_path)
                                if log_tail:
                                    detail += f" | ffmpeg log: {log_tail}"
                            rt.error = detail
                            self.log.emit(detail)
                            mark_fatal(detail)
                            break
                        finally:
                            rt.frame_queue.task_done()
                finally:
                    rt.writer_done = True

            for cam, _, rt in active:
                acq_thread = threading.Thread(
                    target=camera_acquisition_loop,
                    args=(cam, rt),
                    name=f"camera-acquire-{rt.info['index'] + 1}",
                    daemon=True,
                )
                write_thread = threading.Thread(
                    target=camera_writer_loop,
                    args=(rt,),
                    name=f"camera-write-{rt.info['index'] + 1}",
                    daemon=True,
                )
                acquisition_threads.append(acq_thread)
                writer_threads.append(write_thread)
                write_thread.start()
                acq_thread.start()

            while not self._stop.is_set():
                now = cam_gui.time.monotonic()

                if now - last_preview >= 1.0 / max(1, self.preview_fps):
                    last_preview = now
                    frame = cam_gui.grid_image(
                        [rt.last_frame for _, _, rt in active],
                        [f"Cam {rt.info['index'] + 1} {rt.info.get('serial') or rt.info.get('model')}" for _, _, rt in active],
                    )
                    if frame is not None:
                        self.preview.emit(cam_gui.gray_to_qimage(frame))

                if now - last_status >= 1.0:
                    last_status = now
                    self.status.emit({str(rt.info["index"]): rt.frames for _, _, rt in active})

                if acquisition_threads and not any(thread.is_alive() for thread in acquisition_threads):
                    break

                time.sleep(0.005)

            self._stop.set()

            for thread in acquisition_threads:
                thread.join(CAMERA_THREAD_JOIN_TIMEOUT_S)
            for _, _, rt in active:
                rt.acquisition_done = True
            for thread in writer_threads:
                thread.join()

            for _, _, rt in active:
                if not getattr(rt, "writer_done", False) and not rt.error:
                    detail = f"Camera {rt.info['index'] + 1} writer did not drain before shutdown."
                    rt.error = detail
                    result["ok"] = False
                    result["error"] = result.get("error") or detail
                    self.log.emit(detail)

        except Exception as exc:
            result["ok"] = False
            result["error"] = str(exc)
            self.log.emit(f"Camera run error: {exc}")

        finally:
            while active:
                cam, nodemap, rt = active.pop()

                try:
                    cam.EndAcquisition()
                except Exception:
                    pass

                try:
                    restore_camera_acquisition_fps_state(
                        nodemap,
                        getattr(rt, "camera_requested_fps_state", {}),
                    )
                except Exception as exc:
                    self.log.emit(
                        f"Camera {rt.info['index'] + 1} FPS restore warning: {exc}"
                    )

                try:
                    restore_camera_trigger_state(nodemap, getattr(rt, "saved_trigger_state", {}))
                except Exception as exc:
                    self.log.emit(
                        f"Camera {rt.info['index'] + 1} trigger restore warning: {exc}"
                    )

                try:
                    cam.DeInit()
                except Exception:
                    pass

                try:
                    if rt.writer:
                        rt.writer.close()
                except Exception:
                    pass

                try:
                    if rt.ts_file:
                        rt.ts_file.close()
                except Exception:
                    pass

                writer = getattr(rt, "writer", None)
                writer_format = normalize_recording_format(
                    getattr(writer, "recording_format", getattr(self, "recording_format", DEFAULT_RECORDING_FORMAT))
                )
                result["cameras"].append(
                    {
                        "index": rt.info["index"],
                        "serial": rt.info.get("serial", ""),
                        "model": rt.info.get("model", ""),
                        "frames": rt.frames,
                        "acquired_frames": getattr(rt, "acquired_frames", rt.frames),
                        "written_frames": rt.frames,
                        "frame_buffer_size": CAMERA_FRAME_BUFFER_SIZE,
                        "frame_buffer_peak": getattr(rt, "queue_peak", 0),
                        "frame_buffer_overflows": getattr(rt, "queue_overflows", 0),
                        "stop_drain_frames": getattr(rt, "stop_drain_frames", 0),
                        "incomplete_frames": getattr(rt, "incomplete_frames", 0),
                        "stream_buffer": getattr(rt, "stream_buffer_state", {}),
                        "folder": str(rt.folder),
                        "video_path": str(getattr(writer, "path", "")) if writer is not None else "",
                        "recording_format": writer_format,
                        "recording_format_label": recording_format_label(writer_format),
                        "error": rt.error,
                        "triggered_fps": getattr(rt, "record_fps", 0.0),
                        "camera_fps_node": getattr(rt, "camera_saved_fps", 0.0),
                        "camera_requested_fps_actual": getattr(rt, "camera_requested_fps_state", {}).get("actual_fps", 0.0),
                        "camera_resulting_fps": getattr(rt, "camera_requested_fps_state", {}).get("resulting_fps", 0.0),
                        "measured_fps": getattr(rt, "measured_fps", 0.0),
                    }
                )

                cam = None
                nodemap = None
                rt = None

            captured = [cam_result for cam_result in result["cameras"] if int(cam_result.get("frames", 0)) > 0]
            if result["cameras"] and not captured and result.get("ok", True):
                result["ok"] = False
                result["error"] = "No frames captured from any armed camera; TTL was not detected by cameras."
            elif result["cameras"] and len(captured) < len(result["cameras"]):
                result["warning"] = "One or more armed cameras captured zero frames."

            try:
                cam_gui.release_system(cam_list, system)
            except Exception as exc:
                self.log.emit(f"Camera cleanup error: {exc}")

            self.finished.emit(result)

    def write_metadata_preserve_current_camera_settings(self: Any, status: str) -> None:
        if not self.session_dir:
            return

        owner = getattr(self, "_combined_owner", None)
        if owner is not None:
            try:
                owner.write_session_metadata(Path(self.session_dir), status)
                return
            except Exception:
                pass

        payload = {
            "status": status,
            "updated": dt.datetime.now().isoformat(),
            "ttl": {
                "port": cam_gui.combo_value(self.trigger_port),
                "frequency_hz": self.freq.value(),
                "pulse_ms": self.pulse_ms.value(),
                "count": self.ttl_count(),
                "duration_min": self.duration_min.value(),
                "calibration_ppm": ttl_calibration_ppm_value(self),
                "calibration_command_enabled": ttl_calibration_enabled_value(self),
                "camera_settle_delay_s_before_ttl_command": CAMERA_SETTLE_DELAY_S if self.use_cameras.isChecked() else 0.0,
            },
            "camera": {
                "enabled": self.use_cameras.isChecked(),
                "settings_policy": "preserve_image_settings_apply_ui_fps_arm_external_frame_trigger_restore_after_cleanup",
                "persistent_camera_settings_written_by_gui": False,
                "temporary_camera_nodes_written_for_run": ["AcquisitionFrameRateEnable", "AcquisitionFrameRate", "TriggerSelector", "TriggerSource", "TriggerActivation", "TriggerMode", "LineSelector", "LineMode"],
                "fps_policy": "external_ttl_frequency_is_acquisition_and_video_fps",
                "roi_policy": "use_active_camera_roi",
                "recording_format": selected_recording_format(getattr(self, "recording_format", None)),
                "recording_format_label": recording_format_label(selected_recording_format(getattr(self, "recording_format", None))),
            },
            "stim": {
                "enabled": self.use_stim.isChecked(),
                "port": cam_gui.combo_value(self.stim_port),
            },
        }

        try:
            (self.session_dir / "recording_metadata.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    cam_gui.load_default_userset = lambda nodemap: False
    cam_gui.configure_free_run = configure_free_run_preserve_internal
    cam_gui.configure_hardware_trigger = configure_hardware_trigger_preserve_internal
    cam_gui.release_camera = release_camera_preserve_current_state
    cam_gui.FfmpegWriter.start = ffmpeg_start_with_above_normal_priority
    cam_gui.TTLThread.run = ttl_run_match_working_notebook
    cam_gui.MainWindow.start_run = start_run_with_above_normal_priority
    cam_gui.MainWindow.finish_if_idle = finish_if_idle_with_priority_restore
    cam_gui.MainWindow.maybe_start_ttl = maybe_start_ttl_match_working_notebook
    cam_gui.MainWindow.on_ttl_finished = on_ttl_finished_after_start_pulse
    cam_gui.MainWindow.on_cameras_finished = on_cameras_finished_recording
    cam_gui.MainWindow.stop_all = stop_all_recording
    cam_gui.MainWindow.toggle_preview = toggle_preview_disabled
    cam_gui.FfmpegWriter.write = ffmpeg_write_memoryview
    cam_gui.PreviewThread.run = preview_run_preserve_current_camera_state
    cam_gui.CameraRunThread.run = run_preserve_internal_camera_settings
    cam_gui.MainWindow.write_metadata = write_metadata_preserve_current_camera_settings
    _CAMERA_PATCHED = True


def find_label(widget: QtWidgets.QWidget, text: str) -> QtWidgets.QLabel | None:
    for label in widget.findChildren(QtWidgets.QLabel):
        if label.text().strip() == text.strip():
            return label
    return None


def find_label_containing(widget: QtWidgets.QWidget, fragment: str) -> QtWidgets.QLabel | None:
    for label in widget.findChildren(QtWidgets.QLabel):
        if fragment in label.text():
            return label
    return None


def move_to_dict(move: stim_gui.Move) -> dict[str, Any]:
    return {
        "pos_label": normalize_position_label(move.pos_label),
        "pos_cm": float(move.pos_cm),
        "rot_token": normalize_rot_token(move.rot_token),
        "rot_cm_s": float(move.rot_cm_s),
        "rot_dir": move.rot_dir,
        "interval_s": move_interval_s(move),
        "stimulus_condition": move_condition(move),
        "paired_rot_token": paired_rot_token(move.rot_token),
        "repeat_index": int(move.repeat_index),
        "step_in_repeat": int(move.step_in_repeat),
        "global_step": int(move.global_step),
        "move_label": move_saved_label(move),
    }


def move_from_payload(payload: Any, item_index: int) -> stim_gui.Move | None:
    if isinstance(payload, dict):
        try:
            return stim_gui.Move(
                pos_label=normalize_position_label(str(payload.get("pos_label", ""))),
                pos_cm=float(payload.get("pos_cm", 0.0)),
                rot_token=normalize_rot_token(str(payload.get("rot_token", ""))),
                rot_cm_s=float(payload.get("rot_cm_s", 0.0)),
                rot_dir=str(payload.get("rot_dir", "")),
                repeat_index=int(payload.get("repeat_index", 1)),
                step_in_repeat=int(payload.get("step_in_repeat", item_index + 1)),
                global_step=int(payload.get("global_step", item_index + 1)),
                interval_s=float(payload.get("interval_s", payload.get("interval_between_moves_s", -1.0))),
            )
        except Exception:
            return None

    if isinstance(payload, (list, tuple)):
        if len(payload) >= 8:
            try:
                return stim_gui.Move(
                    pos_label=normalize_position_label(str(payload[0])),
                    pos_cm=float(payload[1]),
                    rot_token=normalize_rot_token(str(payload[2])),
                    rot_cm_s=float(payload[3]),
                    rot_dir=str(payload[4]),
                    repeat_index=int(payload[5]),
                    step_in_repeat=int(payload[6]),
                    global_step=int(payload[7]),
                    interval_s=float(payload[8]) if len(payload) >= 9 else -1.0,
                )
            except Exception:
                return None
        if len(payload) >= 5:
            try:
                return stim_gui.Move(
                    pos_label=normalize_position_label(str(payload[0])),
                    pos_cm=float(payload[1]),
                    rot_token=normalize_rot_token(str(payload[2])),
                    rot_cm_s=float(payload[3]),
                    rot_dir=str(payload[4]),
                    repeat_index=1,
                    step_in_repeat=item_index + 1,
                    global_step=item_index + 1,
                )
            except Exception:
                return None
    return None


def move_to_text(move: stim_gui.Move) -> str:
    if stim_gui.is_recalibration_label(move.pos_label):
        return f"Repeat {move.repeat_index} | Step {move.step_in_repeat} | recalibration"
    return (
        f"Repeat {move.repeat_index} | Step {move.step_in_repeat} | "
        f"{move_display_label(move)} | condition {move_condition(move)} | pos {move.pos_cm:g} cm | "
        f"rot {move.rot_cm_s:g}{move.rot_dir} cm/s | interval {move_interval_s(move):g} s"
    )


def flatten_rows(rows: list[dict[str, str]], section: str, prefix: str, value: Any) -> None:
    if isinstance(value, dict):
        for key in sorted(value):
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            flatten_rows(rows, section, next_prefix, value[key])
        return
    if isinstance(value, list):
        rows.append(
            {
                "section": section,
                "key": prefix,
                "value": json.dumps(value, ensure_ascii=False),
            }
        )
        return
    rows.append(
        {
            "section": section,
            "key": prefix,
            "value": "" if value is None else str(value),
        }
    )


def write_config_csv(payload: dict[str, Any], path: Path) -> None:
    rows: list[dict[str, str]] = []
    meta = {
        "schema_version": payload.get("schema_version", ""),
        "app": payload.get("app", ""),
        "saved_at": payload.get("saved_at", ""),
        "mode": payload.get("mode", ""),
        "settings_save_directory": payload.get("settings_save_directory", ""),
        "recording_sessions_directory": payload.get("recording_sessions_directory", ""),
        "saved_settings_file_path": payload.get("saved_settings_file_path", ""),
    }
    flatten_rows(rows, "meta", "", meta)
    flatten_rows(rows, "camera", "", payload.get("camera", {}))
    flatten_rows(
        rows,
        "somatosensory_stimulation_device",
        "",
        payload.get("somatosensory_stimulation_device", {}),
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["section", "key", "value"])
        writer.writeheader()
        writer.writerows(rows)


def write_sequence_csv(path: Path, moves: list[stim_gui.Move]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "global_step",
                "repeat_index",
                "step_in_repeat",
                "move_label",
                "hardness",
                "stimulus_condition",
                "pos_cm",
                "rot_token",
                "paired_rot_token",
                "rot_cm_s",
                "rot_dir",
                "interval_s",
                "next_pos_cm",
                "move_to_next_before_interval",
                "switching_required",
            ]
        )
        previous_pos_cm: float | None = None
        for idx, move in enumerate(moves):
            next_move = moves[idx + 1] if idx + 1 < len(moves) else None
            next_is_stimulus = (
                not stim_gui.is_recalibration_label(move.pos_label)
                and next_move is not None
                and not stim_gui.is_recalibration_label(next_move.pos_label)
            )
            switching_required = previous_pos_cm is not None and float(move.pos_cm) != float(previous_pos_cm)
            writer.writerow(
                [
                    move.global_step,
                    move.repeat_index,
                    move.step_in_repeat,
                    move_saved_label(move),
                    normalize_position_label(move.pos_label),
                    move_condition(move),
                    move.pos_cm,
                    normalize_rot_token(move.rot_token),
                    paired_rot_token(move.rot_token),
                    move.rot_cm_s,
                    move.rot_dir,
                    move_interval_s(move),
                    float(next_move.pos_cm) if next_is_stimulus else "",
                    bool(next_is_stimulus and float(next_move.pos_cm) != float(move.pos_cm)),
                    switching_required,
                ]
            )
            previous_pos_cm = float(move.pos_cm)


class CombinedWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        patch_camera_runtime_for_internal_camera_settings()

        self.setWindowTitle("Camera + Somatosensory stimulation device")
        self.resize(1450, 950)

        self._applying_settings = False
        self._closing = False
        self._layouts_adjusted = False
        self.last_prepared_signature = ""
        self._loaded_settings_path: Path | None = None
        self._observed_camera_thread: Any = None
        self.current_camera_session_dir: Path | None = None
        self._session_name_user_edited = False
        self.camera_first_frame_wallclock: dt.datetime | None = None
        self.camera_first_frame_monotonic: float | None = None
        self.camera_session_started_monotonic: float | None = None
        self._active_stim_run_context: dict[str, Any] | None = None
        self._auto_stim_armed_for_session = False
        self._auto_stim_scheduled_for_session = False
        self._auto_stim_timer_source = ""
        self._session_stim_runs: dict[str, list[dict[str, Any]]] = {}
        self._session_camera_first_frame_at: dict[str, str] = {}
        self._session_camera_results: dict[str, dict[str, Any]] = {}
        self._session_ttl_results: dict[str, dict[str, Any]] = {}
        self._session_camera_stop_requested_at: dict[str, str] = {}
        self._session_stim_stop_requested_at: dict[str, str] = {}
        self.brush_order_edits: list[QtWidgets.QLineEdit] = []
        self.brush_order_preview_label: QtWidgets.QLabel | None = None
        self.null_rotating_combos: list[QtWidgets.QComboBox] = []
        self.interval_range_chk: QtWidgets.QCheckBox | None = None
        self.interval_min_s: QtWidgets.QDoubleSpinBox | None = None
        self.interval_max_s: QtWidgets.QDoubleSpinBox | None = None
        self.interval_min_label: QtWidgets.QLabel | None = None
        self.interval_max_label: QtWidgets.QLabel | None = None
        self.expected_duration_label: QtWidgets.QLabel | None = None
        self.resume_sequence_chk: QtWidgets.QCheckBox | None = None
        self.resume_sequence_step: QtWidgets.QSpinBox | None = None
        self.metadata_edits: dict[str, QtWidgets.QLineEdit] = {}
        self.insertion_table: QtWidgets.QTableWidget | None = None

        self.camera_window = cam_gui.MainWindow()
        self.stim_window = stim_gui.MainWindow()
        self.camera_window.setWindowTitle("Camera")
        self.stim_window.setWindowTitle("Somatosensory stimulation device")
        self.camera_window._combined_owner = self
        preview_btn = getattr(self.camera_window, "preview_btn", None)
        if preview_btn is not None:
            preview_btn.setEnabled(False)
            preview_btn.setVisible(False)
            preview_btn.setToolTip("Live preview is disabled in the combined camera + stimulation GUI.")

        self.auto_stim_timer = QtCore.QTimer(self)
        self.auto_stim_timer.setSingleShot(True)
        self.auto_stim_timer.timeout.connect(self.fire_auto_stim_after_camera)

        self.apply_wrapper_stim_defaults()
        self.ensure_default_storage_paths()

        self.camera_panel = take_central(self.camera_window)
        self.stim_panel = take_central(self.stim_window)

        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        layout = QtWidgets.QVBoxLayout(root)

        mode_box = QtWidgets.QGroupBox("Run mode and settings")
        mode_layout = QtWidgets.QVBoxLayout(mode_box)

        run_row = QtWidgets.QHBoxLayout()
        self.mode_group = QtWidgets.QButtonGroup(self)
        self.mode_camera = QtWidgets.QRadioButton("Camera only")
        self.mode_stim = QtWidgets.QRadioButton("Somatosensory stimulation only")
        self.mode_both = QtWidgets.QRadioButton("Camera + somatosensory stimulation")
        self.mode_both.setChecked(True)
        for idx, button in enumerate((self.mode_camera, self.mode_stim, self.mode_both)):
            self.mode_group.addButton(button, idx)
            run_row.addWidget(button)
        run_row.addSpacing(18)
        self.refresh_btn = QtWidgets.QPushButton("Refresh ports")
        self.refresh_btn.clicked.connect(self.refresh_ports)
        run_row.addWidget(self.refresh_btn)
        run_row.addStretch(1)
        mode_layout.addLayout(run_row)

        settings_row = QtWidgets.QHBoxLayout()
        self.settings_dir_edit = QtWidgets.QLineEdit(str(DEFAULT_SETTINGS_DIR))
        self.settings_dir_btn = QtWidgets.QPushButton("Browse")
        self.settings_dir_btn.clicked.connect(self.browse_settings_directory)
        self.save_btn = QtWidgets.QPushButton("Save settings")
        self.save_btn.clicked.connect(self.export_settings_to_default_path)
        self.save_as_btn = QtWidgets.QPushButton("Save settings as...")
        self.save_as_btn.clicked.connect(self.export_settings_as)
        self.load_btn = QtWidgets.QPushButton("Load settings...")
        self.load_btn.clicked.connect(self.load_settings_from_dialog)
        settings_row.addWidget(QtWidgets.QLabel("Saved settings directory:"))
        settings_row.addWidget(self.settings_dir_edit, 1)
        settings_row.addWidget(self.settings_dir_btn)
        settings_row.addWidget(self.save_btn)
        settings_row.addWidget(self.save_as_btn)
        settings_row.addWidget(self.load_btn)
        mode_layout.addLayout(settings_row)
        layout.addWidget(mode_box)

        hint = QtWidgets.QLabel(
            "The tabs below keep camera and somatosensory control separate. "
            "Camera Run starts only camera and TTL recording. "
            "Somatosensory START starts only the somatosensory stimulation device."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9fb4c8;")
        layout.addWidget(hint)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self.camera_panel, "Camera")
        self.tabs.addTab(self.stim_panel, "Somatosensory stimulation device")
        self.metadata_panel = QtWidgets.QWidget()
        self.metadata_panel_layout = QtWidgets.QVBoxLayout(self.metadata_panel)
        self.metadata_panel_layout.setContentsMargins(8, 8, 8, 8)
        self.metadata_panel_layout.setSpacing(10)
        self.tabs.addTab(self.metadata_panel, "Metadata")
        layout.addWidget(self.tabs, 1)

        self.settings_status = QtWidgets.QLabel("")
        self.settings_status.setStyleSheet("color:#9fb4c8;")
        layout.addWidget(self.settings_status)

        self.enforce_separate_run_controls()
        self.install_camera_wrapper_controls()
        self.install_stim_wrapper_controls()
        self.sync_hidden_output_fields()

        self.mode_group.idClicked.connect(self.apply_mode)
        self.settings_dir_edit.textChanged.connect(self.on_settings_directory_changed)
        self.camera_window.output_root.textChanged.connect(self.on_recording_sessions_directory_changed)

        self.apply_persisted_directory_state()
        self.refresh_default_session_name(force=True)
        self.load_saved_settings_on_startup()
        self.refresh_default_session_name()
        self.apply_mode()
        self.update_settings_status()

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        if not self._layouts_adjusted:
            self._layouts_adjusted = True
            QtCore.QTimer.singleShot(0, self.adjust_embedded_layouts)

    def ensure_default_storage_paths(self) -> None:
        self.camera_window.output_root.setText(str(DEFAULT_SESSIONS_DIR))
        self.stim_window.dir_edit.setText(str(DEFAULT_SESSIONS_DIR))

    def directory_state_payload(self) -> dict[str, str]:
        return {
            "settings_save_directory": str(self.settings_export_directory()),
            "recording_sessions_directory": str(self.recording_sessions_directory()),
        }

    def persist_directory_state(self) -> None:
        try:
            DIRECTORY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            DIRECTORY_STATE_PATH.write_text(
                json.dumps(self.directory_state_payload(), indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def apply_persisted_directory_state(self) -> None:
        try:
            payload = json.loads(DIRECTORY_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        settings_dir = str(payload.get("settings_save_directory", "")).strip()
        recording_dir = str(payload.get("recording_sessions_directory", "")).strip()
        if settings_dir:
            self.settings_dir_edit.setText(settings_dir)
        if recording_dir:
            self.camera_window.output_root.setText(recording_dir)
            self.stim_window.dir_edit.setText(recording_dir)

    def settings_export_directory(self) -> Path:
        text = self.settings_dir_edit.text().strip()
        return Path(text) if text else DEFAULT_SETTINGS_DIR

    def recording_sessions_directory(self) -> Path:
        text = self.camera_window.output_root.text().strip()
        return Path(text) if text else DEFAULT_SESSIONS_DIR

    def default_saved_settings_path(self) -> Path:
        return self.settings_export_directory() / f"{CONFIG_BASENAME}.json"

    def update_settings_status(self, message: str | None = None) -> None:
        base = (
            f"Saved config: {self.default_saved_settings_path()} | "
            f"Recording sessions: {self.recording_sessions_directory()}"
        )
        self.settings_status.setText(f"{message} | {base}" if message else base)

    def on_settings_directory_changed(self, *_: Any) -> None:
        self.update_settings_status()
        if not self._applying_settings:
            self.persist_directory_state()

    def on_recording_sessions_directory_changed(self, *_: Any) -> None:
        self.sync_hidden_output_fields()
        self.refresh_default_session_name()
        self.update_settings_status()
        if not self._applying_settings:
            self.persist_directory_state()

    def on_session_name_edited(self, *_: Any) -> None:
        self._session_name_user_edited = bool(self.session_name_edit.text().strip())

    def refresh_default_session_name(self, force: bool = False) -> None:
        if not hasattr(self, "session_name_edit"):
            return
        if self._session_name_user_edited and not force:
            return
        self.session_name_edit.setText(next_default_session_name(self.recording_sessions_directory()))

    def consume_camera_session_name_for_run(self) -> str:
        raw = self.session_name_edit.text().strip() if hasattr(self, "session_name_edit") else ""
        fallback = next_default_session_name(self.recording_sessions_directory())
        name = cam_gui.safe_name(raw, fallback)
        if not name:
            name = fallback
        self.session_name_edit.setText(name)
        self._session_name_user_edited = False
        QtCore.QTimer.singleShot(0, self.refresh_default_session_name)
        return name

    def browse_settings_directory(self) -> None:
        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Choose settings save directory",
            str(self.settings_export_directory()),
        )
        if chosen:
            self.settings_dir_edit.setText(chosen)

    def load_saved_settings_on_startup(self) -> None:
        self.load_state(self.default_saved_settings_path(), quiet=True)

    def normalize_legacy_null_position_labels(self) -> None:
        if not hasattr(self, "stim_window"):
            return
        for label_edit, _ in self.stim_window.pos_rows:
            raw = label_edit.text()
            normalized = normalize_position_label(raw)
            if normalized != raw.strip():
                label_edit.setText(normalized)

    def install_null_rotating_controls(self) -> None:
        self.null_rotating_combos = []
        if not self.stim_window.pos_rows:
            return

        pos_parent = self.stim_window.pos_rows[0][0].parentWidget()
        if isinstance(pos_parent, QtWidgets.QGroupBox):
            pos_parent.setTitle("Linear positions (<=5): label + cm from reference 0 + no_rotation option")
            pos_parent.setToolTip(
                "A row labelled no_brushing means the rod moves to a no-brush position and still rotates. "
                "Use add no_rotation to add matched trials at that linear position with rotary speed 0."
            )

        grid = pos_parent.layout() if pos_parent is not None else None
        for idx, _row in enumerate(self.stim_window.pos_rows):
            combo = QtWidgets.QComboBox()
            combo.addItem("rotate only", False)
            combo.addItem("add no_rotation", True)
            combo.setToolTip(
                "For this linear position, add one no-rotation trial for every rotary speed token. "
                "For no_brushing rows this adds one null_stimulus trial per repeat."
            )
            combo.setMinimumWidth(160)
            self.null_rotating_combos.append(combo)
            if isinstance(grid, QtWidgets.QGridLayout):
                grid.addWidget(combo, idx, 3)
        self.refresh_null_rotating_controls()

    def apply_wrapper_stim_defaults(self) -> None:
        w = self.stim_window
        self.normalize_legacy_null_position_labels()
        w.lin_halfrev_cm.setValue(11.30)
        w.lin_steps_mm.setValue(14.1593)
        w.lin_home_cm_s.setValue(8.0)
        w.lin_move_cm_s.setValue(8.0)
        w.lin_offset0.setValue(0.0)
        w.scramble.setChecked(False)
        w.seed_edit.clear()
        w.on_scramble_toggled(False)

        if not any(label.text().strip() for label, _ in w.pos_rows):
            defaults = [
                (NO_BRUSHING_LABEL, 1.0),
                ("Soft", 4.7),
                ("Medium", 8.4),
                ("Hard", 12.1),
            ]
            for idx, (label, cm) in enumerate(defaults):
                w.pos_rows[idx][0].setText(label)
                w.pos_rows[idx][1].setValue(cm)

        if not any(field.text().strip() for field in w.rot_rows):
            for idx, token in enumerate(["1R", "3R", "5R", "7R", "9R", "11R"]):
                w.rot_rows[idx].setText(token)

    def null_rotating_enabled_for_row(self, row_index: int) -> bool:
        if row_index < 0 or row_index >= len(self.null_rotating_combos):
            return False
        if row_index >= len(self.stim_window.pos_rows):
            return False
        return bool(self.null_rotating_combos[row_index].currentData())

    def set_null_rotating_for_row(self, row_index: int, enabled: bool) -> None:
        if row_index < 0 or row_index >= len(self.null_rotating_combos):
            return
        combo = self.null_rotating_combos[row_index]
        for idx in range(combo.count()):
            if bool(combo.itemData(idx)) == bool(enabled):
                combo.setCurrentIndex(idx)
                return

    def null_rotating_rows(self) -> list[bool]:
        return [self.null_rotating_enabled_for_row(idx) for idx in range(len(self.stim_window.pos_rows))]

    def refresh_null_rotating_controls(self, *_: Any) -> None:
        for idx, combo in enumerate(self.null_rotating_combos):
            if idx >= len(self.stim_window.pos_rows):
                combo.setEnabled(False)
                continue
            label_edit, _ = self.stim_window.pos_rows[idx]
            label = normalize_position_label(label_edit.text())
            enabled = bool(label)
            if not enabled and bool(combo.currentData()):
                self.set_null_rotating_for_row(idx, False)
            combo.setEnabled(enabled)

    def null_rotating_position_settings(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for idx, (label_edit, pos_spin) in enumerate(self.stim_window.pos_rows):
            enabled = self.null_rotating_enabled_for_row(idx)
            label = normalize_position_label(label_edit.text())
            if enabled_only and not enabled:
                continue
            rows.append(
                {
                    "row_index": idx + 1,
                    "label": label,
                    "cm": float(pos_spin.value()),
                    "add_no_rotation": enabled,
                }
            )
        return rows

    def apply_null_rotating_settings(self, settings: dict[str, Any], positions: list[Any]) -> None:
        enabled_by_row: dict[int, bool] = {}

        raw_rows = settings.get("no_rotation_rows", settings.get("null_rotating_rows", []))
        if isinstance(raw_rows, list):
            for idx, enabled in enumerate(raw_rows):
                enabled_by_row[idx] = bool(enabled)

        raw_positions = settings.get("no_rotation_positions", settings.get("null_rotating_positions", []))
        if isinstance(raw_positions, list):
            for item in raw_positions:
                if isinstance(item, dict):
                    row_index = item.get("row_index")
                    if row_index is not None:
                        try:
                            enabled = bool(
                                item.get("add_no_rotation", item.get("add_null_rotating", item.get("enabled", False)))
                            )
                            enabled_by_row[int(row_index) - 1] = enabled
                            continue
                        except Exception:
                            pass
                    label = normalize_position_label(str(item.get("label", "")))
                    enabled = bool(item.get("add_no_rotation", item.get("add_null_rotating", item.get("enabled", False))))
                    for idx, (label_edit, _) in enumerate(self.stim_window.pos_rows):
                        if normalize_position_label(label_edit.text()) == label:
                            enabled_by_row[idx] = enabled
                else:
                    label = normalize_position_label(str(item))
                    for idx, (label_edit, _) in enumerate(self.stim_window.pos_rows):
                        if normalize_position_label(label_edit.text()) == label:
                            enabled_by_row[idx] = True

        for idx, item in enumerate(positions):
            if isinstance(item, dict) and ("add_no_rotation" in item or "add_null_rotating" in item):
                enabled_by_row[idx] = bool(item.get("add_no_rotation", item.get("add_null_rotating")))

        for idx in range(len(self.null_rotating_combos)):
            self.set_null_rotating_for_row(idx, enabled_by_row.get(idx, False))

    def current_brush_order_entries(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for idx, (label_edit, pos_spin) in enumerate(self.stim_window.pos_rows):
            if idx < len(self.brush_order_edits):
                label = self.brush_order_edits[idx].text().strip()
            else:
                label = label_edit.text().strip()
            label = normalize_position_label(label)
            if not label:
                continue
            entries.append(
                {
                    "label": label,
                    "cm": float(pos_spin.value()),
                    "row_index": idx + 1,
                }
            )
        return sorted(entries, key=lambda item: (float(item["cm"]), int(item["row_index"])))

    def refresh_brush_order_preview(self, *_: Any) -> None:
        if self.brush_order_preview_label is None:
            return
        entries = self.current_brush_order_entries()
        if not entries:
            self.brush_order_preview_label.setText("Saved order preview: empty")
            return
        preview = " -> ".join(f"{entry['label']} ({entry['cm']:g} cm)" for entry in entries)
        self.brush_order_preview_label.setText(f"Saved order preview: {preview}")

    def enforce_separate_run_controls(self) -> None:
        self.camera_window.use_stim.setChecked(False)
        self.camera_window.use_stim.setEnabled(False)
        self.camera_window.use_stim.setToolTip(
            "Disabled in GUI_stim_and_cam.py: use the Somatosensory stimulation tab START button separately."
        )
        self.camera_window.use_cameras.setToolTip(
            "Redundant in GUI_stim_and_cam.py: use the top Run mode selector instead."
        )

    def install_camera_wrapper_controls(self) -> None:
        self.camera_window.use_cameras.hide()
        self.camera_window.use_stim.hide()
        if hasattr(self.camera_window, "refresh_btn"):
            self.camera_window.refresh_btn.hide()

        self.camera_window.stim_port.hide()
        self.camera_window.stim_port.setEnabled(False)
        stim_label = find_label(self.camera_panel, "Stim COM")
        if stim_label is not None:
            stim_label.hide()

        self.camera_window.width.hide()
        self.camera_window.height.hide()
        self.camera_window.width.setEnabled(False)
        self.camera_window.height.setEnabled(False)
        roi_label = find_label(self.camera_panel, "Camera ROI")
        if roi_label is not None:
            roi_label.hide()

        info_label = find_label_containing(self.camera_panel, "Run mode enforces camera hardware trigger")
        if info_label is not None:
            info_label.setText(
                "Camera recording now preserves each camera's saved SpinView user-set settings, "
                "including ROI and exposure. The GUI Frequency field is applied to the camera FPS gate and TTL train."
            )

        self.camera_window.width.setToolTip(
            "Hidden in the combined GUI. ROI and cropping are preserved from the camera's internal settings."
        )
        self.camera_window.height.setToolTip(
            "Hidden in the combined GUI. ROI and cropping are preserved from the camera's internal settings."
        )

        self.session_name_edit = QtWidgets.QLineEdit()
        self.session_name_edit.setPlaceholderText("Run_0_stim_and_cam")
        self.session_name_edit.textEdited.connect(self.on_session_name_edited)

        self.recording_format_combo = QtWidgets.QComboBox()
        for format_id, label in RECORDING_FORMAT_OPTIONS:
            self.recording_format_combo.addItem(label, format_id)
        self.recording_format_combo.setCurrentIndex(0)
        self.recording_format_combo.setToolTip(
            "FFV1/MKV records lossless Mono8 grayscale without yuv420p color conversion. "
            "Legacy HEVC/MP4 keeps the previous smaller viewing format but is heavier during acquisition."
        )
        self.camera_window.recording_format = self.recording_format_combo

        self.ttl_calibration_ppm = QtWidgets.QSpinBox()
        self.ttl_calibration_ppm.setRange(-100000, 100000)
        self.ttl_calibration_ppm.setValue(DEFAULT_TTL_CALIBRATION_PPM)
        self.ttl_calibration_ppm.setSuffix(" ppm")
        self.ttl_calibration_ppm.setMaximumWidth(140)
        self.ttl_calibration_ppm.setToolTip(
            "Firmware correction value. It is sent only when 'Use firmware ppm correction' is checked."
        )
        self.camera_window.ttl_calibration_ppm = self.ttl_calibration_ppm

        self.ttl_calibration_enabled = QtWidgets.QCheckBox("Use firmware ppm correction")
        self.ttl_calibration_enabled.setChecked(False)
        self.ttl_calibration_enabled.setToolTip(
            "Leave this off for the old TTL Arduino firmware. Turn it on only after uploading firmware that supports CAL."
        )
        self.camera_window.ttl_calibration_enabled = self.ttl_calibration_enabled

        form_layout: QtWidgets.QGridLayout | None = None
        for layout in self.camera_panel.findChildren(QtWidgets.QGridLayout):
            try:
                if layout.indexOf(self.camera_window.output_root) != -1:
                    form_layout = layout
                    break
            except Exception:
                continue
        if form_layout is not None:
            form_layout.addWidget(QtWidgets.QLabel("Recording format"), 4, 0)
            form_layout.addWidget(self.recording_format_combo, 4, 1, 1, 5)
            form_layout.addWidget(QtWidgets.QLabel("Session name"), 5, 0)
            form_layout.addWidget(self.session_name_edit, 5, 1, 1, 4)
            form_layout.addWidget(QtWidgets.QLabel("TTL calibration"), 6, 0)
            form_layout.addWidget(self.ttl_calibration_ppm, 6, 1)
            form_layout.addWidget(self.ttl_calibration_enabled, 6, 2, 1, 3)

    def install_stim_wrapper_controls(self) -> None:
        self.stim_window.export_btn.hide()
        self.stim_window.export_btn.setEnabled(False)

        self.stim_window.build_btn.setText("Build / randomise sequence")
        self.stim_window.build_btn.setToolTip(
            "Prepares the exact final movement sequence shown in the Movement order box. "
            "If Scramble movements is ON, each repeat is randomized independently. "
            "Selected no_rotation rows add matched no-rotation trials."
        )

        try:
            self.stim_window.build_btn.clicked.disconnect()
        except Exception:
            pass
        self.stim_window.build_btn.clicked.connect(self.build_stim_sequence_clicked)

        try:
            self.stim_window.start_btn.clicked.disconnect()
        except Exception:
            pass
        self.stim_window.start_btn.clicked.connect(lambda: self.start_stim_run())

        try:
            self.stim_window.abort_btn.clicked.disconnect()
        except Exception:
            pass
        self.stim_window.abort_btn.clicked.connect(self.abort_stim_run)

        output_label = find_label(self.stim_panel, "Output dir:")
        if output_label is not None:
            output_label.hide()
        self.stim_window.dir_edit.hide()
        self.stim_window.dir_btn.hide()
        self.stim_window.dir_edit.setToolTip(
            "Legacy field kept only for compatibility. Saved settings now live in the top Saved settings directory, "
            "while per-session somatosensory run metadata is written into the active recording session folder."
        )

        self.install_null_rotating_controls()
        self.install_interval_range_controls()
        self.install_expected_duration_label()
        self.install_resume_sequence_controls()

        self.auto_stim_after_camera_chk = QtWidgets.QCheckBox("Turn on with delay after camera")
        self.auto_stim_after_camera_delay_s = QtWidgets.QDoubleSpinBox()
        self.auto_stim_after_camera_delay_s.setRange(0.0, 3600.0)
        self.auto_stim_after_camera_delay_s.setDecimals(2)
        self.auto_stim_after_camera_delay_s.setSingleStep(0.5)
        self.auto_stim_after_camera_delay_s.setValue(0.0)
        self.auto_stim_after_camera_delay_s.setSuffix(" s")
        self.auto_stim_after_camera_delay_s.setEnabled(False)
        self.auto_stim_after_camera_chk.toggled.connect(self.auto_stim_after_camera_delay_s.setEnabled)

        self.install_metadata_panel_controls()

        auto_group = QtWidgets.QGroupBox("Camera-linked start")
        auto_row = QtWidgets.QHBoxLayout(auto_group)
        auto_row.addWidget(self.auto_stim_after_camera_chk)
        auto_row.addSpacing(10)
        auto_row.addWidget(QtWidgets.QLabel("Delay from first camera frame:"))
        auto_row.addWidget(self.auto_stim_after_camera_delay_s)
        auto_row.addStretch(1)
        auto_group.setToolTip(
            "If enabled and Run mode is set to Camera + somatosensory stimulation, "
            "the prepared somatosensory sequence will start automatically after the chosen "
            "delay from the first detected camera frame."
        )
        root_layout = self.stim_panel.layout()
        if isinstance(root_layout, QtWidgets.QVBoxLayout):
            root_layout.insertWidget(1, auto_group)

        self.install_stim_sequence_watchers()
        self.stim_window.order_list.model().rowsMoved.connect(self.sync_order_payloads)
        self.refresh_stim_order_drag_mode()
        self.refresh_brush_order_preview()
        self.update_expected_duration_label()

    def install_interval_range_controls(self) -> None:
        self.interval_range_chk = QtWidgets.QCheckBox("Use uniform range")
        self.interval_min_s = QtWidgets.QDoubleSpinBox()
        self.interval_max_s = QtWidgets.QDoubleSpinBox()
        for spin in (self.interval_min_s, self.interval_max_s):
            spin.setRange(0.0, 300.0)
            spin.setDecimals(2)
            spin.setSingleStep(0.25)
            spin.setSuffix(" s")
            spin.setEnabled(False)
        self.interval_min_s.setValue(float(self.stim_window.interval.value()))
        self.interval_max_s.setValue(float(self.stim_window.interval.value()))
        self.interval_range_chk.toggled.connect(self.sync_interval_control_enabled)

        row_widget = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(row_widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.interval_range_chk)
        self.interval_min_label = QtWidgets.QLabel("min")
        self.interval_max_label = QtWidgets.QLabel("max")
        row.addWidget(self.interval_min_label)
        row.addWidget(self.interval_min_s)
        row.addWidget(self.interval_max_label)
        row.addWidget(self.interval_max_s)
        row.addStretch(1)

        parent = self.stim_window.interval.parentWidget()
        form = parent.layout() if parent is not None else None
        if isinstance(form, QtWidgets.QFormLayout):
            row_index = -1
            try:
                row_index, _role = form.getWidgetPosition(self.stim_window.interval)
            except Exception:
                row_index = -1
            form.removeWidget(self.stim_window.interval)
            row.insertWidget(0, self.stim_window.interval)
            if row_index >= 0:
                form.setWidget(row_index, QtWidgets.QFormLayout.FieldRole, row_widget)
            else:
                form.addRow("Interval between moves:", row_widget)
        self.sync_interval_control_enabled(self.interval_range_chk.isChecked())

    def sync_interval_control_enabled(self, use_range: bool) -> None:
        self.stim_window.interval.setEnabled(not use_range)
        if self.interval_min_label is not None:
            self.interval_min_label.setEnabled(use_range)
        if self.interval_min_s is not None:
            self.interval_min_s.setEnabled(use_range)
        if self.interval_max_label is not None:
            self.interval_max_label.setEnabled(use_range)
        if self.interval_max_s is not None:
            self.interval_max_s.setEnabled(use_range)

    def install_expected_duration_label(self) -> None:
        self.expected_duration_label = QtWidgets.QLabel("Expected recording duration: build sequence to calculate.")
        self.expected_duration_label.setWordWrap(True)
        self.expected_duration_label.setStyleSheet("color:#9fb4c8;")
        order_parent = self.stim_window.order_list.parentWidget()
        order_layout = order_parent.layout() if order_parent is not None else None
        if isinstance(order_layout, QtWidgets.QVBoxLayout):
            index = order_layout.indexOf(self.stim_window.order_list)
            order_layout.insertWidget(index + 1 if index >= 0 else 0, self.expected_duration_label)

    def install_resume_sequence_controls(self) -> None:
        inferred_step = infer_resume_global_step_from_events(APP_DIR / "last_run_events.csv")
        self.resume_sequence_chk = QtWidgets.QCheckBox("Resume from global step")
        self.resume_sequence_chk.setToolTip(
            "When enabled, START skips all prepared movements before this global step. "
            "Use this after a crash to rerun from the first movement that did not finish."
        )
        self.resume_sequence_step = QtWidgets.QSpinBox()
        self.resume_sequence_step.setRange(1, 100000)
        self.resume_sequence_step.setValue(inferred_step)
        self.resume_sequence_step.setEnabled(False)
        self.resume_sequence_step.setToolTip(
            f"Default inferred from the mirrored last run events: step {inferred_step}."
        )
        self.resume_sequence_chk.toggled.connect(self.resume_sequence_step.setEnabled)
        self.resume_sequence_chk.toggled.connect(self.update_expected_duration_label)
        self.resume_sequence_step.valueChanged.connect(self.update_expected_duration_label)

        row_widget = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(row_widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.resume_sequence_chk)
        row.addWidget(self.resume_sequence_step)
        row.addStretch(1)

        order_parent = self.stim_window.order_list.parentWidget()
        order_layout = order_parent.layout() if order_parent is not None else None
        if isinstance(order_layout, QtWidgets.QVBoxLayout):
            index = order_layout.indexOf(self.expected_duration_label)
            order_layout.insertWidget(index + 1 if index >= 0 else 0, row_widget)

    def install_metadata_panel_controls(self) -> None:
        subject_group = QtWidgets.QGroupBox("Mouse metadata")
        subject_layout = QtWidgets.QGridLayout(subject_group)
        fields = [
            ("age", "Age"),
            ("sex", "Sex"),
            ("lineage", "Lineage"),
            ("genotype", "Genotype"),
            ("husbandry_mouse_number", "Mouse number in husbandry"),
        ]
        self.metadata_edits = {}
        for idx, (key, label) in enumerate(fields):
            edit = QtWidgets.QLineEdit()
            self.metadata_edits[key] = edit
            row = idx // 2
            col = (idx % 2) * 2
            subject_layout.addWidget(QtWidgets.QLabel(label + ":"), row, col)
            subject_layout.addWidget(edit, row, col + 1)

        insertion_group = QtWidgets.QGroupBox("Neuropixels insertions")
        insertion_group.setMaximumWidth(720)
        insertion_layout = QtWidgets.QVBoxLayout(insertion_group)
        self.insertion_table = QtWidgets.QTableWidget(4, 5)
        self.insertion_table.setHorizontalHeaderLabels(["Structure", "Neuropixels ID", "AP (um)", "ML (um)", "DV (um)"])
        self.insertion_table.setToolTip("AP, ML, and DV coordinates are in micrometers.")
        self.insertion_table.verticalHeader().setVisible(False)
        header = self.insertion_table.horizontalHeader()
        header.setStretchLastSection(False)
        for col, width in enumerate([180, 170, 90, 90, 90]):
            header.setSectionResizeMode(col, QtWidgets.QHeaderView.Fixed)
            self.insertion_table.setColumnWidth(col, width)
        self.insertion_table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.insertion_table.setMaximumWidth(650)
        self.insertion_table.setMinimumHeight(150)
        insertion_layout.addWidget(self.insertion_table)

        recording_group = QtWidgets.QGroupBox("Recording metadata")
        metadata_layout = QtWidgets.QGridLayout(recording_group)
        metadata_layout.addWidget(QtWidgets.QLabel("Saved brush/position order (lowest cm -> highest cm):"), 0, 0)
        self.brush_order_edits = []
        for idx, (label_edit, _) in enumerate(self.stim_window.pos_rows):
            brush_edit = QtWidgets.QLineEdit(normalize_position_label(label_edit.text()))
            brush_edit.setPlaceholderText(f"Brush {idx + 1}")
            brush_edit.setToolTip(
                "Saved as metadata only. Entries are ordered by the matching linear position cm; "
                "the first saved entry is the earliest/lowest-cm position."
            )
            self.brush_order_edits.append(brush_edit)
            metadata_layout.addWidget(brush_edit, 0, idx + 1)

        brush_note = QtWidgets.QLabel(
            "Metadata only: the first saved entry is the earliest linear position, meaning the lowest cm value."
        )
        brush_note.setWordWrap(True)
        brush_note.setStyleSheet("color:#9fb4c8;")
        metadata_layout.addWidget(brush_note, 1, 0, 1, len(self.brush_order_edits) + 2)

        self.brush_order_preview_label = QtWidgets.QLabel("")
        self.brush_order_preview_label.setWordWrap(True)
        self.brush_order_preview_label.setStyleSheet("color:#9fb4c8;")
        metadata_layout.addWidget(self.brush_order_preview_label, 2, 0, 1, len(self.brush_order_edits) + 2)

        metadata_layout.setColumnStretch(len(self.brush_order_edits) + 1, 1)

        self.metadata_panel_layout.addWidget(subject_group)
        self.metadata_panel_layout.addWidget(insertion_group)
        self.metadata_panel_layout.addWidget(recording_group)
        self.metadata_panel_layout.addStretch(1)

    def install_stim_sequence_watchers(self) -> None:
        for label_edit, pos_spin in self.stim_window.pos_rows:
            label_edit.textChanged.connect(self.mark_stim_sequence_dirty)
            label_edit.textChanged.connect(self.refresh_null_rotating_controls)
            label_edit.textChanged.connect(self.refresh_brush_order_preview)
            pos_spin.valueChanged.connect(self.mark_stim_sequence_dirty)
            pos_spin.valueChanged.connect(self.refresh_brush_order_preview)
        for token_edit in self.stim_window.rot_rows:
            token_edit.textChanged.connect(self.mark_stim_sequence_dirty)
        for combo in self.null_rotating_combos:
            combo.currentIndexChanged.connect(self.mark_stim_sequence_dirty)
        for edit in self.brush_order_edits:
            edit.textChanged.connect(self.refresh_brush_order_preview)
        self.stim_window.repeats.valueChanged.connect(self.mark_stim_sequence_dirty)
        self.stim_window.repeats.valueChanged.connect(self.refresh_stim_order_drag_mode)
        self.stim_window.scramble.toggled.connect(self.mark_stim_sequence_dirty)
        self.stim_window.scramble.toggled.connect(self.refresh_stim_order_drag_mode)
        self.stim_window.seed_edit.textChanged.connect(self.mark_stim_sequence_dirty)
        self.stim_window.rot_duration.valueChanged.connect(self.mark_stim_sequence_dirty)
        self.stim_window.interval.valueChanged.connect(self.mark_stim_sequence_dirty)
        self.stim_window.lin_move_cm_s.valueChanged.connect(self.update_expected_duration_label)
        if self.interval_range_chk is not None:
            self.interval_range_chk.toggled.connect(self.mark_stim_sequence_dirty)
        if self.interval_min_s is not None:
            self.interval_min_s.valueChanged.connect(self.mark_stim_sequence_dirty)
        if self.interval_max_s is not None:
            self.interval_max_s.valueChanged.connect(self.mark_stim_sequence_dirty)

    def adjust_embedded_layouts(self) -> None:
        self.tighten_camera_form_layout()
        self.equalize_stim_splitter()

    def tighten_camera_form_layout(self) -> None:
        form_layout: QtWidgets.QGridLayout | None = None
        for layout in self.camera_panel.findChildren(QtWidgets.QGridLayout):
            try:
                if layout.indexOf(self.camera_window.output_root) != -1:
                    form_layout = layout
                    break
            except Exception:
                continue
        if form_layout is None:
            return

        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setHorizontalSpacing(8)
        form_layout.setVerticalSpacing(8)
        form_layout.setColumnStretch(0, 0)
        form_layout.setColumnStretch(1, 5)
        form_layout.setColumnStretch(2, 0)
        form_layout.setColumnStretch(3, 2)
        form_layout.setColumnStretch(4, 0)
        form_layout.setColumnStretch(5, 2)

        self.camera_window.trigger_port.setMaximumWidth(170)
        self.camera_window.freq.setMaximumWidth(110)
        self.camera_window.pulse_ms.setMaximumWidth(110)
        self.camera_window.duration_min.setMaximumWidth(130)
        self.camera_window.preview_fps.setMaximumWidth(110)

    def equalize_stim_splitter(self) -> None:
        splitters = self.stim_panel.findChildren(QtWidgets.QSplitter)
        if not splitters:
            return
        splitter = splitters[0]
        splitter.setChildrenCollapsible(False)
        splitter.setSizes([1000, 1000])

    def sync_hidden_output_fields(self) -> None:
        self.stim_window.dir_edit.setText(str(self.recording_sessions_directory()))

    def generate_seed16(self) -> str:
        return "".join(str(secrets.randbelow(10)) for _ in range(16))

    def current_stim_prepare_signature(self) -> str:
        self.normalize_legacy_null_position_labels()
        payload = {
            "positions": [(normalize_position_label(label.text()), float(value.value())) for label, value in self.stim_window.pos_rows],
            "rotary_tokens": [field.text().strip() for field in self.stim_window.rot_rows],
            "no_rotation_rows": self.null_rotating_rows(),
            "interval": self.interval_settings_payload(),
            "rotation_duration_s": float(self.stim_window.rot_duration.value()),
            "repeats": int(self.stim_window.repeats.value()),
            "scramble": bool(self.stim_window.scramble.isChecked()),
            "seed": self.stim_window.seed_edit.text().strip() if self.stim_window.scramble.isChecked() else "",
            "recalibration_policy": "two_per_repeat",
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def mark_stim_sequence_dirty(self, *_: Any) -> None:
        if self._applying_settings:
            return
        self.last_prepared_signature = ""
        if self.stim_window.order_list.count():
            self.stim_window.progress_label.setText("Sequence changed. Click 'Build / randomise sequence'.")
        self.update_expected_duration_label()

    def refresh_stim_order_drag_mode(self, *_: Any) -> None:
        allow_drag = (not self.stim_window.scramble.isChecked()) and int(self.stim_window.repeats.value()) == 1
        mode = (
            QtWidgets.QAbstractItemView.InternalMove
            if allow_drag
            else QtWidgets.QAbstractItemView.NoDragDrop
        )
        self.stim_window.order_list.setDragDropMode(mode)
        if allow_drag:
            self.stim_window.order_list.setToolTip("You can drag to reorder the final sequence when scramble is OFF and repeats = 1.")
        else:
            self.stim_window.order_list.setToolTip(
                "Sequence is locked for display when scramble is ON or repeats are greater than 1."
            )

    def interval_range_enabled(self) -> bool:
        return bool(self.interval_range_chk and self.interval_range_chk.isChecked())

    def interval_bounds_s(self) -> tuple[float, float]:
        if not self.interval_range_enabled():
            value = float(self.stim_window.interval.value())
            return value, value
        low = float(self.interval_min_s.value()) if self.interval_min_s is not None else 0.0
        high = float(self.interval_max_s.value()) if self.interval_max_s is not None else low
        return (low, high) if low <= high else (high, low)

    def expected_interval_s(self) -> float:
        low, high = self.interval_bounds_s()
        return (low + high) / 2.0

    def sampled_interval_s(self, rng: random.Random | None = None) -> float:
        low, high = self.interval_bounds_s()
        if low == high:
            return low
        source = rng if rng is not None else random
        return float(source.uniform(low, high))

    def interval_settings_payload(self) -> dict[str, Any]:
        low, high = self.interval_bounds_s()
        return {
            "mode": "uniform_range" if self.interval_range_enabled() else "fixed",
            "fixed_s": float(self.stim_window.interval.value()),
            "range_min_s": low,
            "range_max_s": high,
        }

    def apply_interval_settings(self, settings: dict[str, Any]) -> None:
        if not isinstance(settings, dict) or not settings:
            return
        mode = str(settings.get("mode", "fixed"))
        if self.interval_range_chk is not None:
            self.interval_range_chk.setChecked(mode == "uniform_range")
        if self.interval_min_s is not None:
            self.set_value(self.interval_min_s, settings.get("range_min_s"))
        if self.interval_max_s is not None:
            self.set_value(self.interval_max_s, settings.get("range_max_s"))
        self.set_value(self.stim_window.interval, settings.get("fixed_s"))

    def insertion_row_payload(self, row: int) -> dict[str, str]:
        if self.insertion_table is None:
            return {}
        headers = ["structure", "neuropixels_id", "ap_um", "ml_um", "dv_um"]
        return {
            headers[col]: (
                self.insertion_table.item(row, col).text().strip()
                if self.insertion_table.item(row, col) is not None
                else ""
            )
            for col in range(self.insertion_table.columnCount())
        }

    def filled_insertion_rows(self) -> list[tuple[int, dict[str, str]]]:
        if self.insertion_table is None:
            return []
        rows: list[tuple[int, dict[str, str]]] = []
        for row in range(self.insertion_table.rowCount()):
            item = self.insertion_row_payload(row)
            if any(item.values()):
                rows.append((row, item))
        return rows

    def experiment_metadata_payload(self) -> dict[str, Any]:
        subject = {key: edit.text().strip() for key, edit in self.metadata_edits.items()}
        insertions = [item for _, item in self.filled_insertion_rows()]
        return {"subject": subject, "insertions": insertions, "insertion_coordinate_units": "um"}

    def apply_experiment_metadata(self, metadata: dict[str, Any]) -> None:
        subject = metadata.get("subject", {}) if isinstance(metadata, dict) else {}
        if isinstance(subject, dict):
            for key, edit in self.metadata_edits.items():
                edit.setText(str(subject.get(key, "")))

        insertions = metadata.get("insertions", []) if isinstance(metadata, dict) else []
        if self.insertion_table is None:
            return
        self.insertion_table.clearContents()
        if not isinstance(insertions, list):
            return
        headers = [
            ("structure", "structure"),
            ("neuropixels_id", "neuropixels_id"),
            ("ap_um", "ap"),
            ("ml_um", "ml"),
            ("dv_um", "dv"),
        ]
        for row, insertion in enumerate(insertions[: self.insertion_table.rowCount()]):
            if not isinstance(insertion, dict):
                continue
            for col, (key, legacy_key) in enumerate(headers):
                self.insertion_table.setItem(
                    row,
                    col,
                    QtWidgets.QTableWidgetItem(str(insertion.get(key, insertion.get(legacy_key, "")))),
                )

    def estimate_sequence_duration_s(self, moves: list[stim_gui.Move]) -> float:
        total = 0.0
        linear_speed = max(0.001, float(self.stim_window.lin_move_cm_s.value()))
        rot_duration = float(self.stim_window.rot_duration.value())
        post_home_wait_s = float(getattr(stim_gui, "POST_HOME_FIRST_STIMULUS_DELAY_S", 0.0) or 0.0)
        chunks: list[tuple[stim_gui.Move | None, list[stim_gui.Move]]] = []
        recalibration: stim_gui.Move | None = None
        chunk: list[stim_gui.Move] = []
        for move in moves:
            if stim_gui.is_recalibration_label(move.pos_label):
                if recalibration is not None or chunk:
                    chunks.append((recalibration, chunk))
                recalibration = move
                chunk = []
            else:
                chunk.append(move)
        if recalibration is not None or chunk:
            chunks.append((recalibration, chunk))

        for _, chunk_moves in chunks:
            total += post_home_wait_s
            current_pos_cm = 0.0
            for idx, move in enumerate(chunk_moves):
                next_move = chunk_moves[idx + 1] if idx + 1 < len(chunk_moves) else None
                total += abs(float(move.pos_cm) - current_pos_cm) / linear_speed
                total += rot_duration
                if next_move is not None:
                    total += abs(float(next_move.pos_cm) - float(move.pos_cm)) / linear_speed
                    total += move_interval_s(move, self.expected_interval_s())
                    current_pos_cm = float(next_move.pos_cm)
                else:
                    current_pos_cm = float(move.pos_cm)
        return total

    def resume_sequence_enabled(self) -> bool:
        return bool(self.resume_sequence_chk is not None and self.resume_sequence_chk.isChecked())

    def resume_start_global_step(self) -> int:
        if self.resume_sequence_step is None:
            return DEFAULT_RESUME_GLOBAL_STEP
        return max(DEFAULT_RESUME_GLOBAL_STEP, int(self.resume_sequence_step.value()))

    def moves_for_resume_setting(self, moves: list[stim_gui.Move]) -> list[stim_gui.Move]:
        if not self.resume_sequence_enabled():
            return list(moves)
        start_step = self.resume_start_global_step()
        return [move for move in moves if int(getattr(move, "global_step", 0)) >= start_step]

    def update_expected_duration_label(self, *_: Any) -> None:
        if self.expected_duration_label is None:
            return
        if not self.stim_window.order_list.count():
            self.expected_duration_label.setText("Expected recording duration: build sequence to calculate.")
            return
        moves = self.prepared_sequence_from_order_list()
        if not moves:
            self.expected_duration_label.setText("Expected recording duration: build sequence to calculate.")
            return
        full_count = len(moves)
        moves = self.moves_for_resume_setting(moves)
        if not moves:
            self.expected_duration_label.setText(
                f"Expected recording duration: resume step {self.resume_start_global_step()} is beyond the prepared sequence ({full_count} moves)."
            )
            return
        seconds = self.estimate_sequence_duration_s(moves)
        minutes = seconds / 60.0
        resume_text = ""
        if self.resume_sequence_enabled():
            skipped = full_count - len(moves)
            resume_text = f"; resume from global step {self.resume_start_global_step()} skips {skipped} move(s)"
        self.expected_duration_label.setText(
            f"Expected recording duration: {minutes:.1f} min ({len(moves)} moves{resume_text}; includes 10 s after calibration/recalibration; one interval after rotation/next-position transition)."
        )

    def camera_session_is_active(self) -> bool:
        stop_btn = getattr(self.camera_window, "stop_btn", None)
        return bool(stop_btn and stop_btn.isEnabled() and self.current_camera_session_dir)

    def relative_to_first_camera_frame_s(self) -> float | None:
        if self.camera_first_frame_monotonic is None:
            return None
        return max(0.0, time.monotonic() - self.camera_first_frame_monotonic)

    def should_auto_start_stim_after_camera(self) -> bool:
        return bool(
            getattr(self, "auto_stim_after_camera_chk", None)
            and self.auto_stim_after_camera_chk.isChecked()
            and self.mode_name() == "both"
        )

    def selected_stim_port(self) -> str:
        port = self.stim_window.port_combo.currentData()
        return str(port or "").strip()

    def unavailable_stim_port_problem(self) -> str | None:
        port = self.selected_stim_port()
        if not port:
            return "Select a somatosensory COM port first."
        try:
            available = {dev for dev, _desc in stim_gui.list_serial_ports()}
        except Exception:
            available = set()
        if port not in available:
            return f"Selected somatosensory COM port {port} is not available. Refresh ports and select the stimulation Arduino."
        return None

    def stim_worker_is_active(self) -> bool:
        thread = getattr(self.stim_window, "thread", None)
        if thread is not None:
            try:
                if thread.isRunning():
                    return True
            except Exception:
                pass
        worker = getattr(self.stim_window, "worker", None)
        return bool(worker and self._active_stim_run_context)

    def request_active_stim_stop(
        self,
        message: str = "User requested stimulation stop.",
        *,
        session_dir: Path | None = None,
        stop_target: str = "stimulation",
    ) -> bool:
        self.cancel_auto_stim_timer()
        context = self._active_stim_run_context
        if context is None:
            return False
        if session_dir is not None and Path(context["session_dir"]) != Path(session_dir):
            return False
        self.record_stim_stop_requested(context, message=message, stop_target=stop_target)
        worker = getattr(self.stim_window, "worker", None)
        if worker:
            worker.abort()
            self.stim_window.log.appendPlainText("Stimulation abort requested...")
            return True
        return False

    def ensure_auto_start_sequence_ready(self) -> str | None:
        port_problem = self.unavailable_stim_port_problem()
        if port_problem:
            return port_problem

        current_signature = self.current_stim_prepare_signature()
        if (
            self.stim_window.order_list.count()
            and self.last_prepared_signature
            and current_signature == self.last_prepared_signature
        ):
            return None

        try:
            moves, seed_used = self.generate_sequence_from_inputs()
            self.populate_prepared_sequence(moves)
            self.last_prepared_signature = self.current_stim_prepare_signature()
            if self.stim_window.scramble.isChecked():
                self.stim_window.log.appendPlainText(
                    f"Auto-start rebuilt sequence: {len(moves)} moves with seed {seed_used}."
                )
            else:
                self.stim_window.log.appendPlainText(
                    f"Auto-start rebuilt sequence: {len(moves)} moves without scrambling."
                )
            return None
        except Exception as exc:
            return f"Could not rebuild somatosensory movement sequence: {exc}"

    def auto_stim_configuration_problem(self) -> str | None:
        port_problem = self.unavailable_stim_port_problem()
        if port_problem:
            return port_problem
        if not self.stim_window.order_list.count():
            return "Prepare the somatosensory movement sequence first."
        current_signature = self.current_stim_prepare_signature()
        if not self.last_prepared_signature or current_signature != self.last_prepared_signature:
            return "Rebuild the somatosensory movement sequence after the latest edits."
        return None

    def cancel_auto_stim_timer(self) -> None:
        if self.auto_stim_timer.isActive():
            self.auto_stim_timer.stop()
        self._auto_stim_scheduled_for_session = False
        self._auto_stim_timer_source = ""

    def observe_camera_thread(self, thread: Any) -> None:
        if thread is not None and thread is not self._observed_camera_thread:
            thread.status.connect(self.on_camera_status_update)
            thread.finished.connect(self.on_camera_session_finished)
            self._observed_camera_thread = thread

    def schedule_auto_stim_after_camera(
        self,
        delay_s: float,
        source: str,
        replace_existing: bool = False,
    ) -> None:
        if not self.current_camera_session_dir:
            return
        if not (self.should_auto_start_stim_after_camera() and self._auto_stim_armed_for_session):
            return
        if self.auto_stim_timer.isActive() and not replace_existing:
            return

        problem = self.ensure_auto_start_sequence_ready()
        if problem:
            self.record_session_stim_event(
                "stim_auto_start_skipped_invalid_configuration",
                session_dir=self.current_camera_session_dir,
                payload={"reason": problem, "source": source},
            )
            self.camera_window.log(f"Somatosensory auto-start skipped: {problem}")
            return

        delay_s = max(0.0, float(delay_s))
        self._auto_stim_scheduled_for_session = True
        self._auto_stim_timer_source = source
        if delay_s <= 0.0:
            self.camera_window.log(f"Somatosensory auto-start firing immediately ({source}).")
            self.record_session_stim_event(
                "stim_auto_start_immediate",
                session_dir=self.current_camera_session_dir,
                payload={"delay_s": 0.0, "source": source},
            )
            self.start_stim_run(auto_trigger=True, trigger_reason="auto_delay_after_camera")
            return

        self.camera_window.log(
            f"Somatosensory auto-start scheduled in {delay_s:.2f}s ({source})."
        )
        self.record_session_stim_event(
            "stim_auto_start_scheduled",
            session_dir=self.current_camera_session_dir,
            payload={"delay_s": delay_s, "source": source},
        )
        self.auto_stim_timer.start(int(round(delay_s * 1000.0)))

    def begin_camera_session_tracking(self, session_dir: Path, thread: Any = None) -> None:
        session_dir = Path(session_dir)
        same_session = self.current_camera_session_dir == session_dir
        if not same_session:
            self.current_camera_session_dir = session_dir
            self.camera_first_frame_wallclock = None
            self.camera_first_frame_monotonic = None
            self.camera_session_started_monotonic = time.monotonic()
            self._auto_stim_armed_for_session = False
            self.cancel_auto_stim_timer()

        self.observe_camera_thread(thread)

        if same_session:
            return

        self.write_session_settings_snapshot(self.current_camera_session_dir)
        if self.should_auto_start_stim_after_camera():
            problem = self.ensure_auto_start_sequence_ready()
            if problem:
                self.camera_window.log(f"Somatosensory auto-start not armed: {problem}")
            else:
                self._auto_stim_armed_for_session = True
                self.camera_window.log(
                    "Somatosensory auto-start is armed and will use the first camera frame when it is detected."
                )
                self.camera_window.log(
                    "Somatosensory auto-start will not fire until at least one camera frame is acquired."
                )

    def after_camera_run_clicked(self) -> None:
        session_dir = getattr(self.camera_window, "session_dir", None)
        if not session_dir:
            return
        if not self.camera_window.stop_btn.isEnabled():
            return
        self.begin_camera_session_tracking(Path(session_dir), getattr(self.camera_window, "camera_thread", None))

    def on_camera_status_update(self, payload: dict[str, Any]) -> None:
        if not self.current_camera_session_dir:
            return
        if self.camera_first_frame_monotonic is not None:
            return

        is_first_frame_event = str(payload.get("_event", "")).strip() == "first_frame"
        any_frames = False
        for key, value in payload.items():
            if str(key).startswith("_"):
                continue
            try:
                if int(value) > 0:
                    any_frames = True
                    break
            except Exception:
                continue
        if not (is_first_frame_event or any_frames):
            return

        wallclock_raw = str(payload.get("_first_frame_at", "")).strip()
        try:
            wallclock = dt.datetime.fromisoformat(wallclock_raw) if wallclock_raw else dt.datetime.now()
        except Exception:
            wallclock = dt.datetime.now()

        self.camera_first_frame_wallclock = wallclock
        self.camera_first_frame_monotonic = time.monotonic()
        self._session_camera_first_frame_at[self.session_key(self.current_camera_session_dir)] = wallclock.isoformat()
        self.record_session_stim_event(
            "camera_first_frame_detected",
            session_dir=self.current_camera_session_dir,
            payload={"first_frame_at": wallclock.isoformat()},
        )
        self.schedule_auto_stim_after_camera(
            float(self.auto_stim_after_camera_delay_s.value()),
            "first camera frame",
            replace_existing=True,
        )

    def fire_auto_stim_after_camera(self) -> None:
        source = self._auto_stim_timer_source or "scheduled timer"
        self._auto_stim_scheduled_for_session = False
        self._auto_stim_timer_source = ""
        if not self.camera_session_is_active():
            if self.current_camera_session_dir:
                self.record_session_stim_event(
                    "stim_auto_start_skipped_camera_inactive",
                    session_dir=self.current_camera_session_dir,
                    payload={"source": source},
                )
            return
        if not self._auto_stim_armed_for_session:
            return
        if self.stim_worker_is_active():
            self.record_session_stim_event(
                "stim_auto_start_skipped_worker_busy",
                session_dir=self.current_camera_session_dir,
                payload={"source": source},
            )
            return
        problem = self.ensure_auto_start_sequence_ready()
        if problem:
            self.record_session_stim_event(
                "stim_auto_start_skipped_invalid_configuration",
                session_dir=self.current_camera_session_dir,
                payload={"reason": problem, "source": source},
            )
            self.camera_window.log(f"Somatosensory auto-start skipped: {problem}")
            return
        self.record_session_stim_event(
            "stim_auto_start_timer_fired",
            session_dir=self.current_camera_session_dir,
            payload={"source": source},
        )
        self.camera_window.log(f"Somatosensory auto-start timer fired ({source}).")
        self.start_stim_run(auto_trigger=True, trigger_reason="auto_delay_after_camera")

    def on_camera_session_finished(self, *_: Any) -> None:
        session_dir = self.current_camera_session_dir
        self.cancel_auto_stim_timer()
        self.current_camera_session_dir = None
        self.camera_first_frame_wallclock = None
        self.camera_first_frame_monotonic = None
        self.camera_session_started_monotonic = None
        self._observed_camera_thread = None
        self._auto_stim_armed_for_session = False
        if session_dir is not None:
            self.write_session_metadata(session_dir)

    def build_base_combo_with_nulls(self) -> list[tuple[str, float, str, float, str]]:
        self.normalize_legacy_null_position_labels()
        positions: list[tuple[int, str, float]] = []
        for idx, (label_edit, pos_spin) in enumerate(self.stim_window.pos_rows):
            label = normalize_position_label(label_edit.text())
            if not label:
                continue
            positions.append((idx, label, float(pos_spin.value())))

        rotcmds = self.stim_window.get_rotcmds()
        if not positions:
            raise ValueError("Add at least one linear position.")
        if not rotcmds:
            raise ValueError("Add at least one rotary speed token.")

        combos: list[tuple[str, float, str, float, str]] = []
        for row_index, pos_label, pos_cm in positions:
            for rot_cmd in rotcmds:
                combos.append((pos_label, pos_cm, rot_cmd.token, rot_cmd.cm_s, rot_cmd.dir))
                if self.null_rotating_enabled_for_row(row_index) and not is_null_brushing_label(pos_label):
                    combos.append(
                        (
                            pos_label,
                            pos_cm,
                            null_rotating_token(rot_cmd.token),
                            0.0,
                            rot_cmd.dir,
                        )
                    )
            if self.null_rotating_enabled_for_row(row_index) and is_null_brushing_label(pos_label):
                combos.append((pos_label, pos_cm, null_rotating_token(""), 0.0, "R"))
        return combos

    def generate_sequence_from_inputs(self) -> tuple[list[stim_gui.Move], str]:
        base = self.build_base_combo_with_nulls()
        repeats = int(self.stim_window.repeats.value())
        scramble = self.stim_window.scramble.isChecked()
        seed_used = self.stim_window.seed_edit.text().strip() if scramble else ""

        if scramble:
            if not seed_used:
                seed_used = self.generate_seed16()
                self.stim_window.seed_edit.setText(seed_used)
            base_seed = stim_gui.seed_to_int(seed_used)
        else:
            base_seed = 0

        moves: list[stim_gui.Move] = []
        prev_order: list[tuple[str, float, str, float, str]] | None = None
        global_step = 1

        for repeat_index in range(1, repeats + 1):
            order = list(base)
            if scramble:
                rs = random.Random(stim_gui.derive_repeat_seed(base_seed, repeat_index))
                rs.shuffle(order)
                if prev_order is not None and order == prev_order:
                    for _ in range(10):
                        rs.shuffle(order)
                        if order != prev_order:
                            break
                prev_order = list(order)
                interval_rng: random.Random | None = random.Random(
                    stim_gui.derive_repeat_seed(base_seed + 1729, repeat_index)
                )
            else:
                interval_rng = None

            middle_after = (len(order) + 1) // 2
            step_in_repeat = 1
            for item_index, (pos_label, pos_cm, rot_token, rot_cm_s, rot_dir) in enumerate(order, start=1):
                moves.append(
                    stim_gui.Move(
                        pos_label=pos_label,
                        pos_cm=float(pos_cm),
                        rot_token=rot_token,
                        rot_cm_s=float(rot_cm_s),
                        rot_dir=rot_dir,
                        repeat_index=repeat_index,
                        step_in_repeat=step_in_repeat,
                        global_step=global_step,
                        interval_s=self.sampled_interval_s(interval_rng),
                    )
                )
                global_step += 1
                step_in_repeat += 1
                if item_index == middle_after:
                    moves.append(
                        stim_gui.Move(
                            pos_label=stim_gui.RECALIBRATION_MIDDLE_LABEL,
                            pos_cm=0.0,
                            rot_token="",
                            rot_cm_s=0.0,
                            rot_dir="R",
                            repeat_index=repeat_index,
                            step_in_repeat=step_in_repeat,
                            global_step=global_step,
                            interval_s=stim_gui.POST_HOME_FIRST_STIMULUS_DELAY_S,
                        )
                    )
                    global_step += 1
                    step_in_repeat += 1

            moves.append(
                stim_gui.Move(
                    pos_label=stim_gui.RECALIBRATION_END_LABEL,
                    pos_cm=0.0,
                    rot_token="",
                    rot_cm_s=0.0,
                    rot_dir="R",
                    repeat_index=repeat_index,
                    step_in_repeat=step_in_repeat,
                    global_step=global_step,
                    interval_s=stim_gui.POST_HOME_FIRST_STIMULUS_DELAY_S,
                )
            )
            global_step += 1

        return moves, seed_used

    def populate_prepared_sequence(self, moves: list[stim_gui.Move]) -> None:
        self.stim_window.order_list.clear()
        for move in moves:
            item = QtWidgets.QListWidgetItem(move_to_text(move))
            item.setData(QtCore.Qt.UserRole, move_to_dict(move))
            self.stim_window.order_list.addItem(item)
        self.refresh_stim_order_drag_mode()
        self.update_expected_duration_label()

    def prepared_sequence_from_order_list(self) -> list[stim_gui.Move]:
        moves: list[stim_gui.Move] = []
        for idx in range(self.stim_window.order_list.count()):
            item = self.stim_window.order_list.item(idx)
            move = move_from_payload(item.data(QtCore.Qt.UserRole), idx)
            if move is None:
                continue
            if float(getattr(move, "interval_s", -1.0)) < 0.0:
                move.interval_s = self.expected_interval_s()
            move.global_step = idx + 1
            moves.append(move)
        return moves

    def sync_order_payloads(self, *_: Any) -> None:
        if not self.stim_window.order_list.count():
            return
        moves = self.prepared_sequence_from_order_list()
        for idx, move in enumerate(moves):
            item = self.stim_window.order_list.item(idx)
            item.setData(QtCore.Qt.UserRole, move_to_dict(move))
            item.setText(move_to_text(move))
        self.update_expected_duration_label()

    def build_stim_sequence_clicked(self) -> None:
        try:
            moves, seed_used = self.generate_sequence_from_inputs()
            self.populate_prepared_sequence(moves)
            self.last_prepared_signature = self.current_stim_prepare_signature()
            repeat_count = int(self.stim_window.repeats.value())
            if self.stim_window.scramble.isChecked():
                self.stim_window.log.appendPlainText(
                    f"Prepared {len(moves)} moves across {repeat_count} repeat(s) with seed {seed_used}."
                )
            else:
                self.stim_window.log.appendPlainText(
                    f"Prepared {len(moves)} moves across {repeat_count} repeat(s) without scrambling."
                )
            expected_min = self.estimate_sequence_duration_s(moves) / 60.0 if moves else 0.0
            self.stim_window.progress_label.setText(f"Prepared {len(moves)} moves. Expected duration {expected_min:.1f} min.")
            self.update_expected_duration_label()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Sequence error", str(exc))

    def session_key(self, session_dir: Path) -> str:
        return str(Path(session_dir))

    def session_stim_events_csv_path(self, session_dir: Path) -> Path:
        return Path(session_dir) / "events.csv"

    def session_metadata_path(self, session_dir: Path) -> Path:
        return Path(session_dir) / "recording_metadata.json"

    def mirror_last_run_logs(self, session_dir: Path) -> None:
        session_dir = Path(session_dir)
        mirrored: dict[str, str] = {}
        sources = [
            (self.session_metadata_path(session_dir), APP_DIR / "last_run_recording_metadata.json"),
            (self.session_stim_events_csv_path(session_dir), APP_DIR / "last_run_events.csv"),
            (session_dir / "logs" / "session_log.txt", APP_DIR / "last_run_session_log.txt"),
            (session_dir / "stimulus_trials.csv", APP_DIR / "last_run_stimulus_trials.csv"),
        ]
        for source, target in sources:
            try:
                if source.exists():
                    target.write_text(source.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
                    mirrored[target.name] = str(source)
            except Exception:
                pass
        try:
            (APP_DIR / "last_run_session_path.txt").write_text(str(session_dir), encoding="utf-8")
            (APP_DIR / "last_run_manifest.json").write_text(
                json.dumps(
                    {
                        "updated": dt.datetime.now().isoformat(),
                        "session_dir": str(session_dir),
                        "mirrored_files": mirrored,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    def write_session_settings_snapshot(self, session_dir: Path) -> tuple[Path, Path]:
        self.write_session_metadata(session_dir)
        return self.session_metadata_path(session_dir), self.session_metadata_path(session_dir)

    def stimulus_trials_csv_path(self, session_dir: Path, run_index: int) -> Path:
        if run_index <= 1:
            return Path(session_dir) / "stimulus_trials.csv"
        return Path(session_dir) / f"stimulus_trials_run_{run_index:03d}.csv"

    def current_brush_order(self) -> list[str]:
        return [str(entry["label"]) for entry in self.current_brush_order_entries()]

    def seed_used_for_current_sequence(self) -> str | None:
        if not self.stim_window.scramble.isChecked():
            return None
        return self.stim_window.seed_edit.text().strip() or None

    def append_session_stim_event_csv(self, session_dir: Path, event: dict[str, Any]) -> None:
        path = self.session_stim_events_csv_path(session_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "timestamp",
            "event_type",
            "run_id",
            "trigger_reason",
            "auto_trigger",
            "relative_to_first_camera_frame_s",
            "done",
            "total",
            "status",
            "message",
            "ok",
            "delay_s",
            "first_frame_at",
            "camera_count",
            "camera_frames_json",
            "camera_result_json",
            "ttl_ok",
            "stop_target",
            "stimulus_trials_csv",
        ]
        write_header = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            writer.writerow({key: event.get(key, "") for key in fieldnames})
        self.mirror_last_run_logs(session_dir)

    def record_session_stim_event(self, event_type: str, session_dir: Path, payload: dict[str, Any] | None = None) -> None:
        event = {
            "timestamp": dt.datetime.now().isoformat(),
            "event_type": event_type,
            "run_id": "",
            "trigger_reason": "",
            "auto_trigger": "",
            "relative_to_first_camera_frame_s": self.relative_to_first_camera_frame_s(),
            "done": "",
            "total": "",
            "status": "",
            "message": "",
            "ok": "",
            "delay_s": "",
            "first_frame_at": "",
            "camera_count": "",
            "camera_frames_json": "",
            "camera_result_json": "",
            "ttl_ok": "",
            "stop_target": "",
            "stimulus_trials_csv": "",
        }
        if payload:
            event.update(payload)
        self.append_session_stim_event_csv(session_dir, event)
        self.write_session_metadata(session_dir)

    def record_camera_stop_requested(self, session_dir: Path) -> None:
        timestamp = dt.datetime.now().isoformat()
        self._session_camera_stop_requested_at[self.session_key(session_dir)] = timestamp
        self.record_session_stim_event(
            "camera_stop_requested",
            session_dir=session_dir,
            payload={"message": "User requested camera stop.", "stop_target": "camera"},
        )

    def record_camera_ttl_finished(self, session_dir: Path, ok: bool, message: str) -> None:
        self._session_ttl_results[self.session_key(session_dir)] = {
            "finished_at": dt.datetime.now().isoformat(),
            "ok": bool(ok),
            "message": str(message),
        }
        self.record_session_stim_event(
            "ttl_finished",
            session_dir=session_dir,
            payload={"ttl_ok": bool(ok), "message": str(message)},
        )

    def record_camera_finished(self, session_dir: Path, result: dict[str, Any]) -> None:
        cameras = list(result.get("cameras", []))
        frames = {str(camera.get("index", "")): int(camera.get("frames", 0)) for camera in cameras}
        self._session_camera_results[self.session_key(session_dir)] = {
            "finished_at": dt.datetime.now().isoformat(),
            **result,
        }
        self.record_session_stim_event(
            "cameras_finished",
            session_dir=session_dir,
            payload={
                "ok": bool(result.get("ok", False)),
                "message": str(result.get("error", "")),
                "camera_count": len(cameras),
                "camera_frames_json": json.dumps(frames, ensure_ascii=False),
                "camera_result_json": json.dumps(result, ensure_ascii=False),
            },
        )

    def build_session_metadata_payload(self, session_dir: Path, status: str) -> dict[str, Any]:
        session_dir = Path(session_dir)
        session_key = self.session_key(session_dir)
        stim_runs = list(self._session_stim_runs.get(self.session_key(session_dir), []))
        camera = self.camera_window
        stim = self.stim_window
        recording_format = selected_recording_format(getattr(camera, "recording_format", None))
        return {
            "schema_version": 8,
            "app": "GUI_stim_and_cam",
            "updated": dt.datetime.now().isoformat(),
            "status": status,
            "session_dir": str(session_dir),
            "mode": self.mode_name(),
            "settings_file": str(self._loaded_settings_path or self.default_saved_settings_path()),
            "camera_first_frame_at": (
                self.camera_first_frame_wallclock.isoformat()
                if self.camera_first_frame_wallclock
                else self._session_camera_first_frame_at.get(self.session_key(session_dir))
            ),
            "camera": {
                "enabled": camera.use_cameras.isChecked(),
                "duration_min": camera.duration_min.value(),
                "camera_hz": camera.freq.value(),
                "preview_fps": camera.preview_fps.value(),
                "pulse_width_ms": camera.pulse_ms.value(),
                "ttl_calibration_ppm": ttl_calibration_ppm_value(camera),
                "ttl_calibration_command_enabled": ttl_calibration_enabled_value(camera),
                "trigger_port": combo_text(camera.trigger_port),
                "trigger_source": getattr(camera, "trigger_source", cam_gui.DEFAULT_TRIGGER_SOURCE),
                "trigger_activation": getattr(camera, "trigger_activation", cam_gui.DEFAULT_TRIGGER_ACTIVATION),
                "recording_format": recording_format,
                "recording_format_label": recording_format_label(recording_format),
                "videos_dir": "videos",
                "stop_requested_at": self._session_camera_stop_requested_at.get(session_key),
                "ttl_result": self._session_ttl_results.get(session_key),
                "camera_result": self._session_camera_results.get(session_key),
            },
            "experiment_metadata": self.experiment_metadata_payload(),
            "somatosensory_stimulation": {
                "brush_order_policy": BRUSH_ORDER_POLICY,
                "brush_order": self.current_brush_order(),
                "brush_order_entries": self.current_brush_order_entries(),
                "no_rotation_positions": self.null_rotating_position_settings(enabled_only=True),
                "interval": self.interval_settings_payload(),
                "resume_from_global_step_enabled": self.resume_sequence_enabled(),
                "resume_from_global_step": self.resume_start_global_step(),
                "interval_between_moves_s": stim.interval.value(),
                "rotation_duration_s": stim.rot_duration.value(),
                "wheel_diameter_cm": stim.wheel_d.value(),
                "linear_homing_speed_cm_s": stim.lin_home_cm_s.value(),
                "linear_move_speed_cm_s": stim.lin_move_cm_s.value(),
                "steps_per_mm": stim.lin_steps_mm.value(),
                "seed": self.seed_used_for_current_sequence(),
                "stop_requested_at": self._session_stim_stop_requested_at.get(session_key),
                "stimulus_trials_csv": "stimulus_trials.csv" if stim_runs else None,
                "events_csv": str(self.session_stim_events_csv_path(session_dir)),
                "runs": stim_runs,
            },
        }

    def write_session_metadata(self, session_dir: Path, status: str | None = None) -> Path:
        session_dir = Path(session_dir)
        existing_status = "running"
        metadata_path = self.session_metadata_path(session_dir)
        if status is None and metadata_path.exists():
            try:
                existing = json.loads(metadata_path.read_text(encoding="utf-8"))
                existing_status = str(existing.get("status", existing_status))
            except Exception:
                pass
        payload = self.build_session_metadata_payload(session_dir, status or existing_status)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.mirror_last_run_logs(session_dir)
        return metadata_path

    def prepare_stim_run_artifacts(
        self,
        moves: list[stim_gui.Move],
        cfg_lines: list[str],
        *,
        trigger_reason: str,
        auto_trigger: bool,
    ) -> dict[str, Any]:
        active_camera_session = self.current_camera_session_dir if self.camera_session_is_active() else None
        if active_camera_session is None:
            session_dir = self.recording_sessions_directory() / self.consume_camera_session_name_for_run()
            if session_dir.exists():
                raise RuntimeError(f"Choose a different session name. This folder already exists:\n{session_dir}")
            session_dir.mkdir(parents=True, exist_ok=True)
        else:
            session_dir = active_camera_session

        session_key = self.session_key(session_dir)
        runs = self._session_stim_runs.setdefault(session_key, [])
        run_index = len(runs) + 1
        run_id = f"run_{run_index:03d}"
        sequence_csv_path = self.stimulus_trials_csv_path(session_dir, run_index)
        write_sequence_csv(sequence_csv_path, moves)

        run_summary = {
            "run_id": run_id,
            "com_port": combo_text(self.stim_window.port_combo),
            "trigger_reason": trigger_reason,
            "auto_trigger": bool(auto_trigger),
            "start_requested_at": dt.datetime.now().isoformat(),
            "finished_at": "",
            "ok": None,
            "message": "",
            "relative_to_first_camera_frame_at_start_s": self.relative_to_first_camera_frame_s(),
            "relative_to_first_camera_frame_at_finish_s": None,
            "stimulus_trials_csv": str(sequence_csv_path),
            "move_count": len(moves),
            "expected_duration_min": self.estimate_sequence_duration_s(moves) / 60.0 if moves else 0.0,
            "cfg_lines": list(cfg_lines),
        }
        runs.append(run_summary)
        self.write_session_metadata(session_dir, "running")

        context = {
            "session_dir": session_dir,
            "run_id": run_id,
            "stimulus_trials_csv": sequence_csv_path,
            "run_summary": run_summary,
        }
        return context

    def record_stim_run_event(
        self,
        context: dict[str, Any],
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        event = {
            "timestamp": dt.datetime.now().isoformat(),
            "event_type": event_type,
            "run_id": context["run_id"],
            "trigger_reason": context["run_summary"].get("trigger_reason", ""),
            "auto_trigger": context["run_summary"].get("auto_trigger", False),
            "relative_to_first_camera_frame_s": self.relative_to_first_camera_frame_s(),
            "done": "",
            "total": "",
            "status": "",
            "message": "",
            "stimulus_trials_csv": str(context.get("stimulus_trials_csv", "")),
        }
        if payload:
            event.update(payload)
        self.append_session_stim_event_csv(Path(context["session_dir"]), event)

    def finalize_stim_run_metadata(self, context: dict[str, Any], ok: bool, message: str) -> None:
        summary = context["run_summary"]
        summary["finished_at"] = dt.datetime.now().isoformat()
        summary["ok"] = bool(ok)
        summary["message"] = message
        summary["relative_to_first_camera_frame_at_finish_s"] = self.relative_to_first_camera_frame_s()
        session_dir = Path(context["session_dir"])
        status = None if self.camera_session_is_active() else "finished"
        self.write_session_metadata(session_dir, status)

    def on_stim_worker_log(self, context: dict[str, Any], message: str) -> None:
        text = str(message).strip()
        if "HOME_OK" in text:
            self.record_stim_run_event(context, "home_ok", {"message": text})
        elif text.startswith("ARDUINO: MOVE_START"):
            self.record_stim_run_event(context, "move_start", {"message": text})
        elif text.startswith("ARDUINO: MOVE_DONE"):
            self.record_stim_run_event(context, "move_done", {"message": text})

    def on_stim_worker_progress(self, context: dict[str, Any], done: int, total: int, status: str) -> None:
        self.record_stim_run_event(
            context,
            "progress",
            {"done": int(done), "total": int(total), "status": status},
        )

    def record_stim_stop_requested(
        self,
        context: dict[str, Any],
        message: str = "User requested stimulation stop.",
        *,
        stop_target: str = "stimulation",
    ) -> None:
        timestamp = dt.datetime.now().isoformat()
        session_dir = Path(context["session_dir"])
        session_key = self.session_key(session_dir)
        if self._session_stim_stop_requested_at.get(session_key):
            return
        self._session_stim_stop_requested_at[session_key] = timestamp
        context["run_summary"]["stop_requested_at"] = timestamp
        self.record_stim_run_event(
            context,
            "stimulation_stop_requested",
            {"message": message, "stop_target": stop_target},
        )
        self.write_session_metadata(session_dir)

    def abort_stim_run(self) -> None:
        stopped = False
        if not self._closing:
            stopped = self.request_active_stim_stop(
                "User requested stimulation stop.",
                stop_target="stimulation",
            )
        if stopped and self.camera_session_is_active():
            self.camera_window.stop_all()

    def on_stim_worker_finished(self, context: dict[str, Any], ok: bool, message: str) -> None:
        self.record_stim_run_event(
            context,
            "finished",
            {"ok": bool(ok), "message": message},
        )
        self.finalize_stim_run_metadata(context, ok, message)
        if self.camera_session_is_active():
            self.camera_window.log(
                f"Somatosensory run {context['run_id']} finished: ok={ok} {message}"
            )
        if self._active_stim_run_context is context:
            self._active_stim_run_context = None
        session_dir = Path(context["session_dir"])
        self.write_session_metadata(session_dir)

    def start_stim_run(self, auto_trigger: bool = False, trigger_reason: str = "manual_start_button") -> None:
        try:
            port_problem = self.unavailable_stim_port_problem()
            if port_problem:
                raise ValueError(port_problem)

            if not self.stim_window.order_list.count():
                raise ValueError("Prepare the movement sequence first.")

            current_signature = self.current_stim_prepare_signature()
            if not self.last_prepared_signature or current_signature != self.last_prepared_signature:
                QtWidgets.QMessageBox.warning(
                    self,
                    "Build the sequence first",
                    "Click 'Build / randomise sequence' after your latest position, no_rotation, interval, rotary, repeat, or scramble edits before pressing START.",
                )
                return

            full_moves = self.prepared_sequence_from_order_list()
            if not full_moves:
                raise ValueError("No moves generated. Build the movement sequence first.")
            moves = self.moves_for_resume_setting(full_moves)
            if not moves:
                raise ValueError(
                    f"Resume step {self.resume_start_global_step()} is beyond the prepared sequence ({len(full_moves)} moves)."
                )
            resume_enabled = self.resume_sequence_enabled()
            resume_start_step = self.resume_start_global_step() if resume_enabled else DEFAULT_RESUME_GLOBAL_STEP
            resume_skipped = len(full_moves) - len(moves)

            cfg_lines = self.stim_window.build_cfg_lines()
            context = self.prepare_stim_run_artifacts(
                moves,
                cfg_lines,
                trigger_reason=trigger_reason,
                auto_trigger=auto_trigger,
            )
            context["run_summary"]["resume_from_global_step_enabled"] = bool(resume_enabled)
            context["run_summary"]["resume_from_global_step"] = int(resume_start_step)
            context["run_summary"]["resume_skipped_move_count"] = int(resume_skipped)
            context["run_summary"]["original_prepared_move_count"] = int(len(full_moves))
            self._active_stim_run_context = context
            if not auto_trigger:
                self._auto_stim_armed_for_session = False
            self.record_stim_run_event(
                context,
                "start_requested",
                {
                    "auto_trigger": bool(auto_trigger),
                    "trigger_reason": trigger_reason,
                },
            )

            port = self.selected_stim_port()
            self.cancel_auto_stim_timer()
            self.stim_window.log.appendPlainText(
                f"Starting run: {len(moves)} moves. Sequence saved: {Path(context['stimulus_trials_csv']).name}"
            )
            if resume_enabled:
                self.stim_window.log.appendPlainText(
                    f"Resume enabled: starting at global step {resume_start_step}; skipped {resume_skipped} prepared move(s)."
                )
                if self.camera_session_is_active():
                    self.camera_window.log(
                        f"Somatosensory resume enabled: starting at global step {resume_start_step}."
                    )
            self.stim_window.log.appendPlainText("First 10 moves:")
            for move in moves[:10]:
                if stim_gui.is_recalibration_label(move.pos_label):
                    self.stim_window.log.appendPlainText(f"  {move.global_step}: recalibration")
                else:
                    self.stim_window.log.appendPlainText(
                        f"  {move.global_step}: {move_display_label(move)} [{move_condition(move)}]  lin={move.pos_cm}  rot={move.rot_cm_s}{move.rot_dir}  interval={move_interval_s(move):g}s"
                    )
            if self.camera_session_is_active():
                self.camera_window.log(
                    f"Somatosensory run {context['run_id']} started ({trigger_reason})."
                )

            self.stim_window.start_btn.setEnabled(False)
            self.stim_window.abort_btn.setEnabled(True)
            self.stim_window.build_btn.setEnabled(False)

            self.stim_window.thread = QtCore.QThread()
            self.stim_window.worker = stim_gui.RunWorker(port, cfg_lines, moves)
            self.stim_window.worker.moveToThread(self.stim_window.thread)
            self.stim_window.worker.log.connect(self.stim_window.log.appendPlainText)
            self.stim_window.worker.log.connect(lambda message, ctx=context: self.on_stim_worker_log(ctx, message))
            self.stim_window.worker.progress.connect(self.stim_window.on_progress)
            self.stim_window.worker.progress.connect(
                lambda done, total, status, ctx=context: self.on_stim_worker_progress(ctx, done, total, status)
            )
            self.stim_window.worker.finished.connect(
                lambda ok, message, ctx=context: self.on_stim_worker_finished(ctx, ok, message)
            )
            self.stim_window.worker.finished.connect(self.stim_window.on_finished)
            self.stim_window.thread.started.connect(self.stim_window.worker.run)
            self.stim_window.thread.start()

        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Start error", str(exc))

    def refresh_ports(self) -> None:
        try:
            self.camera_window.refresh_ports()
        except Exception:
            pass
        try:
            self.stim_window.refresh_ports()
        except Exception:
            pass

    def mode_name(self) -> str:
        if self.mode_camera.isChecked():
            return "camera"
        if self.mode_stim.isChecked():
            return "stim"
        return "both"

    def camera_session_label(self) -> str:
        return "stim_and_cam" if self.mode_name() == "both" else "camera_only"

    def standalone_stim_session_label(self) -> str:
        return "sensory_stim"

    def set_mode(self, mode: str) -> None:
        if mode == "camera":
            self.mode_camera.setChecked(True)
        elif mode == "stim":
            self.mode_stim.setChecked(True)
        else:
            self.mode_both.setChecked(True)
        self.apply_mode()

    def apply_mode(self) -> None:
        mode = self.mode_name()
        camera_enabled = mode in {"camera", "both"}
        stim_enabled = mode in {"stim", "both"}
        self.tabs.setTabEnabled(0, camera_enabled)
        self.tabs.setTabEnabled(1, stim_enabled)
        self.tabs.setTabEnabled(2, True)
        if mode == "camera":
            self.tabs.setCurrentIndex(0)
            self.camera_window.use_cameras.setChecked(True)
            self.camera_window.use_stim.setChecked(False)
        elif mode == "stim":
            self.tabs.setCurrentIndex(1)
            self.camera_window.use_cameras.setChecked(False)
            self.camera_window.use_stim.setChecked(False)
        else:
            self.tabs.setCurrentIndex(0)
            self.camera_window.use_cameras.setChecked(True)
            self.camera_window.use_stim.setChecked(False)
        self.enforce_separate_run_controls()

    def collect_settings(self) -> dict[str, Any]:
        return {
            "schema_version": 6,
            "app": "GUI_stim_and_cam",
            "saved_at": dt.datetime.now().isoformat(),
            "mode": self.mode_name(),
            "settings_save_directory": str(self.settings_export_directory()),
            "recording_sessions_directory": str(self.recording_sessions_directory()),
            "saved_settings_file_path": str(self._loaded_settings_path or self.default_saved_settings_path()),
            "camera": self.collect_camera_settings(),
            "somatosensory_stimulation_device": self.collect_stim_settings(),
            "experiment_metadata": self.experiment_metadata_payload(),
        }

    def collect_camera_settings(self) -> dict[str, Any]:
        w = self.camera_window
        recording_format = selected_recording_format(getattr(w, "recording_format", None))
        return {
            "use_cameras": w.use_cameras.isChecked(),
            "output_root": w.output_root.text(),
            "trigger_port": combo_text(w.trigger_port),
            "frequency_hz": w.freq.value(),
            "pulse_ms": w.pulse_ms.value(),
            "ttl_calibration_ppm": ttl_calibration_ppm_value(w),
            "ttl_calibration_command_enabled": ttl_calibration_enabled_value(w),
            "duration_min": w.duration_min.value(),
            "preview_fps": w.preview_fps.value(),
            "recording_format": recording_format,
            "recording_format_label": recording_format_label(recording_format),
            "camera_settings_source": "spinview_saved_user_set",
            "recording_fps_source": "external_ttl_frequency_hz",
            "roi_source": "camera_internal_memory",
            "legacy_stim_fields": {
                "stim_cfg": w.stim_cfg.toPlainText(),
                "stim_moves": w.stim_moves.toPlainText(),
            },
        }

    def collect_stim_settings(self) -> dict[str, Any]:
        w = self.stim_window
        prepared_valid = bool(
            self.last_prepared_signature
            and self.last_prepared_signature == self.current_stim_prepare_signature()
        )
        return {
            "port": combo_text(w.port_combo),
            "legacy_output_dir": w.dir_edit.text(),
            "cfg_lines": w.build_cfg_lines(),
            "settings": {
                "wheel_d_cm": w.wheel_d.value(),
                "rot_duration_s": w.rot_duration.value(),
                "interval_s": w.interval.value(),
                "interval": self.interval_settings_payload(),
                "repeats": w.repeats.value(),
                "lin_halfrev_cm": w.lin_halfrev_cm.value(),
                "lin_steps_mm": w.lin_steps_mm.value(),
                "lin_autocalc": w.lin_autocalc.isChecked(),
                "lin_home_cm_s": w.lin_home_cm_s.value(),
                "lin_move_cm_s": w.lin_move_cm_s.value(),
                "lin_offset_cm0": w.lin_offset0.value(),
                "rot_microsteps": w.rot_micro.currentText(),
                "hybrid_en": w.hybrid_en.isChecked(),
                "stealth_max": w.stealth_max.value(),
                "spread_min": w.spread_min.value(),
                "rot_i_stealth_mA": w.rot_i_stealth.value(),
                "rot_i_spread_mA": w.rot_i_spread.value(),
                "lin_i_mA": w.lin_i.value(),
                "spread_preset": w.spread_preset.currentText(),
                "dither_en": w.dither_en.isChecked(),
                "dither_min": w.dither_min.value(),
                "dither_amp": w.dither_amp.value(),
                "dither_hz": w.dither_hz.value(),
                "dither_us": w.dither_us.value(),
                "scramble": w.scramble.isChecked(),
                "seed": w.seed_edit.text(),
                "auto_start_after_camera": self.auto_stim_after_camera_chk.isChecked(),
                "auto_start_delay_s": self.auto_stim_after_camera_delay_s.value(),
                "resume_from_global_step_enabled": self.resume_sequence_enabled(),
                "resume_from_global_step": self.resume_start_global_step(),
                "brush_order_policy": BRUSH_ORDER_POLICY,
                "brush_order": self.current_brush_order(),
                "brush_order_entries": self.current_brush_order_entries(),
                "no_rotation_rows": self.null_rotating_rows(),
                "no_rotation_positions": self.null_rotating_position_settings(),
            },
            "positions": [
                {
                    "label": normalize_position_label(label.text()),
                    "cm": value.value(),
                    "add_no_rotation": self.null_rotating_enabled_for_row(idx),
                }
                for idx, (label, value) in enumerate(w.pos_rows)
            ],
            "rotary_tokens": [field.text() for field in w.rot_rows],
            "experiment_metadata": self.experiment_metadata_payload(),
            "prepared_sequence_signature": self.last_prepared_signature,
            "prepared_sequence_valid": prepared_valid,
            "prepared_sequence": [move_to_dict(move) for move in self.prepared_sequence_from_order_list()],
        }

    def apply_settings(self, payload: dict[str, Any]) -> None:
        self._applying_settings = True
        try:
            settings_dir = str(
                payload.get("settings_save_directory")
                or DEFAULT_SETTINGS_DIR
            )
            self.settings_dir_edit.setText(settings_dir)
            recording_dir = str(payload.get("recording_sessions_directory", "")).strip()
            if recording_dir:
                self.camera_window.output_root.setText(recording_dir)

            self.apply_camera_settings(payload.get("camera", {}) or {})

            stim_payload = payload.get("somatosensory_stimulation_device", {})
            if not stim_payload:
                stim_payload = payload.get("stim", {}) or {}
            self.apply_stim_settings(stim_payload)
            metadata = payload.get("experiment_metadata") or stim_payload.get("experiment_metadata") or {}
            self.apply_experiment_metadata(metadata if isinstance(metadata, dict) else {})
            self.set_mode(str(payload.get("mode", "both")))
        finally:
            self._applying_settings = False
            self.refresh_stim_order_drag_mode()
            self.sync_hidden_output_fields()
            self.update_settings_status()

    def apply_camera_settings(self, data: dict[str, Any]) -> None:
        w = self.camera_window
        if not data:
            return
        w.use_cameras.setChecked(bool(data.get("use_cameras", w.use_cameras.isChecked())))
        w.use_stim.setChecked(False)
        output_root = str(
            data.get("output_root")
            or data.get("recording_sessions_directory")
            or self.recording_sessions_directory()
        )
        w.output_root.setText(output_root)
        self.refresh_default_session_name()
        set_combo(w.trigger_port, str(data.get("trigger_port", "")))
        w.freq.setValue(int(data.get("frequency_hz", w.freq.value())))
        w.pulse_ms.setValue(int(data.get("pulse_ms", w.pulse_ms.value())))
        if hasattr(w, "ttl_calibration_ppm"):
            w.ttl_calibration_ppm.setValue(int(data.get("ttl_calibration_ppm", ttl_calibration_ppm_value(w))))
        if hasattr(w, "ttl_calibration_enabled"):
            w.ttl_calibration_enabled.setChecked(bool(data.get("ttl_calibration_command_enabled", False)))
        w.duration_min.setValue(float(data.get("duration_min", w.duration_min.value())))
        w.preview_fps.setValue(int(data.get("preview_fps", w.preview_fps.value())))
        if hasattr(w, "recording_format"):
            set_combo(
                w.recording_format,
                normalize_recording_format(data.get("recording_format", DEFAULT_RECORDING_FORMAT)),
            )
        w.trigger_source = str(data.get("trigger_source", getattr(w, "trigger_source", cam_gui.DEFAULT_TRIGGER_SOURCE)))
        w.trigger_activation = str(
            data.get("trigger_activation", getattr(w, "trigger_activation", cam_gui.DEFAULT_TRIGGER_ACTIVATION))
        )
        legacy = data.get("legacy_stim_fields", {}) or {}
        if "stim_cfg" in legacy:
            w.stim_cfg.setPlainText(str(legacy.get("stim_cfg", "")))
        if "stim_moves" in legacy:
            w.stim_moves.setPlainText(str(legacy.get("stim_moves", "")))
        self.enforce_separate_run_controls()
        w.update_ttl_plot()

    def apply_stim_settings(self, data: dict[str, Any]) -> None:
        w = self.stim_window
        if not data:
            w.order_list.clear()
            self.last_prepared_signature = ""
            self.apply_wrapper_stim_defaults()
            for idx in range(len(self.null_rotating_combos)):
                self.set_null_rotating_for_row(idx, False)
            self.refresh_null_rotating_controls()
            if self.interval_range_chk is not None:
                self.interval_range_chk.setChecked(False)
            self.auto_stim_after_camera_chk.setChecked(False)
            self.auto_stim_after_camera_delay_s.setValue(0.0)
            if self.resume_sequence_chk is not None:
                self.resume_sequence_chk.setChecked(False)
            if self.resume_sequence_step is not None:
                self.resume_sequence_step.setValue(infer_resume_global_step_from_events(APP_DIR / "last_run_events.csv"))
            self.refresh_brush_order_preview()
            return

        set_combo(w.port_combo, str(data.get("port", "")))
        settings = data.get("settings", {}) or {}

        self.set_value(w.wheel_d, settings.get("wheel_d_cm"))
        self.set_value(w.rot_duration, settings.get("rot_duration_s"))
        self.set_value(w.interval, settings.get("interval_s"))
        self.apply_interval_settings(settings.get("interval", {}) or {})
        self.set_value(w.repeats, settings.get("repeats"))
        self.set_value(w.lin_halfrev_cm, settings.get("lin_halfrev_cm"))
        self.set_value(w.lin_steps_mm, settings.get("lin_steps_mm"))
        if "lin_autocalc" in settings:
            w.lin_autocalc.setChecked(bool(settings["lin_autocalc"]))
        self.set_value(w.lin_home_cm_s, settings.get("lin_home_cm_s"))
        self.set_value(w.lin_move_cm_s, settings.get("lin_move_cm_s"))
        self.set_value(w.lin_offset0, settings.get("lin_offset_cm0"))
        set_combo(w.rot_micro, str(settings.get("rot_microsteps", "")))
        if "hybrid_en" in settings:
            w.hybrid_en.setChecked(bool(settings["hybrid_en"]))
        self.set_value(w.stealth_max, settings.get("stealth_max"))
        self.set_value(w.spread_min, settings.get("spread_min"))
        self.set_value(w.rot_i_stealth, settings.get("rot_i_stealth_mA"))
        self.set_value(w.rot_i_spread, settings.get("rot_i_spread_mA"))
        self.set_value(w.lin_i, settings.get("lin_i_mA"))
        set_combo(w.spread_preset, str(settings.get("spread_preset", "")))
        if "dither_en" in settings:
            w.dither_en.setChecked(bool(settings["dither_en"]))
        self.set_value(w.dither_min, settings.get("dither_min"))
        self.set_value(w.dither_amp, settings.get("dither_amp"))
        self.set_value(w.dither_hz, settings.get("dither_hz"))
        self.set_value(w.dither_us, settings.get("dither_us"))
        if "scramble" in settings:
            w.scramble.setChecked(bool(settings["scramble"]))
        w.seed_edit.setText(str(settings.get("seed", w.seed_edit.text())))
        w.on_scramble_toggled(w.scramble.isChecked())
        if "auto_start_after_camera" in settings:
            self.auto_stim_after_camera_chk.setChecked(bool(settings["auto_start_after_camera"]))
        self.set_value(self.auto_stim_after_camera_delay_s, settings.get("auto_start_delay_s"))
        if self.resume_sequence_step is not None:
            self.set_value(
                self.resume_sequence_step,
                settings.get("resume_from_global_step", infer_resume_global_step_from_events(APP_DIR / "last_run_events.csv")),
            )
        if self.resume_sequence_chk is not None:
            self.resume_sequence_chk.setChecked(bool(settings.get("resume_from_global_step_enabled", False)))

        positions = data.get("positions", []) or []
        for idx, row in enumerate(w.pos_rows):
            item = positions[idx] if idx < len(positions) and isinstance(positions[idx], dict) else {}
            row[0].setText(normalize_position_label(str(item.get("label", ""))))
            self.set_value(row[1], item.get("cm", 0.0))
        self.apply_null_rotating_settings(settings, positions)
        self.refresh_null_rotating_controls()

        brush_order = settings.get("brush_order", []) or []
        brush_order_by_row: dict[int, str] = {}
        for entry in settings.get("brush_order_entries", []) or []:
            if not isinstance(entry, dict):
                continue
            try:
                row_index = int(entry.get("row_index", 0)) - 1
                brush_order_by_row[row_index] = normalize_position_label(str(entry.get("label", "")))
            except Exception:
                continue
        if self.brush_order_edits:
            for idx, edit in enumerate(self.brush_order_edits):
                if idx in brush_order_by_row:
                    edit.setText(brush_order_by_row[idx])
                elif idx < len(brush_order):
                    edit.setText(normalize_position_label(str(brush_order[idx])))
                elif idx < len(w.pos_rows):
                    edit.setText(normalize_position_label(w.pos_rows[idx][0].text()))

        rotary_tokens = data.get("rotary_tokens", []) or []
        for idx, line_edit in enumerate(w.rot_rows):
            line_edit.setText(str(rotary_tokens[idx]) if idx < len(rotary_tokens) else "")

        prepared = data.get("prepared_sequence", []) or []
        if not prepared:
            prepared = data.get("order", []) or []
        w.order_list.clear()
        for idx, raw_move in enumerate(prepared):
            move = move_from_payload(raw_move, idx)
            if move is None:
                continue
            if float(getattr(move, "interval_s", -1.0)) < 0.0:
                move.interval_s = self.expected_interval_s()
            item = QtWidgets.QListWidgetItem(move_to_text(move))
            item.setData(QtCore.Qt.UserRole, move_to_dict(move))
            w.order_list.addItem(item)

        if prepared:
            self.last_prepared_signature = str(
                data.get("prepared_sequence_signature") or self.current_stim_prepare_signature()
            )
            w.progress_label.setText(f"Prepared {len(prepared)} moves.")
        else:
            self.last_prepared_signature = ""
            if not any(label.text().strip() for label, _ in w.pos_rows):
                self.apply_wrapper_stim_defaults()

        self.refresh_stim_order_drag_mode()
        self.refresh_brush_order_preview()

    @staticmethod
    def set_value(widget: Any, value: Any) -> None:
        if value is None:
            return
        widget.setValue(value)

    def default_export_json_path(self) -> Path:
        return self.default_saved_settings_path()

    def export_settings_to_default_path(self) -> None:
        self.export_settings(self.default_export_json_path())

    def export_settings(self, json_path: Path, quiet: bool = False) -> None:
        payload = self.collect_settings()
        json_path = Path(json_path)
        csv_path = json_path.with_suffix(".csv")
        sequence_path = json_path.with_name(f"{json_path.stem}_prepared_sequence.csv")

        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        write_config_csv(payload, csv_path)

        prepared_moves = self.prepared_sequence_from_order_list()
        if prepared_moves:
            write_sequence_csv(sequence_path, prepared_moves)
        elif sequence_path.exists():
            sequence_path.unlink()

        self._loaded_settings_path = json_path
        self.update_settings_status(f"Settings saved: {json_path}")
        if not quiet:
            QtWidgets.QMessageBox.information(
                self,
                "Settings saved",
                f"Saved:\n{json_path}\n{csv_path}"
                + (f"\n{sequence_path}" if prepared_moves else ""),
            )

    def export_settings_as(self) -> None:
        suggested = str(self.default_export_json_path())
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Save combined GUI settings",
            suggested,
            "JSON files (*.json)",
        )
        if not path:
            return
        chosen = Path(path)
        self.settings_dir_edit.setText(str(chosen.parent))
        self.export_settings(chosen)

    def load_state(self, path: Path, quiet: bool = False) -> bool:
        path = Path(path)
        if not path.exists():
            if not quiet:
                QtWidgets.QMessageBox.information(self, "Settings", f"No settings file found:\n{path}")
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.apply_settings(payload)
            self._loaded_settings_path = path
            self.persist_directory_state()
            self.update_settings_status(f"Settings loaded: {path}")
            return True
        except Exception as exc:
            if not quiet:
                QtWidgets.QMessageBox.critical(self, "Load settings failed", str(exc))
            return False

    def load_settings_from_dialog(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Load combined GUI settings",
            str(self.settings_export_directory()),
            "JSON files (*.json)",
        )
        if path:
            self.load_state(Path(path))

    def closeEvent(self, event: Any) -> None:
        self._closing = True
        self.persist_directory_state()
        try:
            self.camera_window.stop_all()
        except Exception:
            pass
        try:
            if getattr(self.stim_window, "worker", None):
                self.stim_window.worker.abort()
        except Exception:
            pass
        self.cancel_auto_stim_timer()
        event.accept()


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    apply_dark_theme(app)
    win = CombinedWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

