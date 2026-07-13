from __future__ import annotations

import datetime as dt
import faulthandler
import gc
import json
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

try:
    import PySpin  # type: ignore
except Exception as exc:
    PySpin = None
    PYSPIN_IMPORT_ERROR = exc
else:
    PYSPIN_IMPORT_ERROR = None

try:
    import serial
    from serial.tools import list_ports
except Exception as exc:
    serial = None
    list_ports = None
    SERIAL_IMPORT_ERROR = exc
else:
    SERIAL_IMPORT_ERROR = None


APP_DIR = Path(__file__).resolve().parent
RESULTS_DIR = APP_DIR / "experimental_results"
CAMERA_PRESET = APP_DIR / "default_camera.json"
STIM_PRESET = APP_DIR / "default_smyrator.json"
DEFAULT_TRIGGER_SOURCE = "Line0"
DEFAULT_TRIGGER_ACTIVATION = "RisingEdge"


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def safe_name(text: str, fallback: str) -> str:
    out = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(text).strip())
    return out.strip("._") or fallback


def available_ports() -> list[str]:
    if list_ports is None:
        return []
    return [str(p.device) for p in list_ports.comports()]


def combo_value(combo: QtWidgets.QComboBox) -> str:
    return combo.currentText().strip()


def set_combo(combo: QtWidgets.QComboBox, value: str) -> None:
    if not value:
        return
    idx = combo.findText(value)
    if idx >= 0:
        combo.setCurrentIndex(idx)
    else:
        combo.setEditText(value)


def spin_int(parent: QtWidgets.QWidget, lo: int, hi: int, value: int, suffix: str = "") -> QtWidgets.QSpinBox:
    box = QtWidgets.QSpinBox(parent)
    box.setRange(lo, hi)
    box.setValue(value)
    if suffix:
        box.setSuffix(suffix)
    return box


def spin_float(parent: QtWidgets.QWidget, lo: float, hi: float, value: float, step: float, suffix: str = "") -> QtWidgets.QDoubleSpinBox:
    box = QtWidgets.QDoubleSpinBox(parent)
    box.setRange(lo, hi)
    box.setDecimals(3)
    box.setSingleStep(step)
    box.setValue(value)
    if suffix:
        box.setSuffix(suffix)
    return box


def get_string_node(nodemap: Any, name: str) -> str:
    try:
        node = PySpin.CStringPtr(nodemap.GetNode(name))
        if PySpin.IsReadable(node):
            return str(node.GetValue())
    except Exception:
        pass
    return ""


def get_enum_value(nodemap: Any, name: str) -> str:
    try:
        node = PySpin.CEnumerationPtr(nodemap.GetNode(name))
        if not PySpin.IsReadable(node):
            return ""
        entry = PySpin.CEnumEntryPtr(node.GetCurrentEntry())
        if not PySpin.IsReadable(entry):
            return ""
        return str(entry.GetSymbolic())
    except Exception:
        return ""


def try_set_enum(nodemap: Any, name: str, value: str) -> bool:
    try:
        node = PySpin.CEnumerationPtr(nodemap.GetNode(name))
        if not PySpin.IsReadable(node) or not PySpin.IsWritable(node):
            return False
        entry = PySpin.CEnumEntryPtr(node.GetEntryByName(value))
        if not PySpin.IsReadable(entry):
            return False
        node.SetIntValue(entry.GetValue())
        return True
    except Exception:
        return False


def try_set_bool(nodemap: Any, name: str, value: bool) -> bool:
    try:
        node = PySpin.CBooleanPtr(nodemap.GetNode(name))
        if PySpin.IsWritable(node):
            node.SetValue(bool(value))
            return True
    except Exception:
        pass
    return False


def try_set_float(nodemap: Any, name: str, value: float) -> bool:
    try:
        node = PySpin.CFloatPtr(nodemap.GetNode(name))
        if not PySpin.IsWritable(node):
            return False
        value = max(float(node.GetMin()), min(float(node.GetMax()), float(value)))
        node.SetValue(value)
        return True
    except Exception:
        return False


def try_set_int(nodemap: Any, name: str, value: int) -> bool:
    try:
        node = PySpin.CIntegerPtr(nodemap.GetNode(name))
        if not PySpin.IsWritable(node):
            return False
        inc = max(1, int(node.GetInc()))
        lo, hi = int(node.GetMin()), int(node.GetMax())
        value = max(lo, min(hi, int(value)))
        value = lo + ((value - lo) // inc) * inc
        node.SetValue(value)
        return True
    except Exception:
        return False


def try_execute_command(nodemap: Any, name: str) -> bool:
    try:
        node = PySpin.CCommandPtr(nodemap.GetNode(name))
        if PySpin.IsWritable(node):
            node.Execute()
            return True
    except Exception:
        pass
    return False


def load_default_userset(nodemap: Any) -> bool:
    del nodemap
    return False


def set_centered_roi(nodemap: Any, width: int, height: int) -> None:
    del nodemap, width, height
    return


def configure_free_run(nodemap: Any, preview_fps: int, width: int, height: int) -> None:
    del nodemap, preview_fps, width, height
    return


def read_float_node(nodemap: Any, name: str) -> Optional[float]:
    try:
        node = PySpin.CFloatPtr(nodemap.GetNode(name))
        if PySpin.IsReadable(node):
            return float(node.GetValue())
    except Exception:
        pass
    return None


def read_bool_node(nodemap: Any, name: str) -> Optional[bool]:
    try:
        node = PySpin.CBooleanPtr(nodemap.GetNode(name))
        if PySpin.IsReadable(node):
            return bool(node.GetValue())
    except Exception:
        pass
    return None


def set_camera_acquisition_fps_for_ttl_run(nodemap: Any, requested_fps: float) -> dict[str, Any]:
    requested_fps = float(requested_fps)
    saved = {
        "acquisition_frame_rate_enable": read_bool_node(nodemap, "AcquisitionFrameRateEnable"),
        "acquisition_frame_rate": read_float_node(nodemap, "AcquisitionFrameRate"),
    }
    try_set_enum(nodemap, "TriggerMode", "Off")
    try_set_bool(nodemap, "AcquisitionFrameRateEnable", True)

    node = PySpin.CFloatPtr(nodemap.GetNode("AcquisitionFrameRate"))
    if not PySpin.IsReadable(node) or not PySpin.IsWritable(node):
        raise RuntimeError("Camera AcquisitionFrameRate is not writable; cannot apply UI FPS.")
    fps_min = float(node.GetMin())
    fps_max = float(node.GetMax())
    if requested_fps < fps_min - 0.01 or requested_fps > fps_max + 0.01:
        raise RuntimeError(
            f"Requested UI FPS {requested_fps:.3f} is outside this camera's writable range "
            f"{fps_min:.3f}-{fps_max:.3f}."
        )
    node.SetValue(requested_fps)
    actual_fps = float(node.GetValue())
    resulting_fps = read_float_node(nodemap, "AcquisitionResultingFrameRate") or actual_fps
    if abs(actual_fps - requested_fps) > 0.25 or resulting_fps + 0.25 < requested_fps:
        raise RuntimeError(
            f"Camera cannot run requested UI FPS {requested_fps:.3f}: "
            f"actual={actual_fps:.3f}, resulting={resulting_fps:.3f}."
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
    try_set_enum(nodemap, "TriggerMode", "Off")
    saved_fps = saved_state.get("acquisition_frame_rate")
    if saved_fps is not None:
        try_set_bool(nodemap, "AcquisitionFrameRateEnable", True)
        try_set_float(nodemap, "AcquisitionFrameRate", float(saved_fps))
    saved_enable = saved_state.get("acquisition_frame_rate_enable")
    if saved_enable is not None:
        try_set_bool(nodemap, "AcquisitionFrameRateEnable", bool(saved_enable))


def configure_hardware_trigger(
    nodemap: Any,
    width: int,
    height: int,
    trigger_source: str,
    trigger_activation: str,
) -> None:
    del width, height
    source = trigger_source or DEFAULT_TRIGGER_SOURCE
    activation = trigger_activation or DEFAULT_TRIGGER_ACTIVATION

    try_set_enum(nodemap, "TriggerMode", "Off")
    try_set_enum(nodemap, "TriggerSelector", "FrameStart")
    try_set_enum(nodemap, "LineSelector", "Line0")
    try_set_enum(nodemap, "LineMode", "Input")
    try_set_enum(nodemap, "TriggerOverlap", "ReadOut")
    if not try_set_enum(nodemap, "TriggerSource", source):
        raise RuntimeError(f"Could not set camera TriggerSource={source!r}")
    if not try_set_enum(nodemap, "TriggerActivation", activation):
        raise RuntimeError(f"Could not set camera TriggerActivation={activation!r}")
    if not try_set_enum(nodemap, "TriggerMode", "On"):
        raise RuntimeError("Could not enable camera TriggerMode=On")

    actual_selector = get_enum_value(nodemap, "TriggerSelector")
    actual_source = get_enum_value(nodemap, "TriggerSource")
    actual_activation = get_enum_value(nodemap, "TriggerActivation")
    actual_mode = get_enum_value(nodemap, "TriggerMode")
    if actual_mode != "On" or actual_selector != "FrameStart" or actual_source != source or actual_activation != activation:
        raise RuntimeError(
            "Camera frame-trigger verification failed: "
            f"TriggerSelector={actual_selector or '-'} "
            f"TriggerMode={actual_mode or '-'} "
            f"TriggerSource={actual_source or '-'} "
            f"TriggerActivation={actual_activation or '-'}"
        )


def image_to_gray(image: Any) -> np.ndarray:
    if image.GetPixelFormatName() == "Mono8":
        return image.GetNDArray()
    converted = image.Convert(PySpin.PixelFormat_Mono8, PySpin.HQ_LINEAR)
    try:
        return converted.GetNDArray().copy()
    finally:
        converted.Release()


def is_timeout(exc: Exception) -> bool:
    text = str(exc).lower()
    return "timeout" in text or "-1011" in text or "spinnaker_err_timeout" in text


def grid_image(frames: list[Optional[np.ndarray]], labels: list[str], tile_h: int = 240) -> Optional[np.ndarray]:
    tiles: list[np.ndarray] = []
    for frame, label in zip(frames, labels):
        if frame is None:
            body = np.full((tile_h, int(tile_h * 1.35)), 20, dtype=np.uint8)
            cv2.putText(body, "Waiting for TTL", (18, tile_h // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.65, 220, 2, cv2.LINE_AA)
        else:
            body = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            scale = tile_h / max(1, body.shape[0])
            body = cv2.resize(body, (max(1, int(body.shape[1] * scale)), tile_h), interpolation=cv2.INTER_AREA)
        tile = cv2.copyMakeBorder(body, 28, 0, 0, 0, cv2.BORDER_CONSTANT, value=35)
        cv2.putText(tile, label[:48], (8, 19), cv2.FONT_HERSHEY_SIMPLEX, 0.48, 240, 1, cv2.LINE_AA)
        tiles.append(tile)

    if not tiles:
        return None

    h = max(t.shape[0] for t in tiles)
    padded = [cv2.copyMakeBorder(t, 0, h - t.shape[0], 0, 0, cv2.BORDER_CONSTANT, value=35) for t in tiles]
    gap = np.full((h, 10), 35, dtype=np.uint8)
    out = padded[0]
    for tile in padded[1:]:
        out = np.hstack((out, gap, tile))
    return out


def gray_to_qimage(frame: np.ndarray) -> QtGui.QImage:
    frame = np.ascontiguousarray(frame)
    qimg = QtGui.QImage(frame.data, frame.shape[1], frame.shape[0], frame.strides[0], QtGui.QImage.Format_Grayscale8)
    return qimg.copy()


def release_camera(cam: Any, nodemap: Any) -> None:
    del nodemap
    try:
        cam.EndAcquisition()
    except Exception:
        pass
    try:
        cam.DeInit()
    except Exception:
        pass


def release_system(cam_list: Any, system: Any) -> None:
    gc.collect()
    try:
        if cam_list is not None:
            cam_list.Clear()
    finally:
        gc.collect()
        system.ReleaseInstance()


class FfmpegWriter:
    def __init__(self, folder: Path, stem: str, width: int, height: int, fps: int, segment_s: int = 3600):
        self.folder = folder
        self.stem = stem
        self.width = int(width)
        self.height = int(height)
        self.fps = int(fps)
        self.segment_s = int(segment_s)
        self.log_path = folder / f"{stem}_ffmpeg.log"
        self.path = folder / f"{stem}.mp4"
        self.pattern = folder / f"{stem}_%03d.mp4"
        self.proc: Optional[subprocess.Popen] = None
        self.log_file: Optional[Any] = None

    def start(self) -> None:
        cmd = [
            "ffmpeg",
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
            "-r",
            str(self.fps),
            "-i",
            "-",
            "-c:v",
            "hevc_nvenc",
            "-preset",
            "slow",
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
        self.log_file.write(" ".join(cmd) + "\n")
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=self.log_file,
            stderr=self.log_file,
            bufsize=8 * 1024 * 1024,
        )
        time.sleep(0.2)
        if self.proc.poll() is not None:
            raise RuntimeError(f"FFmpeg exited early. See {self.log_path}")

    def write(self, frame: np.ndarray) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("FFmpeg writer is not open")
        self.proc.stdin.write(np.ascontiguousarray(frame).tobytes(order="C"))

    def close(self) -> None:
        proc = self.proc
        self.proc = None
        try:
            if proc and proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        if proc:
            try:
                proc.wait(timeout=20)
            except Exception:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
        if self.log_file:
            self.log_file.write(f"----- ffmpeg end {dt.datetime.now().isoformat()} -----\n")
            self.log_file.close()
            self.log_file = None


@dataclass
class CameraRuntime:
    info: dict[str, Any]
    folder: Path
    stem: str
    frames: int = 0
    last_frame: Optional[np.ndarray] = None
    writer: Optional[FfmpegWriter] = None
    ts_file: Optional[Any] = None
    error: str = ""


class PreviewThread(QtCore.QThread):
    preview = QtCore.Signal(QtGui.QImage)
    log = QtCore.Signal(str)
    stopped = QtCore.Signal()

    def __init__(self, fps: int, width: int, height: int):
        super().__init__()
        self.fps = int(fps)
        self.width = int(width)
        self.height = int(height)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if PySpin is None:
            self.log.emit(f"PySpin unavailable: {PYSPIN_IMPORT_ERROR}")
            self.stopped.emit()
            return

        system = PySpin.System.GetInstance()
        cam_list = None
        active: list[tuple[Any, Any, dict[str, Any], Optional[np.ndarray]]] = []

        try:
            cam_list = system.GetCameras()
            detected = int(cam_list.GetSize())
            self.log.emit(f"Preview: PySpin detected {detected} camera(s).")

            for idx in range(detected):
                cam = cam_list.GetByIndex(idx)
                tl = cam.GetTLDeviceNodeMap()
                info = {
                    "index": idx,
                    "serial": get_string_node(tl, "DeviceSerialNumber"),
                    "model": get_string_node(tl, "DeviceModelName"),
                }
                cam.Init()
                nodemap = cam.GetNodeMap()
                load_default_userset(nodemap)
                configure_free_run(nodemap, self.fps, self.width, self.height)
                cam.BeginAcquisition()
                active.append((cam, nodemap, info, None))

            if not active:
                self.log.emit("Preview: no cameras available.")
                return

            delay = 1.0 / max(1, self.fps)
            last_emit = 0.0

            while not self._stop.is_set():
                for i, (cam, nodemap, info, last) in enumerate(active):
                    image = None
                    try:
                        image = cam.GetNextImage(20)
                        if not image.IsIncomplete():
                            last = image_to_gray(image).copy()
                            active[i] = (cam, nodemap, info, last)
                    except PySpin.SpinnakerException as exc:
                        if not is_timeout(exc):
                            self.log.emit(f"Preview {info.get('serial') or info.get('model')}: {exc}")
                    finally:
                        if image is not None:
                            image.Release()

                if time.monotonic() - last_emit >= delay:
                    last_emit = time.monotonic()
                    frame = grid_image(
                        [x[3] for x in active],
                        [f"Cam {x[2]['index'] + 1} {x[2].get('serial') or x[2].get('model')}" for x in active],
                    )
                    if frame is not None:
                        self.preview.emit(gray_to_qimage(frame))

        except Exception as exc:
            self.log.emit(f"Preview error: {exc}")

        finally:
            while active:
                cam, nodemap, info, last = active.pop()
                release_camera(cam, nodemap)
                cam = None
                nodemap = None
                info = None
                last = None

            try:
                release_system(cam_list, system)
            except Exception as exc:
                self.log.emit(f"Preview cleanup error: {exc}")

            self.stopped.emit()


class CameraRunThread(QtCore.QThread):
    armed = QtCore.Signal(int, list)
    preview = QtCore.Signal(QtGui.QImage)
    log = QtCore.Signal(str)
    status = QtCore.Signal(dict)
    finished = QtCore.Signal(dict)

    def __init__(
        self,
        session_dir: Path,
        fps: int,
        width: int,
        height: int,
        preview_fps: int,
        trigger_source: str,
        trigger_activation: str,
    ):
        super().__init__()
        self.session_dir = session_dir
        self.fps = int(fps)
        self.width = int(width)
        self.height = int(height)
        self.preview_fps = int(preview_fps)
        self.trigger_source = trigger_source or DEFAULT_TRIGGER_SOURCE
        self.trigger_activation = trigger_activation or DEFAULT_TRIGGER_ACTIVATION
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if PySpin is None:
            self.log.emit(f"PySpin unavailable: {PYSPIN_IMPORT_ERROR}")
            self.finished.emit({"ok": False, "error": str(PYSPIN_IMPORT_ERROR), "cameras": []})
            return

        system = PySpin.System.GetInstance()
        cam_list = None
        active: list[tuple[Any, Any, CameraRuntime]] = []
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
                        "serial": get_string_node(tl, "DeviceSerialNumber"),
                        "model": get_string_node(tl, "DeviceModelName"),
                    }
                    label = f"cam_{idx + 1:02d}_{safe_name(info['serial'] or info['model'], 'camera')}"
                    runtime = CameraRuntime(
                        info=info,
                        folder=cam_root / label,
                        stem=f"{self.session_dir.name}_cam{idx + 1:02d}",
                    )
                    runtime.folder.mkdir(parents=True, exist_ok=True)
                    runtime.ts_file = (runtime.folder / f"{runtime.stem}_timestamps.txt").open("w", encoding="utf-8", buffering=1)

                    cam.Init()
                    nodemap = cam.GetNodeMap()
                    runtime.camera_requested_fps_state = set_camera_acquisition_fps_for_ttl_run(nodemap, self.fps)
                    configure_hardware_trigger(
                        nodemap,
                        self.width,
                        self.height,
                        self.trigger_source,
                        self.trigger_activation,
                    )

                    self.log.emit(
                        f"Camera {idx + 1} armed: "
                        f"serial={info['serial'] or '-'} "
                        f"model={info['model'] or '-'} "
                        f"UiFps={runtime.camera_requested_fps_state['actual_fps']:.3f} "
                        f"ResultingFps={runtime.camera_requested_fps_state['resulting_fps']:.3f} "
                        f"TriggerMode={get_enum_value(nodemap, 'TriggerMode') or '-'} "
                        f"TriggerSource={get_enum_value(nodemap, 'TriggerSource') or '-'} "
                        f"TriggerActivation={get_enum_value(nodemap, 'TriggerActivation') or '-'}"
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

            while not self._stop.is_set():
                for cam, nodemap, rt in active:
                    image = None
                    try:
                        image = cam.GetNextImage(20)
                        if image.IsIncomplete():
                            continue

                        frame = image_to_gray(image)
                        ts = int(image.GetTimeStamp())

                        if rt.writer is None:
                            h, w = frame.shape
                            rt.writer = FfmpegWriter(rt.folder, rt.stem, w, h, self.fps)
                            rt.writer.start()
                            self.log.emit(f"Recording started: cam {rt.info['index'] + 1} {w}x{h} -> {rt.folder}")

                        rt.writer.write(frame)
                        if rt.ts_file:
                            rt.ts_file.write(f"{ts}\n")
                        rt.frames += 1
                        rt.last_frame = frame.copy()

                    except PySpin.SpinnakerException as exc:
                        if not is_timeout(exc):
                            rt.error = str(exc)
                            self.log.emit(f"Camera {rt.info['index'] + 1} error: {exc}")
                    except Exception as exc:
                        rt.error = str(exc)
                        self.log.emit(f"Camera {rt.info['index'] + 1} write error: {exc}")
                    finally:
                        if image is not None:
                            image.Release()

                now = time.monotonic()

                if now - last_preview >= 1.0 / max(1, self.preview_fps):
                    last_preview = now
                    frame = grid_image(
                        [rt.last_frame for _, _, rt in active],
                        [f"Cam {rt.info['index'] + 1} {rt.info.get('serial') or rt.info.get('model')}" for _, _, rt in active],
                    )
                    if frame is not None:
                        self.preview.emit(gray_to_qimage(frame))

                if now - last_status >= 1.0:
                    last_status = now
                    self.status.emit({str(rt.info["index"]): rt.frames for _, _, rt in active})

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
                    self.log.emit(f"Camera {rt.info['index'] + 1} FPS restore warning: {exc}")
                release_camera(cam, nodemap)

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

                result["cameras"].append(
                    {
                        "index": rt.info["index"],
                        "serial": rt.info.get("serial", ""),
                        "model": rt.info.get("model", ""),
                        "frames": rt.frames,
                        "folder": str(rt.folder),
                        "error": rt.error,
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
                release_system(cam_list, system)
            except Exception as exc:
                self.log.emit(f"Run cleanup error: {exc}")

            self.finished.emit(result)


class TTLThread(QtCore.QThread):
    log = QtCore.Signal(str)
    finished = QtCore.Signal(bool, str)

    def __init__(self, port: str, freq_hz: int, pulse_ms: int, count: int, baud: int = 115200):
        super().__init__()
        self.port = port
        self.freq_hz = int(freq_hz)
        self.pulse_ms = int(pulse_ms)
        self.count = int(count)
        self.baud = int(baud)
        self._stop = threading.Event()
        self._ser: Optional[Any] = None

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ser:
                self._ser.write(b"STOP\n")
                self._ser.flush()
        except Exception:
            pass

    def run(self) -> None:
        if serial is None:
            self.finished.emit(False, f"pyserial unavailable: {SERIAL_IMPORT_ERROR}")
            return

        try:
            with serial.Serial(self.port, self.baud, timeout=0.2) as ser:
                self._ser = ser
                time.sleep(2.0)

                try:
                    ser.reset_input_buffer()
                    ser.reset_output_buffer()
                except Exception:
                    pass

                ser.write(b"STOP\n")
                ser.flush()
                time.sleep(0.25)

                while ser.in_waiting:
                    line = ser.readline().decode(errors="ignore").strip()
                    if line:
                        self.log.emit(f"TTL Arduino: {line}")

                cmd = f"{self.freq_hz} {self.pulse_ms} {self.count}"
                self.log.emit(f"TTL command: {cmd}")
                ser.write((cmd + "\n").encode("ascii"))
                ser.flush()

                deadline = time.time() + (self.count / max(1, self.freq_hz)) + 10.0
                while not self._stop.is_set() and time.time() < deadline:
                    line = ser.readline().decode(errors="ignore").strip()
                    if not line:
                        continue
                    self.log.emit(f"TTL Arduino: {line}")
                    if line.startswith("ERR"):
                        self.finished.emit(False, line)
                        return
                    if line == "DONE":
                        self.finished.emit(True, "DONE from Arduino serial; camera frame counts confirm actual TTL detection")
                        return

                self.finished.emit(False, "Stopped" if self._stop.is_set() else "Timeout waiting for DONE")

        except Exception as exc:
            self.finished.emit(False, str(exc))

        finally:
            self._ser = None


@dataclass
class Move:
    pos_cm: float
    rot_cm_s: float
    rot_dir: str
    label: str


class StimThread(QtCore.QThread):
    log = QtCore.Signal(str)
    ready_for_ttl = QtCore.Signal()
    progress = QtCore.Signal(int, int, str)
    finished = QtCore.Signal(bool, str)

    def __init__(self, port: str, cfg_lines: list[str], moves: list[Move], baud: int = 115200):
        super().__init__()
        self.port = port
        self.cfg_lines = cfg_lines
        self.moves = moves
        self.baud = baud
        self._stop = threading.Event()
        self._ser: Optional[Any] = None

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ser:
                self._ser.write(b"ABORT\n")
                self._ser.flush()
        except Exception:
            pass

    def read_line(self, timeout_s: float) -> Optional[str]:
        deadline = time.time() + timeout_s
        while not self._stop.is_set() and time.time() < deadline:
            line = self._ser.readline().decode(errors="ignore").strip()
            if line:
                return line
        return None

    def run(self) -> None:
        if serial is None:
            self.finished.emit(False, f"pyserial unavailable: {SERIAL_IMPORT_ERROR}")
            return

        try:
            with serial.Serial(self.port, self.baud, timeout=0.3) as ser:
                self._ser = ser
                time.sleep(2.2)

                ser.write(b"PING\n")
                ser.flush()

                self.log.emit("Waiting for READY...")
                ready = False
                deadline = time.time() + 15.0
                while time.time() < deadline and not self._stop.is_set():
                    line = ser.readline().decode(errors="ignore").strip()
                    if line:
                        self.log.emit(f"Stim Arduino: {line}")
                    if line == "READY":
                        ready = True
                        break

                if not ready:
                    self.finished.emit(False, "Did not receive READY")
                    return

                for line in self.cfg_lines:
                    ser.write((line.rstrip() + "\n").encode())
                    ser.flush()
                    time.sleep(0.01)

                ser.write(b"CFG_END\n")
                ser.flush()

                line = self.read_line(6.0)
                if line != "CFG_OK":
                    self.finished.emit(False, f"Expected CFG_OK, got {line}")
                    return
                self.log.emit("CFG_OK")

                total = len(self.moves)
                ser.write(f"RUN_BEGIN {total}\n".encode())
                ser.flush()

                line = self.read_line(6.0)
                if line != "RUN_OK":
                    self.finished.emit(False, f"Expected RUN_OK, got {line}")
                    return
                self.log.emit("RUN_OK; homing")

                while not self._stop.is_set():
                    line = self.read_line(120.0)
                    if line is None:
                        self.finished.emit(False, "Timeout waiting for HOME_OK")
                        return
                    self.log.emit(f"Stim Arduino: {line}")
                    if line == "HOME_OK":
                        self.ready_for_ttl.emit()
                        break
                    if "FAIL" in line:
                        self.finished.emit(False, line)
                        return

                idx = 0
                while idx < total and not self._stop.is_set():
                    line = self.read_line(90.0)
                    if line is None:
                        self.finished.emit(False, "Timeout waiting for READY_MOVE")
                        return

                    self.log.emit(f"Stim Arduino: {line}")

                    if line.startswith("READY_MOVE"):
                        move = self.moves[idx]
                        cmd = f"MOVE {move.pos_cm:.3f} {move.rot_cm_s:.3f} {move.rot_dir} {move.label}"
                        ser.write((cmd + "\n").encode())
                        ser.flush()
                        self.progress.emit(idx, total, f"Sent {move.label}")
                    elif line.startswith("MOVE_DONE"):
                        idx += 1
                        self.progress.emit(idx, total, "Completed")
                    elif line.startswith("MOVE_FAIL") or line.startswith("ERR") or "FAIL" in line:
                        self.finished.emit(False, line)
                        return
                    elif line == "RUN_DONE":
                        break

                self.finished.emit(not self._stop.is_set(), "Stim complete" if not self._stop.is_set() else "Stopped")

        except Exception as exc:
            self.finished.emit(False, str(exc))

        finally:
            self._ser = None


class TTLPlotWidget(QtWidgets.QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.freq_hz = 60
        self.pulse_ms = 4
        self.count = 0
        self.setMinimumHeight(130)

    def set_values(self, freq_hz: int, pulse_ms: int, count: int) -> None:
        self.freq_hz = int(freq_hz)
        self.pulse_ms = int(pulse_ms)
        self.count = int(count)
        self.update()

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor("#10151d"))
        p.setRenderHint(QtGui.QPainter.Antialiasing)

        margin = 18
        w = max(1, self.width() - 2 * margin)
        y_low, y_high = self.height() - 34, 38
        period_ms = 1000.0 / max(1, self.freq_hz)
        high_frac = min(0.95, self.pulse_ms / period_ms)
        cycles = 8
        step = w / cycles

        p.setPen(QtGui.QPen(QtGui.QColor("#7fd7ff"), 2))
        x = margin
        path = QtGui.QPainterPath(QtCore.QPointF(x, y_low))
        for _ in range(cycles):
            path.lineTo(x, y_high)
            path.lineTo(x + step * high_frac, y_high)
            path.lineTo(x + step * high_frac, y_low)
            x += step
            path.lineTo(x, y_low)
        p.drawPath(path)

        p.setPen(QtGui.QColor("#d7e7f5"))
        p.drawText(margin, 20, f"Commanded TTL: {self.freq_hz} Hz, {self.pulse_ms} ms pulse, {self.count} pulses")
        p.setPen(QtGui.QColor("#9fb4c8"))
        p.drawText(margin, self.height() - 10, "Commanded TTL only. Camera run preserves the saved SpinView hardware-trigger settings.")


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Neuropixels Minimal Rig Control")
        self.resize(1180, 860)

        self.preview_thread: Optional[PreviewThread] = None
        self.camera_thread: Optional[CameraRunThread] = None
        self.ttl_thread: Optional[TTLThread] = None
        self.stim_thread: Optional[StimThread] = None

        self.session_dir: Optional[Path] = None
        self.camera_armed = False
        self.stim_ready = False
        self.ttl_started = False
        self.trigger_source = DEFAULT_TRIGGER_SOURCE
        self.trigger_activation = DEFAULT_TRIGGER_ACTIVATION

        self._build_ui()
        self._load_defaults()
        self.refresh_ports()

    def _build_ui(self) -> None:
        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        layout = QtWidgets.QVBoxLayout(root)

        top = QtWidgets.QHBoxLayout()
        self.use_cameras = QtWidgets.QCheckBox("Use cameras")
        self.use_cameras.setChecked(True)
        self.use_stim = QtWidgets.QCheckBox("Use sensory stimulation")
        top.addWidget(self.use_cameras)
        top.addWidget(self.use_stim)
        top.addStretch(1)

        self.refresh_btn = QtWidgets.QPushButton("Refresh ports")
        self.refresh_btn.clicked.connect(self.refresh_ports)
        top.addWidget(self.refresh_btn)
        layout.addLayout(top)

        info = QtWidgets.QLabel(
            "Run mode enforces camera hardware trigger from default_camera.json "
            f"({DEFAULT_TRIGGER_SOURCE}/{DEFAULT_TRIGGER_ACTIVATION} by default)."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color:#9fb4c8;")
        layout.addWidget(info)

        form = QtWidgets.QGridLayout()

        self.output_root = QtWidgets.QLineEdit(str(RESULTS_DIR))
        browse = QtWidgets.QPushButton("Browse")
        browse.clicked.connect(self.browse_output)
        form.addWidget(QtWidgets.QLabel("Output root"), 0, 0)
        form.addWidget(self.output_root, 0, 1, 1, 4)
        form.addWidget(browse, 0, 5)

        self.trigger_port = QtWidgets.QComboBox()
        self.trigger_port.setEditable(True)

        self.stim_port = QtWidgets.QComboBox()
        self.stim_port.setEditable(True)

        self.freq = spin_int(self, 1, 500, 60, " Hz")
        self.pulse_ms = spin_int(self, 1, 100, 4, " ms")
        self.duration_min = spin_float(self, 0.01, 1000000.0, 30.0, 1.0, " min")
        self.preview_fps = spin_int(self, 1, 30, 5, " fps")
        self.width = spin_int(self, 64, 8192, 1280)
        self.height = spin_int(self, 64, 8192, 1280)

        form.addWidget(QtWidgets.QLabel("Trigger COM"), 1, 0)
        form.addWidget(self.trigger_port, 1, 1)
        form.addWidget(QtWidgets.QLabel("Freq"), 1, 2)
        form.addWidget(self.freq, 1, 3)
        form.addWidget(QtWidgets.QLabel("Pulse"), 1, 4)
        form.addWidget(self.pulse_ms, 1, 5)

        form.addWidget(QtWidgets.QLabel("Duration"), 2, 0)
        form.addWidget(self.duration_min, 2, 1)
        form.addWidget(QtWidgets.QLabel("Preview"), 2, 2)
        form.addWidget(self.preview_fps, 2, 3)
        form.addWidget(QtWidgets.QLabel("Stim COM"), 2, 4)
        form.addWidget(self.stim_port, 2, 5)

        form.addWidget(QtWidgets.QLabel("Camera ROI"), 3, 0)
        form.addWidget(self.width, 3, 1)
        form.addWidget(self.height, 3, 2)

        layout.addLayout(form)

        self.ttl_plot = TTLPlotWidget()
        layout.addWidget(self.ttl_plot)

        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)

        left = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left)

        self.preview_label = QtWidgets.QLabel("Preview")
        self.preview_label.setAlignment(QtCore.Qt.AlignCenter)
        self.preview_label.setStyleSheet("background:#0b0d11;color:#d7e7f5;border:1px solid #273344;")
        self.preview_label.setFixedSize(760, 420)
        self.preview_label.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
        self.preview_label.setScaledContents(False)
        left_layout.addWidget(self.preview_label, alignment=QtCore.Qt.AlignCenter)

        buttons = QtWidgets.QHBoxLayout()
        self.preview_btn = QtWidgets.QPushButton("Start live preview")
        self.preview_btn.clicked.connect(self.toggle_preview)
        self.run_btn = QtWidgets.QPushButton("Run")
        self.run_btn.clicked.connect(self.start_run)
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop_all)
        self.stop_btn.setEnabled(False)
        buttons.addWidget(self.preview_btn)
        buttons.addWidget(self.run_btn)
        buttons.addWidget(self.stop_btn)
        left_layout.addLayout(buttons)

        split.addWidget(left)

        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.addWidget(QtWidgets.QLabel("Stim CFG lines"))
        self.stim_cfg = QtWidgets.QPlainTextEdit()
        self.stim_cfg.setMinimumHeight(150)
        right_layout.addWidget(self.stim_cfg)
        right_layout.addWidget(QtWidgets.QLabel("Stim moves: pos_cm rot_cm_s dir label"))
        self.stim_moves = QtWidgets.QPlainTextEdit()
        self.stim_moves.setPlaceholderText("Example:\n1.0 8.0 CW whisker_A\n2.0 8.0 CCW whisker_B")
        right_layout.addWidget(self.stim_moves)

        split.addWidget(right)
        split.setSizes([760, 360])
        layout.addWidget(split)

        self.log_box = QtWidgets.QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(3000)
        layout.addWidget(self.log_box, stretch=1)

        for widget in (self.freq, self.pulse_ms, self.duration_min):
            widget.valueChanged.connect(self.update_ttl_plot)
        self.update_ttl_plot()

    def _load_defaults(self) -> None:
        cam = read_json(CAMERA_PRESET)
        hw = cam.get("hardware_trigger", {})
        acq = cam.get("camera_acquisition", {})

        set_combo(self.trigger_port, str(hw.get("com_port", "COM3")))
        self.freq.setValue(int(hw.get("frequency_hz", 60)))
        self.pulse_ms.setValue(int(hw.get("pulse_ms", 4)))
        self.trigger_source = str(hw.get("trigger_source", DEFAULT_TRIGGER_SOURCE))
        self.trigger_activation = str(hw.get("trigger_activation", DEFAULT_TRIGGER_ACTIVATION))
        self.width.setValue(int(acq.get("target_width", 1280)))
        self.height.setValue(int(acq.get("target_height", 1280)))
        self.preview_fps.setValue(int(acq.get("preview_max_fps", 5)))
        self.output_root.setText(str(acq.get("save_root_dir", RESULTS_DIR)))

        stim = read_json(STIM_PRESET)
        set_combo(self.stim_port, str(stim.get("com_port", "")))
        self.stim_cfg.setPlainText("\n".join(default_stim_cfg(stim)))
        self.stim_moves.setPlainText("")

    def refresh_ports(self) -> None:
        old_trigger = combo_value(self.trigger_port)
        old_stim = combo_value(self.stim_port)
        ports = available_ports()
        for combo, old in ((self.trigger_port, old_trigger), (self.stim_port, old_stim)):
            combo.clear()
            combo.addItems(ports)
            set_combo(combo, old)

    def browse_output(self) -> None:
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Output root", self.output_root.text())
        if path:
            self.output_root.setText(path)

    def ttl_count(self) -> int:
        return max(1, int(round(self.freq.value() * self.duration_min.value() * 60.0)))

    def update_ttl_plot(self) -> None:
        self.ttl_plot.set_values(self.freq.value(), self.pulse_ms.value(), self.ttl_count())

    def log(self, text: str) -> None:
        line = f"[{dt.datetime.now().strftime('%H:%M:%S')}] {text}"
        print(line, flush=True)
        self.log_box.appendPlainText(line)
        if self.session_dir:
            try:
                log_dir = self.session_dir / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                session_log = log_dir / "session_log.txt"
                with session_log.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
                try:
                    (APP_DIR / "last_run_session_log.txt").write_text(
                        session_log.read_text(encoding="utf-8", errors="replace"),
                        encoding="utf-8",
                    )
                    (APP_DIR / "last_run_session_path.txt").write_text(str(self.session_dir), encoding="utf-8")
                except Exception:
                    pass
            except Exception:
                pass

    def set_preview(self, image: QtGui.QImage) -> None:
        pix = QtGui.QPixmap.fromImage(image)
        pix = pix.scaled(
            self.preview_label.width(),
            self.preview_label.height(),
            QtCore.Qt.KeepAspectRatio,
            QtCore.Qt.SmoothTransformation,
        )
        self.preview_label.setPixmap(pix)

    def toggle_preview(self) -> None:
        if self.preview_thread and self.preview_thread.isRunning():
            self.preview_thread.stop()
            self.preview_btn.setEnabled(False)
            return

        if not self.use_cameras.isChecked():
            self.log("Preview ignored: cameras disabled.")
            return

        self.preview_label.setText("Preview")
        self.preview_label.setPixmap(QtGui.QPixmap())

        self.preview_thread = PreviewThread(self.preview_fps.value(), self.width.value(), self.height.value())
        self.preview_thread.preview.connect(self.set_preview)
        self.preview_thread.log.connect(self.log)
        self.preview_thread.stopped.connect(self.preview_stopped)
        self.preview_thread.start()
        self.preview_btn.setText("Stop live preview")

    def preview_stopped(self) -> None:
        self.preview_btn.setEnabled(True)
        self.preview_btn.setText("Start live preview")

    def start_run(self) -> None:
        if self.preview_thread and self.preview_thread.isRunning():
            self.preview_thread.stop()
            self.preview_thread.wait(5000)

        if self.use_cameras.isChecked() and shutil.which("ffmpeg") is None:
            QtWidgets.QMessageBox.critical(self, "Missing FFmpeg", "ffmpeg is not on PATH.")
            return

        if serial is None:
            QtWidgets.QMessageBox.critical(self, "Missing serial", str(SERIAL_IMPORT_ERROR))
            return

        if not self.use_cameras.isChecked() and not self.use_stim.isChecked():
            QtWidgets.QMessageBox.warning(self, "Nothing enabled", "Enable cameras, sensory stimulation, or both.")
            return

        if self.use_cameras.isChecked() or self.use_stim.isChecked():
            trigger_port = combo_value(self.trigger_port)
            ports = available_ports()
            if not trigger_port or trigger_port not in ports:
                self.refresh_ports()
                QtWidgets.QMessageBox.critical(
                    self,
                    "Trigger port not available",
                    f"Selected trigger port is '{trigger_port or '(none)'}', but available ports are: "
                    f"{', '.join(ports) if ports else '(none)'}.\n\n"
                    "Select the Arduino/TTL trigger port before starting the run.",
                )
                return

        self.session_dir = Path(self.output_root.text().strip() or RESULTS_DIR) / f"{now_stamp()}_Neuropixels_minimal"
        self.session_dir.mkdir(parents=True, exist_ok=True)

        self.log_box.clear()
        self.preview_label.setText("Preview")
        self.preview_label.setPixmap(QtGui.QPixmap())

        self.camera_armed = not self.use_cameras.isChecked()
        self.stim_ready = not self.use_stim.isChecked()
        self.ttl_started = False

        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.preview_btn.setEnabled(False)

        self.write_metadata("starting")
        self.log(f"Session: {self.session_dir}")

        if self.use_cameras.isChecked():
            self.camera_thread = CameraRunThread(
                self.session_dir,
                fps=self.freq.value(),
                width=self.width.value(),
                height=self.height.value(),
                preview_fps=self.preview_fps.value(),
                trigger_source=self.trigger_source,
                trigger_activation=self.trigger_activation,
            )
            self.camera_thread.armed.connect(self.on_cameras_armed)
            self.camera_thread.preview.connect(self.set_preview)
            self.camera_thread.log.connect(self.log)
            self.camera_thread.status.connect(lambda s: self.log(f"Camera frames: {s}"))
            self.camera_thread.finished.connect(self.on_cameras_finished)
            self.camera_thread.start()

        if self.use_stim.isChecked():
            try:
                moves = parse_moves(self.stim_moves.toPlainText())
            except Exception as exc:
                self.log(f"Stim moves invalid: {exc}")
                self.stop_all()
                return

            self.stim_thread = StimThread(combo_value(self.stim_port), clean_lines(self.stim_cfg.toPlainText()), moves)
            self.stim_thread.log.connect(self.log)
            self.stim_thread.ready_for_ttl.connect(self.on_stim_ready)
            self.stim_thread.progress.connect(lambda done, total, status: self.log(f"Stim {done}/{total}: {status}"))
            self.stim_thread.finished.connect(self.on_stim_finished)
            self.stim_thread.start()

        self.maybe_start_ttl()

    def on_cameras_armed(self, count: int, infos: list) -> None:
        self.log(f"Cameras armed: {count}")
        if count <= 0:
            self.camera_armed = False
            self.log("TTL not started because cameras are enabled but none armed.")
            if self.stim_thread and self.stim_thread.isRunning():
                self.stim_thread.stop()
            return
        self.camera_armed = True
        self.maybe_start_ttl()

    def on_stim_ready(self) -> None:
        self.stim_ready = True
        self.log("Stim reached HOME_OK; ready for TTL.")
        self.maybe_start_ttl()

    def maybe_start_ttl(self) -> None:
        if self.ttl_started or not (self.camera_armed and self.stim_ready):
            return

        port = combo_value(self.trigger_port)
        if not port:
            self.log("No trigger COM port selected.")
            self.stop_all()
            return
        if port not in available_ports():
            self.log(f"Trigger COM port {port} is not available.")
            self.stop_all()
            return

        self.ttl_started = True
        self.ttl_thread = TTLThread(port, self.freq.value(), self.pulse_ms.value(), self.ttl_count())
        self.ttl_thread.log.connect(self.log)
        self.ttl_thread.finished.connect(self.on_ttl_finished)
        self.ttl_thread.start()
        self.write_metadata("running")
        self.log("TTL started.")

    def on_ttl_finished(self, ok: bool, message: str) -> None:
        self.log(f"TTL finished: ok={ok} {message}")
        if self.camera_thread and self.camera_thread.isRunning():
            self.camera_thread.stop()
        if not ok and self.stim_thread and self.stim_thread.isRunning():
            self.stim_thread.stop()
        if not self.use_stim.isChecked():
            self.finish_if_idle()

    def on_cameras_finished(self, result: dict) -> None:
        self.log(f"Cameras finished: {json.dumps(result, ensure_ascii=False)}")
        if self.use_cameras.isChecked() and not result.get("ok", False):
            if self.stim_thread and self.stim_thread.isRunning():
                self.stim_thread.stop()
        self.finish_if_idle()

    def on_stim_finished(self, ok: bool, message: str) -> None:
        self.log(f"Stim finished: ok={ok} {message}")
        self.finish_if_idle()

    def finish_if_idle(self) -> None:
        busy = any(t and t.isRunning() for t in (self.camera_thread, self.ttl_thread, self.stim_thread))
        if busy:
            return
        self.write_metadata("finished")
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.preview_btn.setEnabled(True)
        self.log("Session finished.")

    def stop_all(self) -> None:
        for thread in (self.ttl_thread, self.stim_thread, self.camera_thread, self.preview_thread):
            if thread and thread.isRunning():
                thread.stop()
        self.log("Stop requested.")
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.preview_btn.setEnabled(True)
        self.write_metadata("stopped")

    def write_metadata(self, status: str) -> None:
        if not self.session_dir:
            return
        if getattr(self, "_combined_owner", None) is not None:
            return

        payload = {
            "status": status,
            "updated": dt.datetime.now().isoformat(),
            "ttl": {
                "port": combo_value(self.trigger_port),
                "frequency_hz": self.freq.value(),
                "pulse_ms": self.pulse_ms.value(),
                "count": self.ttl_count(),
                "duration_min": self.duration_min.value(),
            },
            "camera": {
                "enabled": self.use_cameras.isChecked(),
                "width": self.width.value(),
                "height": self.height.value(),
                "run_mode": "force_hardware_trigger_settings",
                "trigger_source": self.trigger_source,
                "trigger_activation": self.trigger_activation,
            },
            "stim": {
                "enabled": self.use_stim.isChecked(),
                "port": combo_value(self.stim_port),
            },
        }

        try:
            (self.session_dir / "recording_metadata.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self.stop_all()
        for thread in (self.preview_thread, self.camera_thread, self.ttl_thread, self.stim_thread):
            if thread:
                thread.wait(5000)
        event.accept()


def clean_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def parse_moves(text: str) -> list[Move]:
    moves: list[Move] = []
    for line in clean_lines(text):
        if line.upper().startswith("MOVE "):
            line = line[5:]
        parts = line.split()
        if len(parts) < 4:
            raise ValueError(f"Bad move line: {line!r}")
        moves.append(Move(float(parts[0]), float(parts[1]), parts[2], "_".join(parts[3:])))
    if not moves:
        raise ValueError("No stimulus moves provided.")
    return moves


def default_stim_cfg(payload: dict[str, Any]) -> list[str]:
    s = payload.get("settings", {}) if isinstance(payload, dict) else {}
    get = s.get
    return [
        f"CFG WHEEL_D_CM {float(get('wheel_d_cm', 5.0)):.4f}",
        f"CFG LIN_STEPS_PER_MM {float(get('lin_steps_mm', 14.1593)):.6f}",
        f"CFG LIN_HOME_CM_S {float(get('lin_home_cm_s', 3.0)):.4f}",
        f"CFG LIN_MOVE_CM_S {float(get('lin_move_cm_s', 5.0)):.4f}",
        f"CFG LIN_OFFSET_CM0 {float(get('lin_offset_cm0', 0.5)):.3f}",
        f"CFG ROT_MICROSTEPS {int(get('rot_microsteps', 128))}",
        "CFG LIN_MICROSTEPS 16",
        f"CFG ROT_DUR_S {float(get('rot_duration_s', 2.5)):.4f}",
        f"CFG INTERVAL_S {float(get('interval_s', 0.25)):.4f}",
        f"CFG HYBRID_EN {1 if bool(get('hybrid_en', True)) else 0}",
        f"CFG STEALTH_MAX {float(get('stealth_max', 8.0)):.4f}",
        f"CFG SPREAD_MIN {float(get('spread_min', 8.0)):.4f}",
        f"CFG ROT_I_STEALTH {int(get('rot_i_stealth_mA', 350))}",
        f"CFG ROT_I_SPREAD {int(get('rot_i_spread_mA', 400))}",
        f"CFG LIN_I {int(get('lin_i_mA', 500))}",
        f"CFG SPREAD_PRESET {int(get('spread_preset', 1))}",
        f"CFG DITHER_EN {1 if bool(get('dither_en', True)) else 0}",
        f"CFG DITHER_MIN {float(get('dither_min', 8.0)):.4f}",
        f"CFG DITHER_AMP {float(get('dither_amp', 0.0035)):.6f}",
        f"CFG DITHER_HZ {float(get('dither_hz', 30.0)):.3f}",
        f"CFG DITHER_US {int(get('dither_us', 2000))}",
    ]


def main() -> int:
    faulthandler.enable()

    app = QtWidgets.QApplication(sys.argv)
    app.setStyleSheet(
        """
        QWidget { background:#161b22; color:#d7e7f5; font-size:10pt; }
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {
            background:#0f131a; border:1px solid #2d3a4c; border-radius:5px; padding:4px;
        }
        QPushButton { background:#24415a; border:1px solid #41627f; border-radius:6px; padding:7px 12px; }
        QPushButton:hover { background:#2d526f; }
        QPushButton:disabled { color:#718092; background:#1b222d; }
        QCheckBox { spacing:8px; }
        """
    )

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

