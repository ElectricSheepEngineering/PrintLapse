"""Shared helpers for the test suite."""

from __future__ import annotations

import os
import time
from pathlib import Path


FAKE_FFMPEG = Path(__file__).resolve().parent / "fake_ffmpeg.py"


def ensure_fake() -> str:
    mode = FAKE_FFMPEG.stat().st_mode | 0o755
    FAKE_FFMPEG.chmod(mode)
    return str(FAKE_FFMPEG)


def wait_until(predicate, timeout: float = 8, detail=None) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    if detail is None:
        raise AssertionError("timed out")
    raise AssertionError(detail() if callable(detail) else detail)


class Env:
    def __init__(self, **values: str):
        self.values = values
        self.previous: dict[str, str | None] = {}

    def __enter__(self) -> "Env":
        for key, value in self.values.items():
            self.previous[key] = os.environ.get(key)
            os.environ[key] = value
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
