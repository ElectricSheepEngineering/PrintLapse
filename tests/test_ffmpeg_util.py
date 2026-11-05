"""Command building, URL redaction, and JPEG parsing."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from prusa_timelapse.ffmpeg_util import (
    build_capture_args,
    build_encode_args,
    build_snapshot_args,
    jpeg_size_bytes,
    rate_filter,
    redact_url,
    redact_urls_in_text,
    validate_rtsp_url,
)
from tests.fake_ffmpeg import make_jpeg


class FfmpegUtilTests(unittest.TestCase):
    def test_rate_filter_uses_a_rational(self) -> None:
        self.assertEqual(rate_filter(10), "fps=1/10")
        self.assertEqual(rate_filter(1), "fps=1/1")
        self.assertEqual(rate_filter(2.5), "fps=2/5")
        self.assertEqual(rate_filter(0.2), "fps=5/1")

    def test_rate_filter_rejects_zero(self) -> None:
        with self.assertRaises(ValueError):
            rate_filter(0)

    def test_validate_and_redact_url(self) -> None:
        url = validate_rtsp_url("RTSP://cam:secret@10.1.1.5:554/live")
        self.assertTrue(url.startswith("rtsp://"))
        self.assertIn("secret", url)
        self.assertEqual(redact_url(url), "rtsp://cam:***@10.1.1.5:554/live")
        self.assertEqual(
            redact_urls_in_text(f"Opening {url} now"),
            "Opening rtsp://cam:***@10.1.1.5:554/live now",
        )
        self.assertEqual(redact_url("rtsp://10.1.1.5/live"), "rtsp://10.1.1.5/live")

    def test_validate_rejects_other_schemes(self) -> None:
        with self.assertRaises(ValueError):
            validate_rtsp_url("http://10.1.1.5/live")
        with self.assertRaises(ValueError):
            validate_rtsp_url("")
        with self.assertRaises(ValueError):
            validate_rtsp_url("rtsp://")

    def test_capture_command_keeps_the_password_for_ffmpeg(self) -> None:
        args = build_capture_args(
            "ffmpeg",
            "rtsp://cam:secret@10.0.0.8/live",
            10,
            3,
            1280,
            "tcp",
            4,
            Path("/tmp/frames/frame_%06d.jpg"),
        )
        self.assertEqual(args[args.index("-rtsp_transport") + 1], "tcp")
        self.assertIn("-use_wallclock_as_timestamps", args)
        self.assertIn("fps=1/10,scale=1280:-2", args)
        self.assertEqual(args[args.index("-start_number") + 1], "4")
        self.assertIn("rtsp://cam:secret@10.0.0.8/live", args)
        self.assertEqual(args[args.index("-q:v") + 1], "3")

    def test_snapshot_and_encode_commands(self) -> None:
        snap = build_snapshot_args("ffmpeg", "rtsp://10.0.0.8/live", 3, 0, "udp", Path("/tmp/a.jpg"))
        self.assertEqual(snap[snap.index("-rtsp_transport") + 1], "udp")
        self.assertIn("-frames:v", snap)
        self.assertNotIn("-vf", snap)
        encode = build_encode_args("ffmpeg", Path("/tmp/frames"), Path("/tmp/out.mp4"), 24)
        self.assertIn("libx264", encode)
        self.assertEqual(encode[encode.index("-framerate") + 1], "24")
        self.assertEqual(encode[-1], "/tmp/out.mp4")

    def test_jpeg_size_reads_the_sof_marker(self) -> None:
        self.assertEqual(jpeg_size_bytes(make_jpeg(32, 18)), (32, 18))
        self.assertIsNone(jpeg_size_bytes(b"not a jpeg"))


if __name__ == "__main__":
    unittest.main()
