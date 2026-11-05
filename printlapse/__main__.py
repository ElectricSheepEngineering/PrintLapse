"""Command line entry for the local app and for headless capture."""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path

from printlapse import __version__
from printlapse.capture import CaptureService, config_from_dict
from printlapse.ffmpeg_util import validate_rtsp_url
from printlapse.server import App, make_server
from printlapse.settings import SettingsStore, app_config_dir, normalize_settings


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["gui"]
    parser = argparse.ArgumentParser(
        prog="printlapse",
        description="Grab frames from an RTSP camera on a timer and build a timelapse video.",
    )
    parser.add_argument("--version", action="version", version=f"printlapse {__version__}")
    commands = parser.add_subparsers(dest="command")

    gui = commands.add_parser("gui", help="Open the local control page in a browser.")
    gui.add_argument("--host", default="127.0.0.1")
    gui.add_argument("--port", type=int, default=8765)
    gui.add_argument("--no-browser", action="store_true")

    test = commands.add_parser("test", help="Grab one frame to check an RTSP URL.")
    test.add_argument("--url", required=True)
    test.add_argument("--transport", choices=("tcp", "udp"), default="tcp")
    test.add_argument("--quality", type=int, default=3)
    test.add_argument("--width", type=int, default=0)
    test.add_argument("--timeout", type=float, default=20)

    capture = commands.add_parser("capture", help="Record frames until stopped, then build a video.")
    capture.add_argument("--url", required=True)
    capture.add_argument("--every", type=float, required=True, help="Seconds between frames.")
    capture.add_argument("--fps", type=int, default=30, help="Playback frame rate of the video.")
    capture.add_argument("--quality", type=int, default=3, help="JPEG quality, 2 (best) through 31.")
    capture.add_argument("--width", type=int, default=0, help="Frame width in pixels. 0 keeps the camera size.")
    capture.add_argument("--output", default="", help="Folder for session directories.")
    capture.add_argument("--name", default="", help="Session folder name. Blank uses the time.")
    capture.add_argument("--minutes", type=float, default=0, help="Stop after this many minutes. 0 runs until stopped.")
    capture.add_argument("--transport", choices=("tcp", "udp"), default="tcp")
    capture.add_argument("--no-encode", action="store_true", help="Keep the frames and skip the video.")
    capture.add_argument("--delete-frames", action="store_true", help="Delete JPEGs after a successful encode.")

    encode = commands.add_parser("encode", help="Build a video from a session folder that already has frames.")
    encode.add_argument("session", help="Path to a session folder.")
    encode.add_argument("--fps", type=int, default=0, help="Playback frame rate. 0 uses the rate saved with the session.")

    args = parser.parse_args(argv)
    if args.command == "gui":
        return gui_main(args)
    if args.command == "test":
        return test_main(args)
    if args.command == "capture":
        return capture_main(args)
    if args.command == "encode":
        return encode_main(args)
    parser.print_help()
    return 2


def gui_main(args) -> int:
    if not 1 <= args.port <= 65535:
        print("Port must be between 1 and 65535.", file=sys.stderr)
        return 2
    directory = app_config_dir()
    service = CaptureService(preview_dir=directory)
    store = SettingsStore(directory / "settings.json")
    app = App(service, store)
    httpd = None
    last_error = None
    for port in range(args.port, min(args.port + 10, 65536)):
        try:
            httpd = make_server(app, args.host, port)
            break
        except OSError as exc:
            last_error = exc
    if httpd is None:
        print(f"No free port near {args.port}: {last_error}", file=sys.stderr)
        return 1

    host = args.host
    bound = httpd.server_address[1]
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{display_host}:{bound}/"
    if host in {"0.0.0.0", "::"}:
        print(f"PrintLapse is listening on port {bound}.")
        print(f"On this machine: http://127.0.0.1:{bound}/")
    else:
        print(f"PrintLapse is at {url}")
    print("Leave this process running while a capture is going. Closing it stops the capture.")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print("This server has no password. Anyone who can open that address can start a capture.")
    if not args.no_browser:
        webbrowser.open(url)

    state = {"hits": 0}

    def handle_signal(signum, frame) -> None:
        state["hits"] += 1
        if state["hits"] >= 2:
            service.request_stop()
            raise SystemExit(130)
        print("\nStopping. Press Ctrl+C again to quit immediately.", flush=True)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    try:
        httpd.serve_forever()
    finally:
        if service.status()["phase"] != "idle":
            print("Finishing the capture. A video is built when frames were saved.")
        service.request_stop()
        service.join(timeout=180)
        httpd.server_close()
    return 0


def test_main(args) -> int:
    service = CaptureService(snapshot_timeout=args.timeout)
    try:
        service.test_connection(
            validate_rtsp_url(args.url),
            args.transport,
            args.quality,
            args.width,
        )
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    _wait_until_idle(service)
    status = service.status()
    if status["last_error"]:
        print(status["last_error"], file=sys.stderr)
        return 1
    print(status["message"])
    if status["width"] and status["height"]:
        print(f"{status['width']}x{status['height']}")
    if status["session_dir"] is None and service.preview_file.is_file():
        print(service.preview_file)
    return 0


def capture_main(args) -> int:
    try:
        settings = normalize_settings({
            "url": args.url,
            "interval_s": args.every,
            "video_fps": args.fps,
            "quality": args.quality,
            "max_width": args.width,
            "output_dir": args.output,
            "session_name": args.name,
            "max_duration_min": args.minutes,
            "transport": args.transport,
            "auto_encode": not args.no_encode,
            "keep_frames": not args.delete_frames,
        })
        config = config_from_dict(settings)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    service = CaptureService()
    hits = {"n": 0}

    def handle_signal(signum, frame) -> None:
        hits["n"] += 1
        service.request_stop()
        if hits["n"] >= 2:
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    try:
        service.start(config)
    except (ValueError, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"Saving frames to {config.output_dir}")
    print("Press Ctrl+C to stop and build the video.")
    try:
        _wait_until_idle(service, echo=True)
    except KeyboardInterrupt:
        service.request_stop()
        service.join(timeout=5)
        return 130
    return _finish_cli(service)


def encode_main(args) -> int:
    session = Path(args.session).expanduser()
    if not session.is_absolute():
        session = Path.cwd() / session
    session = session.resolve()
    fps = args.fps
    if fps <= 0:
        meta = session / "meta.json"
        fps = 30
        if meta.is_file():
            import json
            try:
                loaded = json.loads(meta.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and loaded.get("video_fps"):
                    fps = int(loaded["video_fps"])
            except (OSError, ValueError, json.JSONDecodeError):
                fps = 30
    service = CaptureService()
    try:
        service.encode_session(session, fps)
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        print(exc, file=sys.stderr)
        return 2
    _wait_until_idle(service, echo=True)
    return _finish_cli(service)


def _wait_until_idle(service: CaptureService, echo: bool = False) -> None:
    last_key = None
    while True:
        status = service.status()
        key = (status["phase"], status["frames"], status["message"])
        if echo and key != last_key:
            print(_format_status(status), flush=True)
            last_key = key
        thread = service._thread
        idle = status["phase"] == "idle" and (thread is None or not thread.is_alive())
        if idle:
            service.join(timeout=2)
            return
        time.sleep(0.3)


def _finish_cli(service: CaptureService) -> int:
    status = service.status()
    print(_format_status(status))
    if status["session_dir"]:
        print(status["session_dir"])
    if status["video_path"]:
        print(status["video_path"])
    if status["last_error"]:
        print(status["last_error"], file=sys.stderr)
        if not status["video_path"]:
            return 1
    return 0


def _format_status(status: dict) -> str:
    clock = _format_clock(status.get("elapsed_s") or 0)
    video = status.get("video_seconds") or 0
    return (
        f"{status['phase']:10}  {status['frames']:5} frames  "
        f"{clock}  video {_format_clock(video)}  {status['message']}"
    )


def _format_clock(seconds: float) -> str:
    whole = max(0, int(seconds))
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


if __name__ == "__main__":
    sys.exit(main())
