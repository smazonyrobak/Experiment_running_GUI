# Experiment running GUI

Windows GUI for synchronized FLIR camera recording, camera-trigger TTL generation, and somatosensory stimulus control during Neuropixels experiments.

## Included

- GUI_stim_and_cam.py: combined camera and stimulation GUI
- NeuropixelsGUI.py: camera/TTL module and standalone camera GUI
- GUI_for_smyrator.py: somatosensory controller module and standalone stimulation GUI
- GUI_stim_and_cam_launcher.pyw: no-console Windows launcher
- default_camera.json and default_smyrator.json: editable hardware presets
- Arduino_codes/Camera TTL code/: camera-trigger firmware
- Somatosensory_stim_Arduino_code/: the Arduino firmware for the somatosensory stimulus-control hardware

## Installation

The current rig is tested with Windows and Python 3.10. Install the Teledyne FLIR Spinnaker SDK for the camera, make sure FFmpeg is on PATH, then install the Python dependencies:

    py -3.10 -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install -r requirements.txt

PySpin is supplied by the pinned spinnaker-python package and requires a compatible Spinnaker installation and supported FLIR camera.

## Run

From the activated environment:

    python GUI_stim_and_cam.py

For a desktop shortcut, run:

    .\create_GUI_stim_and_cam_shortcut.ps1

The default data root is %USERPROFILE%\Neuropixels_GUI_data. Override it before launch when needed:

    $env:NEUROPIXELS_GUI_STORAGE_DIR = "D:\Neuropixels_data"
    python GUI_stim_and_cam.py

## Arduino firmware

The camera TTL sketch is Arduino_codes/Camera TTL code/cam_trigger_szymon.ino; edit TRIGGER_PINS to match the rig.

The somatosensory hardware has one firmware sketch: Somatosensory_stim_Arduino_code/Somatosensory_stim_Arduino_code.ino. It targets Arduino UNO R4 hardware and requires these Arduino libraries:

- [U8g2](https://github.com/olikraus/u8g2)
- [TMCStepper](https://github.com/teemuatlut/TMCStepper)
- [GPT_Stepper](https://github.com/delta-G/GPT_Stepper)

## Generated files

Session output, videos, logs, last-run metadata, machine-specific directories, shortcuts, and Python caches are intentionally excluded by .gitignore.

No software license has been selected for this repository.
