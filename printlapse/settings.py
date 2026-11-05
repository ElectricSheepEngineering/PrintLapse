"""Saved form values and the rules for session names and output folders."""

from __future__ import annotations

import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path


SETTINGS_KEYS = (
    "url",
    "interval_s",
    "video_fps",
    "quality",
    "max_width",
    "output_dir",
    "session_name",
    "max_duration_min",
    "transport",
    "auto_encode",
    "keep_frames",
)

_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def default_settings() -> dict:
    return {
        "url": "",
        "interval_s": 10,
        "video_fps": 30,
        "quality": 3,
        "max_width": 0,
        "output_dir": "",
        "session_name": "",
        "max_duration_min": 0,
        "transport": "tcp",
        "auto_encode": True,
        "keep_frames": True,
    }


def data_root() -> Path | None:
    """Directory that holds both settings and sessions, when one is configured.

    Docker sets PRINTLAPSE_DATA to the mounted volume. Left unset, the
    desktop app keeps using the platform config folder and the home directory.
    """
    raw = os.environ.get("PRINTLAPSE_DATA", "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        path = path.resolve()
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"Could not use PRINTLAPSE_DATA: {exc}") from exc
    return path


def app_config_dir() -> Path:
    root = data_root()
    if root is not None:
        path = root / "config"
    elif sys.platform == "win32":
        base_text = os.environ.get("APPDATA")
        base = Path(base_text) if base_text else Path.home() / "AppData" / "Roaming"
        path = base / "printlapse"
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "printlapse"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg else Path.home() / ".config"
        path = base / "printlapse"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_output_dir() -> Path:
    root = data_root()
    if root is not None:
        path = root / "timelapses"
        path.mkdir(parents=True, exist_ok=True)
        return path
    return Path.home() / "PrintLapse"


def _reject_bool(value, label: str) -> None:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a number.")


def as_float(value, label: str, low: float, high: float) -> float:
    _reject_bool(value, label)
    if isinstance(value, str):
        value = value.strip()
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number.")
    if not math.isfinite(number) or number < low or number > high:
        raise ValueError(f"{label} must be between {low:g} and {high:g}.")
    return number


def as_int(value, label: str, low: int, high: int) -> int:
    _reject_bool(value, label)
    if isinstance(value, str):
        value = value.strip()
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a whole number.")
    if not math.isfinite(number) or not number.is_integer():
        raise ValueError(f"{label} must be a whole number.")
    whole = int(number)
    if whole < low or whole > high:
        raise ValueError(f"{label} must be between {low} and {high}.")
    return whole


def as_bool(value, label: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{label} must be true or false.")


def parse_transport(value) -> str:
    transport = str(value or "tcp").strip().lower()
    if transport not in {"tcp", "udp"}:
        raise ValueError("Transport must be tcp or udp.")
    return transport


def parse_width(value) -> int:
    width = as_int(value, "Frame width", 0, 4096)
    if width != 0 and (width < 160 or width % 2):
        raise ValueError("Frame width must be original, or an even number from 160 to 4096.")
    return width


def sanitize_session_name(name: str, now: datetime | None = None) -> str:
    text = (name or "").strip()
    if not text:
        moment = now or datetime.now()
        return moment.strftime("print-%Y%m%d-%H%M%S")
    text = text.replace("/", "-").replace("\\", "-")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^A-Za-z0-9._ -]+", "-", text)
    text = text.strip(" .-")
    if not text or text in {".", ".."} or ".." in text:
        raise ValueError("Use a session name made of letters, numbers, spaces, dots, or dashes.")
    if text.upper() in _RESERVED_NAMES:
        raise ValueError("That session name is reserved on Windows. Pick another.")
    return text[:80]


def expand_output_dir(raw: str) -> Path:
    text = (raw or "").strip()
    if not text:
        text = str(default_output_dir())
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise ValueError(f"Could not use that output folder: {exc}") from exc
    if resolved.exists() and not resolved.is_dir():
        raise ValueError("The output path is a file. Choose a folder.")
    return resolved


def normalize_settings(data: dict) -> dict:
    """Check a settings object and fill anything the form left out."""
    if not isinstance(data, dict):
        raise ValueError("Settings must be a JSON object.")
    merged = default_settings()
    for key in SETTINGS_KEYS:
        if key in data and data[key] is not None:
            merged[key] = data[key]
    url = str(merged["url"] or "").strip()
    merged["url"] = url
    merged["interval_s"] = as_float(merged["interval_s"], "Frame interval", 0.1, 3600)
    merged["video_fps"] = as_int(merged["video_fps"], "Timelapse frame rate", 1, 120)
    merged["quality"] = as_int(merged["quality"], "JPEG quality", 2, 31)
    merged["max_width"] = parse_width(merged["max_width"])
    merged["output_dir"] = str(merged["output_dir"] or "").strip()
    merged["session_name"] = str(merged["session_name"] or "").strip()
    if merged["session_name"]:
        # Validate early, but keep the user's text. A blank name is filled in
        # when the capture actually starts so the timestamp is current.
        sanitize_session_name(merged["session_name"])
    merged["max_duration_min"] = as_float(
        merged["max_duration_min"], "Stop after", 0, 20160
    )
    merged["transport"] = parse_transport(merged["transport"])
    merged["auto_encode"] = as_bool(merged["auto_encode"], "Build the video when capture stops")
    merged["keep_frames"] = as_bool(merged["keep_frames"], "Keep frame images")
    return merged


class SettingsStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        if not self.path.is_file():
            return default_settings()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default_settings()
        if not isinstance(raw, dict):
            return default_settings()
        try:
            return normalize_settings(raw)
        except ValueError:
            cleaned = default_settings()
            cleaned.update({key: raw[key] for key in SETTINGS_KEYS if key in raw})
            try:
                return normalize_settings(cleaned)
            except ValueError:
                return default_settings()

    def save(self, data: dict) -> dict:
        normalized = normalize_settings(data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(normalized, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)
        return normalized
