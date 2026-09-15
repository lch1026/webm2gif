"""Persisted user preferences (window options, last folders, ffmpeg path)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import APP_NAME
from .hardware import HARDWARE_AUTO
from .options import DEFAULT_PRESET_KEY

ENV_CONFIG_DIR = "WEBM2GIF_CONFIG_DIR"

DEFAULTS: dict[str, Any] = {
    "output_mode": "source",  # "source" or "custom"
    "output_dir": "",
    "preset_key": DEFAULT_PRESET_KEY,
    "fps": None,
    "width": None,
    "loop": True,
    #: Hardware acceleration mode (see :mod:`webm2gif.hardware`).
    "hardware": HARDWARE_AUTO,
    #: Run the scaling filter on the GPU (``scale_vt``).
    "gpu_scale": False,
    #: Parallel conversions; ``0`` means "decide from the CPU core count".
    "workers": 0,
    "ffmpeg_path": "",
    "last_input_dir": "",
    "recursive_folders": True,
}


def config_dir() -> Path:
    """Directory holding ``settings.json`` (overridable for tests)."""
    override = os.environ.get(ENV_CONFIG_DIR)
    if override:
        return Path(override)
    return Path.home() / "Library" / "Application Support" / APP_NAME


def config_path() -> Path:
    return config_dir() / "settings.json"


def load_settings() -> dict[str, Any]:
    """Read settings from disk, falling back to defaults on any problem."""
    values = dict(DEFAULTS)
    try:
        raw = config_path().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return values
    try:
        stored = json.loads(raw)
    except json.JSONDecodeError:
        return values
    if isinstance(stored, dict):
        for key, value in stored.items():
            if key in DEFAULTS:
                values[key] = value
    return values


def save_settings(values: dict[str, Any]) -> bool:
    """Persist settings; returns ``False`` when the location is not writable."""
    payload = {key: values.get(key, DEFAULTS[key]) for key in DEFAULTS}
    try:
        directory = config_dir()
        directory.mkdir(parents=True, exist_ok=True)
        config_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return False
    return True
