"""ffmpeg command building, process control, and JPEG checks.

Capture keeps one RTSP connection open and lets ffmpeg's fps filter keep a
frame every N seconds of wall-clock time. That stays accurate when the camera
frame rate jitters, and it avoids reconnecting for every still.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit


_CREDENTIALS = re.compile(r"(rtsps?://[^/\s:@]+):[^/\s@]+@")
_RTSP_URL = re.compile(r"rtsps?://\S+")
_SOF_MARKERS = {
    0xC0, 0xC1, 0xC2, 0xC3,
    0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB,
    0xCD, 0xCE, 0xCF,
}


def install_hint() -> str:
    if sys.platform == "darwin":
        return "Install ffmpeg with Homebrew: brew install ffmpeg"
    if sys.platform == "win32":
        return "Install ffmpeg with winget: winget install Gyan.FFmpeg"
    return "Install ffmpeg with your package manager, for example: sudo apt install ffmpeg"


def find_ffmpeg() -> str | None:
    """Return an executable ffmpeg path, or None.

    PRINTLAPSE_FFMPEG overrides PATH so a portable build can be used
    without installing it system-wide.
    """
    override = os.environ.get("PRINTLAPSE_FFMPEG", "").strip()
    if override:
        path = Path(override)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        return None
    return shutil.which("ffmpeg")


def run_ffmpeg(args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run ffmpeg and capture its output. No console window on Windows."""
    kwargs: dict = {"capture_output": True, "text": True}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(args, **kwargs)


def probe_ffmpeg(ffmpeg: str | None) -> dict:
    hint = install_hint()
    if not ffmpeg:
        return {
            "available": False,
            "path": None,
            "version": None,
            "libx264": False,
            "hint": hint,
        }
    try:
        version_run = run_ffmpeg([ffmpeg, "-version"], timeout=8)
        encoder_run = run_ffmpeg([ffmpeg, "-hide_banner", "-encoders"], timeout=8)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "available": False,
            "path": ffmpeg,
            "version": None,
            "libx264": False,
            "hint": f"Could not run ffmpeg: {exc}",
        }
    first = (version_run.stdout or "").splitlines()
    version = first[0].strip() if first else "ffmpeg"
    available = version_run.returncode == 0
    return {
        "available": available,
        "path": ffmpeg,
        "version": version,
        "libx264": "libx264" in (encoder_run.stdout or ""),
        "hint": None if available else hint,
    }


def redact_url(url: str) -> str:
    """Hide a password embedded in an RTSP URL. The username stays visible."""
    return _CREDENTIALS.sub(r"\1:***@", url, count=1)


def redact_urls_in_text(text: str) -> str:
    return _RTSP_URL.sub(lambda match: redact_url(match.group(0)), text)


def validate_rtsp_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        raise ValueError("Enter an RTSP URL, such as rtsp://192.168.1.10/live.")
    if "://" not in raw:
        raise ValueError("URL must start with rtsp:// or rtsps://.")
    scheme, rest = raw.split("://", 1)
    scheme = scheme.lower()
    if scheme not in {"rtsp", "rtsps"}:
        raise ValueError("URL must start with rtsp:// or rtsps://.")
    normalized = f"{scheme}://{rest}"
    parts = urlsplit(normalized)
    if not parts.hostname:
        raise ValueError("That URL is missing a host.")
    return normalized


def rate_filter(interval_s: float) -> str:
    """ffmpeg fps filter that keeps one frame per interval.

    A rational (1/10 rather than 0.1) avoids binary float drift. 10 seconds
    becomes fps=1/10, which is one frame every ten seconds of stream time.
    """
    from fractions import Fraction

    if interval_s <= 0:
        raise ValueError("Interval must be greater than zero.")
    fraction = Fraction(interval_s).limit_denominator(1000)
    if fraction <= 0:
        raise ValueError("Interval must be greater than zero.")
    return f"fps={fraction.denominator}/{fraction.numerator}"


def scale_filter(max_width: int) -> str | None:
    if not max_width:
        return None
    width = max_width - (max_width % 2)
    if width < 2:
        raise ValueError("Frame width is too small.")
    # -2 keeps the aspect ratio and forces an even height, which H.264 needs.
    return f"scale={width}:-2"


def video_filter(interval_s: float | None, max_width: int) -> str:
    parts: list[str] = []
    if interval_s is not None:
        parts.append(rate_filter(interval_s))
    scaled = scale_filter(max_width)
    if scaled:
        parts.append(scaled)
    return ",".join(parts)


def build_capture_args(
    ffmpeg: str,
    url: str,
    interval_s: float,
    quality: int,
    max_width: int,
    transport: str,
    start_number: int,
    output_pattern: Path,
) -> list[str]:
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-rtsp_transport",
        transport,
        # Camera timestamps are often wrong. Wall-clock time makes "every N
        # seconds" mean N seconds on the computer running the capture.
        "-use_wallclock_as_timestamps",
        "1",
        "-i",
        url,
        "-an",
    ]
    filters = video_filter(interval_s, max_width)
    if filters:
        args.extend(["-vf", filters])
    args.extend([
        "-q:v",
        str(quality),
        "-start_number",
        str(start_number),
        str(output_pattern),
    ])
    return args


def build_snapshot_args(
    ffmpeg: str,
    url: str,
    quality: int,
    max_width: int,
    transport: str,
    output: Path,
) -> list[str]:
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        transport,
        "-i",
        url,
        "-an",
        "-frames:v",
        "1",
    ]
    filters = video_filter(None, max_width)
    if filters:
        args.extend(["-vf", filters])
    args.extend(["-q:v", str(quality), str(output)])
    return args


def build_encode_args(
    ffmpeg: str,
    frames_dir: Path,
    output: Path,
    fps: int,
) -> list[str]:
    pattern = str(frames_dir / "frame_%06d.jpg")
    return [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-framerate",
        str(fps),
        "-start_number",
        "1",
        "-i",
        pattern,
        # Odd dimensions make yuv420p H.264 fail. Truncate to even numbers.
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-crf",
        "18",
        "-preset",
        "medium",
        "-movflags",
        "+faststart",
        str(output),
    ]


def spawn_ffmpeg(args: list[str]) -> subprocess.Popen:
    """Start ffmpeg in its own session so it can be signalled as a group.

    A new session also keeps a Ctrl+C in the terminal from hitting ffmpeg
    directly. The app stops it itself, then encodes whatever frames are saved.
    """
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
        "bufsize": 0,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    elif os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(args, **kwargs)


def _signal_process(proc: subprocess.Popen, sig: int) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, sig)
        elif sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        return


def stop_process(proc: subprocess.Popen, *, wait: bool = True) -> None:
    """Ask ffmpeg to exit. With wait=False this returns before it is gone."""
    if proc.poll() is not None:
        if wait:
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                return
        return
    _signal_process(proc, signal.SIGTERM)
    if not wait:
        return
    try:
        proc.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        _signal_process(proc, signal.SIGKILL)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return


def pump_stderr(proc: subprocess.Popen, log) -> None:
    """Read ffmpeg progress lines so the pipe cannot fill and stall it."""
    stream = proc.stderr
    if stream is None:
        return
    buffer = b""
    try:
        while True:
            chunk = stream.read(512)
            if not chunk:
                break
            buffer += chunk
            buffer = _emit_lines(buffer, log)
        if buffer.strip():
            _emit_lines(buffer + b"\n", log)
    except Exception:
        return


def _emit_lines(buffer: bytes, log) -> bytes:
    parts = re.split(br"[\r\n]+", buffer)
    for piece in parts[:-1]:
        if piece:
            log(piece.decode("utf-8", "replace"))
    return parts[-1]


def is_complete_jpeg(path: Path) -> bool:
    """True when the file looks like a finished JPEG, not a half-written one."""
    try:
        size = path.stat().st_size
        if size < 100:
            return False
        with path.open("rb") as handle:
            if handle.read(2) != b"\xff\xd8":
                return False
            handle.seek(max(0, size - 16))
            tail = handle.read()
        if b"\xff\xd9" not in tail:
            return False
        return path.stat().st_size == size
    except OSError:
        return False


def jpeg_size(path: Path) -> tuple[int, int] | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return jpeg_size_bytes(data)


def jpeg_size_bytes(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    index = 2
    limit = len(data)
    while index + 3 < limit:
        if data[index] != 0xFF:
            index += 1
            continue
        while index < limit and data[index] == 0xFF:
            index += 1
        if index >= limit:
            return None
        marker = data[index]
        index += 1
        if marker in {0xD8, 0xD9, 0xDA}:
            return None
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if index + 1 >= limit:
            return None
        segment_length = int.from_bytes(data[index:index + 2], "big")
        if segment_length < 2 or index + segment_length > limit:
            return None
        if marker in _SOF_MARKERS and segment_length >= 7:
            height = int.from_bytes(data[index + 3:index + 5], "big")
            width = int.from_bytes(data[index + 5:index + 7], "big")
            if width > 0 and height > 0:
                return width, height
        index += segment_length
    return None
