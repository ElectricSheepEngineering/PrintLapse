#!/usr/bin/env python3
"""Stand-in for ffmpeg used by the tests. It writes tiny JPEG files."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def make_jpeg(width: int = 32, height: int = 18) -> bytes:
    """A JPEG-shaped byte string with a SOF0 segment. Browsers will not show it."""
    sof = bytes([
        0xFF, 0xC0,
        0x00, 0x0B,
        0x08,
        (height >> 8) & 0xFF, height & 0xFF,
        (width >> 8) & 0xFF, width & 0xFF,
        0x01, 0x01, 0x11, 0x00,
    ])
    return b"\xff\xd8" + (b"\x00" * 120) + sof + b"\xff\xd9"


def _write(path: str, payload: bytes) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)


def main(argv: list[str]) -> int:
    args = argv[1:]
    record = os.environ.get("FAKE_FFMPEG_ARGV")
    if record:
        Path(record).write_text("\n".join(argv), encoding="utf-8")
    if args == ["-version"]:
        print("ffmpeg version fake-1.0")
        return 0
    if "-encoders" in args:
        print(" V..... libx264 H.264 / AVC")
        return 0
    mode = os.environ.get("FAKE_FFMPEG_MODE", "stream")
    if mode == "fail":
        sys.stderr.write("Connection refused\n")
        return 1
    if "-frames:v" in args:
        _write(args[-1], make_jpeg())
        return 0
    if "-c:v" in args:
        _write(args[-1], b"\x00\x00\x00\x18ftypisom")
        return 0
    if mode == "hang":
        while True:
            time.sleep(0.2)
    start = 1
    if "-start_number" in args:
        start = int(args[args.index("-start_number") + 1])
    pattern = args[-1]
    delay = float(os.environ.get("FAKE_FFMPEG_DELAY", "0.08"))
    number = start
    while True:
        _write(pattern.replace("%06d", f"{number:06d}"), make_jpeg())
        number += 1
        time.sleep(delay)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        sys.exit(0)
