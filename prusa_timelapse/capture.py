"""Capture stills from an RTSP stream and encode them into a timelapse.

One ffmpeg process stays connected and writes a JPEG on the requested
interval. If the stream drops, the next process continues the frame numbers
so the finished sequence has no gaps. Stopping the capture encodes whatever
frames were saved.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from prusa_timelapse.ffmpeg_util import (
    build_capture_args,
    build_encode_args,
    build_snapshot_args,
    find_ffmpeg,
    is_complete_jpeg,
    jpeg_size,
    probe_ffmpeg,
    pump_stderr,
    redact_url,
    run_ffmpeg,
    redact_urls_in_text,
    spawn_ffmpeg,
    stop_process,
    validate_rtsp_url,
)
from prusa_timelapse.settings import (
    expand_output_dir,
    sanitize_session_name,
)


class BusyError(RuntimeError):
    """Raised when a capture, test, or encode is already running."""


@dataclass
class CaptureConfig:
    url: str
    interval_s: float
    video_fps: int
    quality: int
    max_width: int
    transport: str
    output_dir: Path
    session_name: str
    max_duration_s: float | None
    auto_encode: bool
    keep_frames: bool


def config_from_dict(data: dict) -> CaptureConfig:
    minutes = float(data["max_duration_min"])
    return CaptureConfig(
        url=validate_rtsp_url(str(data["url"])),
        interval_s=float(data["interval_s"]),
        video_fps=int(data["video_fps"]),
        quality=int(data["quality"]),
        max_width=int(data["max_width"]),
        transport=str(data["transport"]),
        output_dir=expand_output_dir(str(data.get("output_dir") or "")),
        session_name=sanitize_session_name(str(data.get("session_name") or "")),
        max_duration_s=None if minutes <= 0 else minutes * 60,
        auto_encode=bool(data["auto_encode"]),
        keep_frames=bool(data["keep_frames"]),
    )


class SubprocessRunner:
    def spawn(self, args: list[str]) -> subprocess.Popen:
        return spawn_ffmpeg(args)


class CaptureService:
    def __init__(
        self,
        ffmpeg: str | None = None,
        preview_dir: Path | None = None,
        runner: SubprocessRunner | None = None,
        max_failures: int = 5,
        retry_delay: float = 2.0,
        stall_grace: float | None = None,
        snapshot_timeout: float = 20.0,
    ):
        self.ffmpeg = find_ffmpeg() if ffmpeg is None else ffmpeg
        self._runner = runner or SubprocessRunner()
        self._max_failures = max_failures
        self._retry_delay = retry_delay
        self._stall_grace = stall_grace
        self._snapshot_timeout = snapshot_timeout
        self._preview_dir = preview_dir or Path(tempfile.gettempdir()) / "prusa-timelapse"
        self._preview_dir.mkdir(parents=True, exist_ok=True)
        self._preview_file = self._preview_dir / "preview.jpg"

        self._lock = threading.Lock()
        self._proc_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._logs: deque[str] = deque(maxlen=300)
        self._info: dict | None = None

        self._phase = "idle"
        self._message = "Idle."
        self._last_error: str | None = None
        self._frames = 0
        self._reconnects = 0
        self._started_mono: float | None = None
        self._elapsed = 0.0
        self._session_dir: Path | None = None
        self._session_name: str | None = None
        self._video_path: Path | None = None
        self._interval_s: float | None = None
        self._video_fps: int | None = None
        self._width: int | None = None
        self._height: int | None = None
        self._preview_rev = 0
        self._output_dir: Path | None = None

        if is_complete_jpeg(self._preview_file):
            self._preview_rev = 1
            size = jpeg_size(self._preview_file)
            if size:
                self._width, self._height = size

    @property
    def preview_file(self) -> Path:
        return self._preview_file

    def ffmpeg_info(self) -> dict:
        if self._info is None:
            self._info = probe_ffmpeg(self.ffmpeg)
        return self._info

    def status(self) -> dict:
        with self._lock:
            elapsed = self._elapsed
            if self._phase != "idle" and self._started_mono is not None:
                elapsed = time.monotonic() - self._started_mono
            fps = self._video_fps or 0
            frames = self._frames
            return {
                "phase": self._phase,
                "message": self._message,
                "last_error": self._last_error,
                "frames": frames,
                "reconnects": self._reconnects,
                "elapsed_s": elapsed,
                "interval_s": self._interval_s,
                "video_fps": self._video_fps,
                "video_seconds": (frames / fps) if fps else 0,
                "session_dir": str(self._session_dir) if self._session_dir else None,
                "session_name": self._session_name,
                "video_path": str(self._video_path) if self._video_path else None,
                "width": self._width,
                "height": self._height,
                "preview_rev": self._preview_rev,
                "output_dir": str(self._output_dir) if self._output_dir else None,
            }

    def log_lines(self) -> list[str]:
        with self._lock:
            return list(self._logs)

    def request_stop(self) -> None:
        """Ask the running job to finish. Safe to call from a signal handler."""
        self._stop.set()
        with self._proc_lock:
            proc = self._proc
        if proc is not None:
            stop_process(proc, wait=False)

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def test_connection(self, url: str, transport: str, quality: int, max_width: int) -> None:
        info = self.ffmpeg_info()
        if not info["available"] or not self.ffmpeg:
            raise ValueError(info.get("hint") or "ffmpeg is not installed.")
        self._begin("testing", "Connecting to the camera.", last_error=None)
        self._start_worker("prusa-timelapse-test", self._run_test, (url, transport, quality, max_width))

    def start(self, config: CaptureConfig) -> None:
        self._require_ffmpeg()
        session_dir = self._reserve_session(config)
        self._begin(
            "capturing",
            "Capturing.",
            frames=0,
            reconnects=0,
            last_error=None,
            elapsed=0.0,
            session_dir=session_dir,
            session_name=session_dir.name,
            video_path=None,
            interval_s=config.interval_s,
            video_fps=config.video_fps,
            output_dir=config.output_dir,
        )
        self._start_worker("prusa-timelapse-capture", self._run_capture, (config, session_dir))

    def encode_session(self, session_dir: Path, fps: int) -> None:
        self._require_ffmpeg()
        if not session_dir.is_dir():
            raise FileNotFoundError("That session folder is gone.")
        self._begin(
            "encoding",
            "Building the video.",
            session_dir=session_dir,
            session_name=session_dir.name,
            video_fps=fps,
            last_error=None,
        )
        self._start_worker(
            "prusa-timelapse-encode",
            self._run_encode,
            (session_dir, fps, False),
        )

    def _start_worker(self, name: str, fn, args: tuple) -> None:
        worker = threading.Thread(
            target=self._guard,
            args=(fn, *args),
            name=name,
            daemon=False,
        )
        self._thread = worker
        worker.start()

    def _require_ffmpeg(self) -> None:
        info = self.ffmpeg_info()
        if not info["available"] or not self.ffmpeg:
            hint = info.get("hint") or "ffmpeg is not installed."
            raise ValueError(hint)
        if not info["libx264"]:
            raise ValueError(
                "This ffmpeg has no libx264 encoder, so it cannot build the video. "
                "Install a full ffmpeg build and try again."
            )

    def _begin(self, phase: str, message: str, **fields) -> None:
        with self._lock:
            if self._phase != "idle":
                raise BusyError("A capture or encode is already running.")
            alive = self._thread is not None and self._thread.is_alive()
            if alive:
                raise BusyError("A capture or encode is already running.")
            self._phase = phase
            self._message = message
            self._stop.clear()
            self._started_mono = time.monotonic()
            for key, value in fields.items():
                setattr(self, f"_{key}", value)

    def _guard(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as exc:
            self._log(f"error: {exc}")
            self._finish(message="Stopped because of an error.", last_error=str(exc))

    def _finish(self, message: str, last_error: str | None) -> None:
        with self._lock:
            if self._started_mono is not None:
                self._elapsed = time.monotonic() - self._started_mono
            self._phase = "idle"
            self._message = message
            self._last_error = last_error
            self._started_mono = None

    def _update(self, **fields) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self, f"_{key}", value)

    def _log(self, line: str) -> None:
        text = redact_urls_in_text(line).strip()
        if not text:
            return
        with self._lock:
            self._logs.append(text)

    def _last_log(self) -> str:
        with self._lock:
            return self._logs[-1] if self._logs else ""

    def _set_proc(self, proc: subprocess.Popen | None) -> None:
        with self._proc_lock:
            self._proc = proc

    def _spawn(self, args: list[str]) -> subprocess.Popen:
        try:
            return self._runner.spawn(args)
        except OSError as exc:
            raise RuntimeError(f"Could not start ffmpeg: {exc}") from exc

    def _run_test(self, url: str, transport: str, quality: int, max_width: int) -> None:
        destination = self._preview_dir / "snapshot.jpg"
        if destination.exists():
            destination.unlink()
        args = build_snapshot_args(
            self.ffmpeg or "ffmpeg",
            url,
            quality,
            max_width,
            transport,
            destination,
        )
        self._log(f"Testing {redact_url(url)}")
        proc = self._spawn(args)
        self._set_proc(proc)
        pump = threading.Thread(target=pump_stderr, args=(proc, self._log), daemon=True)
        pump.start()
        deadline = time.monotonic() + self._snapshot_timeout
        try:
            while proc.poll() is None:
                if self._stop.is_set() or time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
        finally:
            self._reap(proc, pump)
        if self._stop.is_set() and not is_complete_jpeg(destination):
            self._finish(message="Connection test cancelled.", last_error=None)
            return
        if is_complete_jpeg(destination):
            self._publish(destination)
            self._finish(message="Connection looks good.", last_error=None)
            return
        detail = self._last_log()
        reason = "No frame arrived from the camera."
        if detail and detail != f"Testing {redact_url(url)}":
            reason = f"{reason} {detail}"
        self._finish(message="Could not grab a frame.", last_error=reason)

    def _run_capture(self, config: CaptureConfig, session_dir: Path) -> None:
        frames_dir = session_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        failures = 0
        reconnects = 0
        started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        meta_at = 0.0
        duration_hit = False
        stream_error: Exception | None = None
        self._write_meta(config, session_dir, started_at, count, final=False)
        try:
            while not self._stop.is_set():
                if self._duration_reached(config):
                    duration_hit = True
                    self._stop.set()
                    break
                before = count
                count = self._capture_once(config, session_dir, frames_dir, count)
                self._discard_tail(frames_dir, count)
                now = time.monotonic()
                if now - meta_at >= 1:
                    self._write_meta(config, session_dir, started_at, count, final=False)
                    meta_at = now
                if self._duration_reached(config):
                    duration_hit = True
                    self._stop.set()
                if self._stop.is_set():
                    break
                detail = self._last_log()
                if count == before:
                    failures += 1
                    reconnects += 1
                    self._update(reconnects=reconnects, message="Reconnecting to the camera.")
                    self._log("The stream dropped before a new frame arrived.")
                    if failures >= self._max_failures:
                        reason = (
                            "ffmpeg could not keep frames coming from the stream. "
                            "Check the RTSP URL, that this computer and the camera are on "
                            "the same network, and that no other viewer is using the camera."
                        )
                        if detail and not detail.startswith("Opening "):
                            reason = f"{reason} Last ffmpeg message: {detail}"
                        raise RuntimeError(reason)
                else:
                    failures = 0
                    reconnects += 1
                    self._update(reconnects=reconnects, message="Reconnecting to the camera.")
                    self._log("Stream dropped. Continuing the frame sequence.")
                if self._stop.wait(self._retry_delay):
                    break
        except Exception as exc:
            stream_error = exc

        # A failure inside one connection can return before the caller's count
        # catches the frames already written. Keep the larger number.
        count = max(count, self.status()["frames"])
        self._update(frames=count)
        video_path = None
        encode_error = None
        if config.auto_encode and count > 0:
            self._update(phase="encoding", message=f"Building the video from {count} frames.")
            try:
                video_path = self._encode(config.video_fps, session_dir, frames_dir)
                if not config.keep_frames:
                    shutil.rmtree(frames_dir, ignore_errors=True)
            except Exception as exc:
                encode_error = str(exc)
                self._log(f"encode failed: {exc}")
        self._update(video_path=video_path, frames=count)
        try:
            self._write_meta(
                config,
                session_dir,
                started_at,
                count,
                final=True,
                video_ready=video_path is not None,
            )
        except OSError:
            pass
        if encode_error:
            self._finish(
                message="Frames are saved, but the video was not built.",
                last_error=encode_error,
            )
            return
        if stream_error and video_path:
            self._finish(
                message="Stream ended early. The video includes the frames that were saved.",
                last_error=str(stream_error),
            )
            return
        if stream_error:
            self._finish(
                message="Capture failed. Saved frames are still in the session folder.",
                last_error=str(stream_error),
            )
            return
        if duration_hit and video_path:
            message = "Reached the time limit. The video is ready."
        elif duration_hit:
            message = "Reached the time limit."
        elif video_path:
            message = "Video is ready."
        elif count == 0:
            message = "Capture stopped before any frames arrived."
        else:
            message = f"Capture stopped. {count} frames saved."
        self._finish(message=message, last_error=None)

    def _capture_once(
        self,
        config: CaptureConfig,
        session_dir: Path,
        frames_dir: Path,
        count: int,
    ) -> int:
        pattern = frames_dir / "frame_%06d.jpg"
        args = build_capture_args(
            self.ffmpeg or "ffmpeg",
            config.url,
            config.interval_s,
            config.quality,
            config.max_width,
            config.transport,
            count + 1,
            pattern,
        )
        self._log(f"Opening {redact_url(config.url)}")
        proc = self._spawn(args)
        self._set_proc(proc)
        pump = threading.Thread(target=pump_stderr, args=(proc, self._log), daemon=True)
        pump.start()
        last_new = time.monotonic()
        try:
            while proc.poll() is None:
                if self._stop.is_set():
                    break
                if self._duration_reached(config):
                    self._stop.set()
                    break
                gained = self._drain(frames_dir, session_dir, count)
                if gained:
                    count += gained
                    last_new = time.monotonic()
                    self._update(frames=count, message="Capturing.")
                elif time.monotonic() - last_new > self._stall_after(config.interval_s):
                    self._log("No new frame arrived. Reconnecting.")
                    break
                time.sleep(0.25)
        finally:
            self._reap(proc, pump)
            count += self._drain(frames_dir, session_dir, count)
            self._update(frames=count)
        return count

    def _reap(self, proc: subprocess.Popen, pump: threading.Thread) -> None:
        stop_process(proc, wait=True)
        self._set_proc(None)
        pump.join(timeout=2)
        if proc.stderr is not None:
            proc.stderr.close()

    def _drain(self, frames_dir: Path, session_dir: Path, count: int) -> int:
        gained = 0
        number = count + 1
        while True:
            path = frames_dir / f"frame_{number:06d}.jpg"
            if not path.is_file() or not is_complete_jpeg(path):
                return gained
            self._publish(path, session_dir / "latest.jpg")
            gained += 1
            number += 1

    def _discard_tail(self, frames_dir: Path, count: int) -> None:
        tail = frames_dir / f"frame_{count + 1:06d}.jpg"
        if tail.is_file() and not is_complete_jpeg(tail):
            try:
                tail.unlink()
            except OSError:
                return

    def _stall_after(self, interval_s: float) -> float:
        if self._stall_grace is not None:
            return self._stall_grace
        return max(interval_s * 2 + 15, 25)

    def _duration_reached(self, config: CaptureConfig) -> bool:
        if config.max_duration_s is None or self._started_mono is None:
            return False
        with self._lock:
            started = self._started_mono
        if started is None:
            return False
        return time.monotonic() - started >= config.max_duration_s

    def _publish(self, source: Path, latest: Path | None = None) -> None:
        try:
            _atomic_copy(source, self._preview_file)
            if latest is not None:
                _atomic_copy(source, latest)
        except OSError as exc:
            self._log(f"Could not update the preview: {exc}")
            return
        size = jpeg_size(source)
        with self._lock:
            self._preview_rev += 1
            if size:
                self._width, self._height = size

    def _run_encode(self, session_dir: Path, fps: int, delete_frames: bool) -> None:
        frames_dir = session_dir / "frames"
        count = contiguous_frame_count(frames_dir)
        if count <= 0:
            raise RuntimeError("That session has no frames to encode.")
        self._update(frames=count, message=f"Building the video from {count} frames.")
        video = self._encode(fps, session_dir, frames_dir)
        if delete_frames:
            shutil.rmtree(frames_dir, ignore_errors=True)
        self._update(video_path=video, video_fps=fps)
        self._touch_meta_video(session_dir, fps, count)
        self._finish(message="Video is ready.", last_error=None)

    def _encode(self, fps: int, session_dir: Path, frames_dir: Path) -> Path:
        if not self.ffmpeg:
            raise RuntimeError("ffmpeg is not installed.")
        output = session_dir / "timelapse.mp4"
        args = build_encode_args(self.ffmpeg, frames_dir, output, fps)
        self._log(f"Encoding {output.name} at {fps} fps.")
        completed = run_ffmpeg(args)
        if completed.stderr:
            for line in completed.stderr.splitlines():
                self._log(line)
        if completed.returncode != 0:
            tail = [line.strip() for line in (completed.stderr or "").splitlines() if line.strip()]
            detail = " ".join(tail[-4:]) or f"ffmpeg exited with status {completed.returncode}."
            raise RuntimeError(f"ffmpeg could not build the video. {detail}")
        if not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError("ffmpeg did not write a video.")
        return output

    def _write_meta(
        self,
        config: CaptureConfig,
        session_dir: Path,
        started_at: str,
        frames: int,
        final: bool,
        video_ready: bool = False,
    ) -> None:
        payload = {
            "name": session_dir.name,
            "source": redact_url(config.url),
            "interval_s": config.interval_s,
            "video_fps": config.video_fps,
            "quality": config.quality,
            "max_width": config.max_width,
            "transport": config.transport,
            "started_at": started_at,
            "ended_at": datetime.now().astimezone().isoformat(timespec="seconds") if final else None,
            "frames": frames,
            "video": "timelapse.mp4" if video_ready else None,
        }
        _write_json(session_dir / "meta.json", payload)

    def _touch_meta_video(self, session_dir: Path, fps: int, frames: int) -> None:
        path = session_dir / "meta.json"
        payload: dict = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    payload = loaded
            except (OSError, json.JSONDecodeError):
                payload = {}
        payload["video"] = "timelapse.mp4"
        payload["video_fps"] = fps
        payload["frames"] = frames
        _write_json(path, payload)

    def _reserve_session(self, config: CaptureConfig) -> Path:
        try:
            config.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"Could not create the output folder: {exc}") from exc
        return allocate_session_dir(config.output_dir, config.session_name)


def allocate_session_dir(root: Path, name: str) -> Path:
    candidate = root / name
    if not candidate.exists():
        return candidate
    for suffix in range(2, 1000):
        alternative = root / f"{name}-{suffix}"
        if not alternative.exists():
            return alternative
    raise RuntimeError("Could not find a free session folder name.")


def contiguous_frame_count(frames_dir: Path) -> int:
    count = 0
    if not frames_dir.is_dir():
        return 0
    while True:
        path = frames_dir / f"frame_{count + 1:06d}.jpg"
        if not path.is_file() or not is_complete_jpeg(path):
            return count
        count += 1


def list_sessions(root: Path) -> list[dict]:
    # Imported here so library.py can import resolve_session without a cycle.
    from prusa_timelapse.library import SESSION_LIST_LIMIT, summarize_frames

    if not root.is_dir():
        return []
    rows = []
    for child in root.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        meta = _read_meta(child / "meta.json")
        summary = summarize_frames(child / "frames")
        on_disk = summary["contiguous"]
        stored = meta.get("frames")
        if not isinstance(stored, int):
            stored = on_disk
        shown = on_disk or stored
        video = child / "timelapse.mp4"
        try:
            video_bytes = video.stat().st_size if video.is_file() else 0
        except OSError:
            video_bytes = 0
        has_video = video_bytes > 0
        if shown <= 0 and not has_video and not meta:
            continue
        try:
            modified = child.stat().st_mtime
        except OSError:
            modified = 0
        rows.append({
            "name": child.name,
            "frames": shown,
            "frames_on_disk": on_disk,
            "frame_files": summary["count"],
            "frames_bytes": summary["bytes"],
            "has_video": has_video,
            "video_bytes": video_bytes,
            "video_fps": meta.get("video_fps"),
            "interval_s": meta.get("interval_s"),
            "started_at": meta.get("started_at"),
            "mtime": modified,
        })
    rows.sort(key=lambda row: row["mtime"], reverse=True)
    return rows[:SESSION_LIST_LIMIT]


def resolve_session(root: Path, name: str) -> Path:
    if not name or name != Path(name).name or name.startswith("."):
        raise ValueError("Unknown session.")
    base = root.resolve()
    candidate = (base / name).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError("Unknown session.") from exc
    if not candidate.is_dir():
        raise FileNotFoundError("That session folder is gone.")
    return candidate


def _read_meta(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    shutil.copyfile(source, temporary)
    os.replace(temporary, destination)
