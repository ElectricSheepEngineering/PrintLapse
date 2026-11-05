"""Capture, reconnect, and encode using a stand-in ffmpeg."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from prusa_timelapse.capture import CaptureService, config_from_dict
from prusa_timelapse.ffmpeg_util import is_complete_jpeg
from prusa_timelapse.settings import normalize_settings
from tests.support import Env, ensure_fake, wait_until


class CaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = ensure_fake()
        self.tmp = tempfile.TemporaryDirectory()
        self.output = Path(self.tmp.name) / "out"
        self.argv_path = Path(self.tmp.name) / "argv.txt"
        self.service = CaptureService(
            ffmpeg=self.fake,
            preview_dir=Path(self.tmp.name) / "preview",
            max_failures=3,
            retry_delay=0.05,
            snapshot_timeout=3,
        )
        self.env = Env(
            FAKE_FFMPEG_MODE="stream",
            FAKE_FFMPEG_DELAY="0.05",
            FAKE_FFMPEG_ARGV=str(self.argv_path),
        )
        self.env.__enter__()

    def tearDown(self) -> None:
        self.service.request_stop()
        self.service.join(timeout=8)
        self.env.__exit__(None, None, None)
        self.tmp.cleanup()

    def _config(self, **overrides):
        data = {
            "url": "rtsp://cam:secret@127.0.0.1/live",
            "interval_s": 10,
            "video_fps": 30,
            "output_dir": str(self.output),
            "session_name": "unit-print",
            "auto_encode": True,
            "keep_frames": True,
        }
        data.update(overrides)
        return config_from_dict(normalize_settings(data))

    def test_capture_stop_encodes_without_leaking_the_password(self) -> None:
        self.service.start(self._config())
        wait_until(lambda: self.service.status()["frames"] >= 2, detail=self.service.status)
        argv = self.argv_path.read_text(encoding="utf-8")
        self.assertIn("rtsp://cam:secret@127.0.0.1/live", argv)
        self.assertIn("fps=1/10", argv)
        self.assertIn("-rtsp_transport\ntcp", argv.replace(" ", ""))
        self.service.request_stop()
        wait_until(lambda: self.service.status()["phase"] == "idle", detail=self.service.status)
        status = self.service.status()
        self.assertIsNone(status["last_error"], status)
        self.assertGreaterEqual(status["frames"], 2)
        self.assertTrue(status["video_path"])
        self.assertTrue(Path(status["video_path"]).is_file())
        frames = list((Path(status["session_dir"]) / "frames").glob("frame_*.jpg"))
        self.assertGreaterEqual(len(frames), 2)
        self.assertTrue(is_complete_jpeg(frames[0]))
        meta = (Path(status["session_dir"]) / "meta.json").read_text(encoding="utf-8")
        self.assertNotIn("secret", meta)
        self.assertNotIn("secret", "\n".join(self.service.log_lines()))
        self.assertIn("rtsp://cam:***@127.0.0.1/live", meta)
        loaded = json.loads(meta)
        self.assertEqual(loaded["video"], "timelapse.mp4")

    def test_duration_limit_builds_a_video_and_can_drop_frames(self) -> None:
        config = self._config(
            session_name="limited",
            max_duration_min=0.02,
            keep_frames=False,
        )
        self.assertAlmostEqual(config.max_duration_s or 0, 1.2)
        self.service.start(config)
        wait_until(lambda: self.service.status()["phase"] == "idle", timeout=12, detail=self.service.status)
        status = self.service.status()
        self.assertIsNone(status["last_error"], status)
        self.assertGreater(status["frames"], 0)
        self.assertTrue(Path(status["video_path"]).is_file())
        self.assertFalse((Path(status["session_dir"]) / "frames").exists())
        self.assertIn("time limit", status["message"].lower())

    def test_unreachable_stream_reports_ffmpeg_and_stops(self) -> None:
        os.environ["FAKE_FFMPEG_MODE"] = "fail"
        self.service.start(self._config(session_name="down", auto_encode=True))
        wait_until(lambda: self.service.status()["phase"] == "idle", timeout=8, detail=self.service.status)
        status = self.service.status()
        self.assertEqual(status["frames"], 0)
        self.assertIsNone(status["video_path"])
        self.assertIn("Connection refused", status["last_error"] or "")

    def test_stalled_stream_reconnects_then_gives_up(self) -> None:
        os.environ["FAKE_FFMPEG_MODE"] = "hang"
        self.service._stall_grace = 0.35
        self.service.start(self._config(session_name="stalled"))
        wait_until(lambda: self.service.status()["phase"] == "idle", timeout=8, detail=self.service.status)
        status = self.service.status()
        self.assertGreaterEqual(status["reconnects"], 1)
        self.assertIn("could not keep frames", status["last_error"] or "")

    def test_connection_check_publishes_a_preview(self) -> None:
        self.service.test_connection("rtsp://127.0.0.1/live", "tcp", 3, 0)
        wait_until(lambda: self.service.status()["phase"] == "idle", detail=self.service.status)
        status = self.service.status()
        self.assertIsNone(status["last_error"], status)
        self.assertEqual((status["width"], status["height"]), (32, 18))
        self.assertGreaterEqual(status["preview_rev"], 1)
        self.assertTrue(self.service.preview_file.is_file())

    def test_failed_connection_check(self) -> None:
        os.environ["FAKE_FFMPEG_MODE"] = "fail"
        self.service.test_connection("rtsp://127.0.0.1/live", "tcp", 3, 0)
        wait_until(lambda: self.service.status()["phase"] == "idle", detail=self.service.status)
        self.assertIn("Connection refused", self.service.status()["last_error"] or "")


if __name__ == "__main__":
    unittest.main()
