"""HTTP API for the local control page."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from printlapse.__main__ import main
from printlapse.capture import CaptureService
from printlapse.server import App, make_server
from printlapse.settings import SettingsStore
from tests.fake_ffmpeg import make_jpeg
from tests.support import Env, ensure_fake, wait_until


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        fake = ensure_fake()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.output = root / "out"
        self.opened: list[Path] = []
        self.service = CaptureService(
            ffmpeg=fake,
            preview_dir=root / "preview",
            max_failures=3,
            retry_delay=0.05,
            snapshot_timeout=3,
        )
        self.store = SettingsStore(root / "settings.json")
        self.app = App(self.service, self.store, opener=self.opened.append)
        self.httpd = make_server(self.app, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.env = Env(FAKE_FFMPEG_MODE="stream", FAKE_FFMPEG_DELAY="0.05")
        self.env.__enter__()

    def tearDown(self) -> None:
        self.service.request_stop()
        self.service.join(timeout=8)
        self.httpd.shutdown()
        self.httpd.server_close()
        self.env.__exit__(None, None, None)
        self.tmp.cleanup()

    def request(self, method: str, path: str, body: dict | None = None, raw: bool = False, headers=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if headers:
            for key, value in headers.items():
                req.add_header(key, value)
        try:
            with urllib.request.urlopen(req) as response:
                self.last_headers = response.headers
                payload = response.read()
                if raw:
                    return response.status, payload
                return response.status, json.loads(payload.decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            self.last_headers = exc.headers
            payload = exc.read()
            if raw:
                return exc.code, payload
            return exc.code, json.loads(payload.decode("utf-8"))

    def _settled(self) -> bool:
        status = self.service.status()
        thread = self.service._thread
        alive = thread is not None and thread.is_alive()
        return status["phase"] == "idle" and not alive

    def _job(self, **overrides):
        job = {
            "url": "rtsp://cam:secret@127.0.0.1/live",
            "interval_s": 10,
            "video_fps": 30,
            "output_dir": str(self.output),
            "session_name": "api-print",
            "auto_encode": True,
            "keep_frames": True,
        }
        job.update(overrides)
        return job

    def test_page_mentions_the_buddy_cam_url(self) -> None:
        status, body = self.request("GET", "/", raw=True)
        text = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("Buddy3D", text)
        self.assertIn("/live", text)
        self.assertIn("PrintLapse", text)
        self.assertIn("Saved files", text)
        self.assertIn("Frames + video", text)

    def test_settings_round_trip(self) -> None:
        status, saved = self.request("PUT", "/api/settings", self._job(interval_s=8, video_fps=12))
        self.assertEqual(status, 200)
        self.assertEqual(saved["interval_s"], 8)
        self.assertEqual(saved["video_fps"], 12)
        code, boot = self.request("GET", "/api/bootstrap")
        self.assertEqual(code, 200)
        self.assertTrue(boot["ffmpeg"]["available"])
        self.assertEqual(boot["settings"]["video_fps"], 12)

    def test_bad_url_is_rejected(self) -> None:
        status, payload = self.request("POST", "/api/start", self._job(url="http://printer/video"))
        self.assertEqual(status, 400)
        self.assertIn("rtsp://", payload["error"])

    def test_capture_stop_open_and_rebuild(self) -> None:
        status, payload = self.request("POST", "/api/start", self._job())
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["phase"], "capturing")
        wait_until(lambda: self.service.status()["frames"] >= 2, detail=self.service.status)
        again, busy = self.request("POST", "/api/start", self._job())
        self.assertEqual(again, 409)
        self.assertIn("already running", busy["error"])
        code, _ = self.request("POST", "/api/stop", {})
        self.assertEqual(code, 200)
        wait_until(self._settled, detail=self.service.status)
        done = self.service.status()
        self.assertTrue(done["video_path"])
        self.assertIsNone(done["last_error"], done)
        code, log = self.request("GET", "/api/log")
        self.assertEqual(code, 200)
        self.assertNotIn("secret", "\n".join(log["lines"]))
        code, listing = self.request("GET", "/api/sessions")
        self.assertEqual(code, 200)
        self.assertEqual(listing["sessions"][0]["name"], "api-print")
        code, opened = self.request("POST", "/api/open", {"session": "api-print", "target": "folder"})
        self.assertEqual(code, 200, opened)
        self.assertEqual(self.opened[-1].name, "api-print")
        code, _ = self.request("POST", "/api/open", {"session": "api-print", "target": "video"})
        self.assertEqual(code, 200)
        self.assertTrue(self.opened[-1].name.endswith(".mp4"))
        code, preview = self.request("GET", "/preview.jpg", raw=True)
        self.assertEqual(code, 200)
        self.assertTrue(preview.startswith(b"\xff\xd8"))
        code, encoded = self.request("POST", "/api/encode", {"session": "api-print", "fps": 12})
        self.assertEqual(code, 200, encoded)
        wait_until(self._settled, detail=self.service.status)
        self.assertEqual(self.service.status()["video_fps"], 12)
        code, escaped = self.request("POST", "/api/open", {"session": "../secret", "target": "folder"})
        self.assertEqual(code, 400)

    def _plant_session(self, name: str = "shelf") -> Path:
        self.request("PUT", "/api/settings", self._job(session_name=name))
        session = self.output / name
        frames = session / "frames"
        frames.mkdir(parents=True)
        good = make_jpeg()
        (frames / "frame_000001.jpg").write_bytes(good)
        (frames / "frame_000002.jpg").write_bytes(good)
        (frames / "frame_000005.jpg").write_bytes(b"short")
        video = b"\x00\x00\x00\x18ftypisom-video-bytes"
        (session / "timelapse.mp4").write_bytes(video)
        (session / "meta.json").write_text(
            '{"frames": 2, "video_fps": 30, "interval_s": 10}\n',
            encoding="utf-8",
        )
        (session / "latest.jpg").write_bytes(good)
        return session

    def test_download_zip_video_and_delete(self) -> None:
        session = self._plant_session()
        code, listing = self.request("GET", "/api/sessions")
        self.assertEqual(code, 200)
        row = listing["sessions"][0]
        self.assertEqual(row["name"], "shelf")
        self.assertEqual(row["frames_on_disk"], 2)
        self.assertEqual(row["frame_files"], 3)
        self.assertGreater(row["video_bytes"], 0)

        code, detail = self.request("GET", "/api/sessions/shelf?offset=0&limit=10")
        self.assertEqual(code, 200, detail)
        self.assertEqual(
            [item["name"] for item in detail["frames_page"]["files"]],
            ["frame_000001.jpg", "frame_000002.jpg"],
        )

        code, blob = self.request("GET", "/api/sessions/shelf/frames.zip", raw=True)
        self.assertEqual(code, 200, blob[:80])
        self.assertEqual(self.last_headers["Content-Type"], "application/zip")
        self.assertIn("shelf-frames.zip", self.last_headers["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            self.assertEqual(
                archive.namelist(),
                ["frames/frame_000001.jpg", "frames/frame_000002.jpg"],
            )

        code, blob = self.request("GET", "/api/sessions/shelf/archive.zip", raw=True)
        self.assertEqual(code, 200)
        self.assertIn("shelf-timelapse.zip", self.last_headers["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            names = archive.namelist()
            self.assertIn("timelapse.mp4", names)
            self.assertIn("frames/frame_000001.jpg", names)
            self.assertIn("meta.json", names)
            self.assertEqual(archive.read("timelapse.mp4"), (session / "timelapse.mp4").read_bytes())

        code, video = self.request("GET", "/api/sessions/shelf/video", raw=True)
        self.assertEqual(code, 200)
        self.assertEqual(video, (session / "timelapse.mp4").read_bytes())
        self.assertIn("inline", self.last_headers["Content-Disposition"])
        code, part = self.request(
            "GET",
            "/api/sessions/shelf/video?download=1",
            raw=True,
            headers={"Range": "bytes=0-3"},
        )
        self.assertEqual(code, 206)
        self.assertEqual(part, video[:4])
        self.assertTrue(self.last_headers["Content-Range"].startswith("bytes 0-3/"))
        self.assertIn("attachment", self.last_headers["Content-Disposition"])
        code, denied = self.request(
            "GET",
            "/api/sessions/shelf/video",
            raw=True,
            headers={"Range": "bytes=9999-9999"},
        )
        self.assertEqual(code, 416, denied)

        code, frame = self.request("GET", "/api/sessions/shelf/frames/frame_000001.jpg", raw=True)
        self.assertEqual(code, 200)
        self.assertTrue(frame.startswith(b"\xff\xd8"))
        code, missing = self.request("GET", "/api/sessions/shelf/frames/frame_000005.jpg")
        self.assertEqual(code, 404, missing)
        code, escaped = self.request("GET", "/api/sessions/..%2Fsecret/frames.zip")
        self.assertEqual(code, 400, escaped)

        code, deleted = self.request("DELETE", "/api/sessions/shelf?target=video")
        self.assertEqual(code, 200, deleted)
        self.assertFalse((session / "timelapse.mp4").exists())
        self.assertTrue((session / "frames").is_dir())
        code, deleted = self.request("DELETE", "/api/sessions/shelf?target=frames")
        self.assertEqual(code, 200, deleted)
        self.assertFalse((session / "frames").exists())
        code, deleted = self.request("DELETE", "/api/sessions/shelf?target=session")
        self.assertEqual(code, 200, deleted)
        self.assertFalse(session.exists())

    def test_refuses_to_delete_the_session_being_captured(self) -> None:
        status, payload = self.request("POST", "/api/start", self._job())
        self.assertEqual(status, 200, payload)
        wait_until(lambda: (self.output / "api-print" / "frames").is_dir(), detail=self.service.status)
        code, denied = self.request("DELETE", "/api/sessions/api-print?target=session")
        self.assertEqual(code, 409, denied)
        self.assertTrue((self.output / "api-print").is_dir())
        self.request("POST", "/api/stop", {})
        wait_until(self._settled, detail=self.service.status)

    def test_cli_rejects_a_bad_url_without_starting(self) -> None:
        errors = StringIO()
        with redirect_stderr(errors):
            code = main(["capture", "--url", "http://nope", "--every", "10"])
        self.assertEqual(code, 2)
        self.assertIn("rtsp://", errors.getvalue())
        with redirect_stdout(StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main(["--help"])
        self.assertEqual(caught.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
