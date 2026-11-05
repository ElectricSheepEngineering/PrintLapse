"""Local web UI. The desktop app binds to localhost and has no password."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from printlapse.capture import (
    BusyError,
    config_from_dict,
    list_sessions,
    resolve_session,
)
from printlapse.ffmpeg_util import validate_rtsp_url
from printlapse.library import (
    contained_file,
    delete_stored,
    download_filename,
    frame_file,
    parse_byte_range,
    session_detail,
    video_file,
    write_zip,
    zip_members,
)
from printlapse.settings import (
    data_root,
    default_output_dir,
    expand_output_dir,
    normalize_settings,
)


_WEB = Path(__file__).resolve().parent / "web"
_MAX_BODY = 64 * 1024
_SESSION_PATH = re.compile(r"^/api/sessions/([^/]+)(?:/(.*))?$")
_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


class ChunkedWriter:
    """HTTP chunked body that looks like an unseekable file to zipfile.

    tell() counts zip bytes, not the chunk framing, so archive offsets stay
    correct when the client reassembles the body.
    """

    def __init__(self, wfile):
        self._wfile = wfile
        self._written = 0

    def write(self, data) -> int:
        if not data:
            return 0
        payload = bytes(data)
        self._wfile.write(f"{len(payload):x}\r\n".encode("ascii"))
        self._wfile.write(payload)
        self._wfile.write(b"\r\n")
        self._written += len(payload)
        return len(payload)

    def tell(self) -> int:
        return self._written

    def flush(self) -> None:
        self._wfile.flush()

    def seekable(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def finish(self) -> None:
        self._wfile.write(b"0\r\n\r\n")
        self._wfile.flush()


class AppServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def load_html() -> bytes:
    return (_WEB / "index.html").read_bytes()


def load_favicon() -> bytes:
    return (_WEB / "favicon.svg").read_bytes()


def open_in_file_manager(path: Path) -> None:
    if sys.platform == "darwin":
        command = ["open", str(path)]
    elif sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    else:
        command = ["xdg-open", str(path)]
    subprocess.run(command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class App:
    def __init__(self, service, store, opener=None):
        self.service = service
        self.store = store
        self.opener = opener or open_in_file_manager
        self.html = load_html()
        self.favicon = load_favicon()

    def output_root(self) -> Path:
        settings = self.store.load()
        return expand_output_dir(str(settings.get("output_dir") or ""))


def make_server(app: App, host: str, port: int) -> AppServer:
    handler = handler_class(app)
    return AppServer((host, port), handler)


def handler_class(app: App):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:
            return

        def do_GET(self) -> None:
            self._guard(self._get)

        def do_POST(self) -> None:
            self._guard(self._post)

        def do_PUT(self) -> None:
            self._guard(self._put)

        def do_DELETE(self) -> None:
            self._guard(self._delete)

        def _guard(self, method) -> None:
            try:
                method()
            except BusyError as exc:
                self._send_json(409, {"error": str(exc)})
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except FileNotFoundError as exc:
                self._send_json(404, {"error": str(exc)})
            except Exception as exc:
                if getattr(self, "_started", False):
                    if not isinstance(exc, _GONE):
                        traceback.print_exc()
                    return
                traceback.print_exc()
                self._send_json(500, {"error": str(exc)})

        def _get(self) -> None:
            path = urlparse(self.path).path
            if path in {"/", "/index.html"}:
                self._send_bytes(200, app.html, "text/html; charset=utf-8")
                return
            if path == "/favicon.svg":
                self._send_bytes(200, app.favicon, "image/svg+xml")
                return
            if path == "/favicon.ico":
                self._send_bytes(204, b"", "image/x-icon")
                return
            if path == "/preview.jpg":
                preview = app.service.preview_file
                if not preview.is_file():
                    self._send_json(404, {"error": "No frame yet."})
                    return
                self._send_bytes(200, preview.read_bytes(), "image/jpeg")
                return
            if path == "/api/bootstrap":
                self._send_json(200, self._bootstrap())
                return
            if path == "/api/status":
                self._send_json(200, app.service.status())
                return
            if path == "/api/log":
                self._send_json(200, {"lines": app.service.log_lines()})
                return
            if path == "/api/sessions":
                self._send_json(200, {"sessions": list_sessions(app.output_root())})
                return
            route = self._match_session()
            if route is not None:
                self._get_session(*route)
                return
            self._send_json(404, {"error": "Not found."})

        def _post(self) -> None:
            path = urlparse(self.path).path
            body = self._read_json()
            if path == "/api/test":
                settings = self._save_settings(body)
                url = validate_rtsp_url(settings["url"])
                app.service.test_connection(
                    url,
                    settings["transport"],
                    settings["quality"],
                    settings["max_width"],
                )
                self._send_json(200, app.service.status())
                return
            if path == "/api/start":
                settings = self._save_settings(body)
                app.service.start(config_from_dict(settings))
                self._send_json(200, app.service.status())
                return
            if path == "/api/stop":
                app.service.request_stop()
                self._send_json(200, app.service.status())
                return
            if path == "/api/encode":
                session = resolve_session(app.output_root(), str(body.get("session") or ""))
                fps = self._encode_fps(body, session)
                app.service.encode_session(session, fps)
                self._send_json(200, app.service.status())
                return
            if path == "/api/open":
                self._open(body)
                self._send_json(200, {"ok": True})
                return
            self._send_json(404, {"error": "Not found."})

        def _delete(self) -> None:
            route = self._match_session()
            if route is None:
                self._send_json(404, {"error": "Not found."})
                return
            name, rest, parsed = route
            if rest:
                self._send_json(404, {"error": "Not found."})
                return
            target = (parse_qs(parsed.query).get("target") or [""])[0]
            self._require_idle_session(name)
            deleted = delete_stored(app.output_root(), name, target)
            self._send_json(200, {"ok": True, "deleted": deleted})

        def _put(self) -> None:
            path = urlparse(self.path).path
            if path != "/api/settings":
                self._send_json(404, {"error": "Not found."})
                return
            settings = self._save_settings(self._read_json())
            self._send_json(200, settings)

        def _save_settings(self, body: dict) -> dict:
            current = app.store.load()
            if body:
                current.update(body)
            return app.store.save(current)

        def _encode_fps(self, body: dict, session: Path) -> int:
            if body.get("fps") not in (None, ""):
                return normalize_settings({"video_fps": body["fps"]})["video_fps"]
            meta_path = session / "meta.json"
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    meta = {}
                if isinstance(meta, dict) and meta.get("video_fps"):
                    return normalize_settings({"video_fps": meta["video_fps"]})["video_fps"]
            return int(app.store.load()["video_fps"])

        def _open(self, body: dict) -> None:
            session = resolve_session(app.output_root(), str(body.get("session") or ""))
            target = str(body.get("target") or "folder")
            if target == "video":
                path = session / "timelapse.mp4"
                if not path.is_file():
                    raise FileNotFoundError("That session has no video yet.")
            elif target == "folder":
                path = session
            else:
                raise ValueError("Open target must be folder or video.")
            app.opener(path)

        def _bootstrap(self) -> dict:
            root = app.output_root()
            data = data_root()
            return {
                "ffmpeg": app.service.ffmpeg_info(),
                "settings": app.store.load(),
                "default_output_dir": str(default_output_dir()),
                "settings_path": str(app.store.path),
                "container": data is not None,
                "data_dir": str(data) if data else "",
                "status": app.service.status(),
                "sessions": list_sessions(root),
            }

        def _match_session(self):
            parsed = urlparse(self.path)
            match = _SESSION_PATH.match(parsed.path)
            if match is None:
                return None
            name = unquote(match.group(1))
            rest = unquote(match.group(2) or "")
            return name, rest, parsed

        def _require_idle_session(self, name: str) -> None:
            status = app.service.status()
            phase = status.get("phase")
            if phase in {"capturing", "encoding"} and status.get("session_name") == name:
                raise BusyError(
                    "That session is in use. Stop the capture before changing its files."
                )

        def _video_is_encoding(self, name: str) -> bool:
            status = app.service.status()
            return status.get("phase") == "encoding" and status.get("session_name") == name

        def _get_session(self, name: str, rest: str, parsed) -> None:
            session = resolve_session(app.output_root(), name)
            if rest == "":
                offset, limit = self._page_args(parsed)
                self._send_json(200, session_detail(session, offset, limit))
                return
            if rest == "video":
                if self._video_is_encoding(name):
                    raise BusyError("That video is still being built.")
                download = self._wants_download(parsed)
                self._send_file(
                    video_file(session),
                    "video/mp4",
                    download_filename(session.name, "timelapse.mp4"),
                    download,
                )
                return
            if rest == "frames.zip":
                self._send_zip(session, "frames", download_filename(session.name, "frames.zip"), True)
                return
            if rest == "archive.zip":
                self._send_zip(
                    session,
                    "archive",
                    download_filename(session.name, "timelapse.zip"),
                    include_video=not self._video_is_encoding(name),
                )
                return
            if rest == "latest.jpg":
                self._send_file(contained_file(session, "latest.jpg"), "image/jpeg", "latest.jpg", False)
                return
            if rest.startswith("frames/"):
                self._send_file(
                    frame_file(session, rest[len("frames/"):]),
                    "image/jpeg",
                    rest[len("frames/"):],
                    False,
                )
                return
            self._send_json(404, {"error": "Not found."})

        def _page_args(self, parsed) -> tuple[int, int]:
            query = parse_qs(parsed.query)
            try:
                offset = int((query.get("offset") or ["0"])[0] or 0)
                limit = int((query.get("limit") or ["24"])[0] or 24)
            except ValueError as exc:
                raise ValueError("Page offset and limit must be whole numbers.") from exc
            if offset < 0 or limit < 1 or limit > 60:
                raise ValueError("Page offset and limit are out of range.")
            return offset, limit

        def _wants_download(self, parsed) -> bool:
            value = (parse_qs(parsed.query).get("download") or [""])[0].lower()
            return value not in {"", "0", "false"}

        def _send_zip(self, session: Path, kind: str, download_name: str, include_video: bool) -> None:
            members = zip_members(session, kind, include_video=include_video)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self._started = True
            writer = ChunkedWriter(self.wfile)
            try:
                write_zip(members, writer)
            except _GONE:
                return
            except Exception:
                traceback.print_exc()
            try:
                writer.finish()
            except _GONE:
                return

        def _send_file(self, path: Path, content_type: str, download_name: str, attachment: bool) -> None:
            try:
                size = path.stat().st_size
            except OSError as exc:
                raise FileNotFoundError("That file is not in this session.") from exc
            try:
                span = parse_byte_range(self.headers.get("Range"), size)
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self._started = True
                return
            if span is None:
                start = 0
                length = size
                status = 200
            else:
                start, end = span
                length = end - start + 1
                status = 206
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if status == 206 and span is not None:
                self.send_header("Content-Range", f"bytes {span[0]}-{span[1]}/{size}")
            if download_name:
                kind = "attachment" if attachment else "inline"
                self.send_header("Content-Disposition", f'{kind}; filename="{download_name}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self._started = True
            if self.command == "HEAD" or length <= 0:
                return
            try:
                with path.open("rb") as handle:
                    handle.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = handle.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except _GONE:
                return

        def _read_json(self) -> dict:
            length_header = self.headers.get("Content-Length", "0")
            try:
                length = int(length_header)
            except ValueError as exc:
                raise ValueError("Content-Length must be a number.") from exc
            if length < 0 or length > _MAX_BODY:
                raise ValueError("Request is too large.")
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError("Request body must be JSON.") from exc
            if not isinstance(data, dict):
                raise ValueError("Request body must be a JSON object.")
            return data

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self._send_bytes(status, body, "application/json; charset=utf-8")

        def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self._started = True
            if body and self.command != "HEAD":
                try:
                    self.wfile.write(body)
                except _GONE:
                    return

    return Handler
