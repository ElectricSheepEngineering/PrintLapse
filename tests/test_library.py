"""Stored sessions: sizes, zip downloads, and deletion."""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from prusa_timelapse.capture import list_sessions
from prusa_timelapse.library import (
    contained_file,
    delete_stored,
    download_filename,
    parse_byte_range,
    session_detail,
    write_zip,
    zip_members,
)
from tests.fake_ffmpeg import make_jpeg


class _Sink:
    def __init__(self) -> None:
        self.buf = bytearray()
        self.pos = 0

    def write(self, data) -> int:
        payload = bytes(data)
        self.buf += payload
        self.pos += len(payload)
        return len(payload)

    def tell(self) -> int:
        return self.pos

    def flush(self) -> None:
        return None

    def seekable(self) -> bool:
        return False

    def writable(self) -> bool:
        return True


class LibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.session = self.root / "bench print"
        frames = self.session / "frames"
        frames.mkdir(parents=True)
        good = make_jpeg()
        (frames / "frame_000001.jpg").write_bytes(good)
        (frames / "frame_000002.jpg").write_bytes(good)
        (frames / "frame_000004.jpg").write_bytes(good)
        (frames / "notes.txt").write_bytes(b"not a frame")
        (frames / "frame_000005.jpg").write_bytes(b"short")
        self.video = b"\x00\x00\x00\x18ftypisom-video-bytes"
        (self.session / "timelapse.mp4").write_bytes(self.video)
        (self.session / "meta.json").write_text('{"frames": 2}\n', encoding="utf-8")
        (self.session / "latest.jpg").write_bytes(good)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_listing_counts_bytes_and_stops_the_sequence_at_a_gap(self) -> None:
        rows = list_sessions(self.root)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["name"], "bench print")
        self.assertEqual(row["frames_on_disk"], 2)
        self.assertEqual(row["frame_files"], 4)
        self.assertGreater(row["frames_bytes"], row["frames_on_disk"])
        self.assertEqual(row["video_bytes"], len(self.video))
        self.assertTrue(row["has_video"])

    def test_zip_contains_finished_frames_and_the_video(self) -> None:
        frames = zip_members(self.session, "frames")
        self.assertEqual(
            [arc for _, arc in frames],
            ["frames/frame_000001.jpg", "frames/frame_000002.jpg", "frames/frame_000004.jpg"],
        )
        sink = _Sink()
        write_zip(frames, sink)
        with zipfile.ZipFile(io.BytesIO(bytes(sink.buf))) as archive:
            self.assertEqual(archive.read("frames/frame_000001.jpg")[:2], b"\xff\xd8")
        archive_members = zip_members(self.session, "archive")
        names = [arc for _, arc in archive_members]
        self.assertIn("timelapse.mp4", names)
        self.assertIn("meta.json", names)
        self.assertIn("latest.jpg", names)
        self.assertNotIn("frames/notes.txt", names)
        self.assertNotIn("frames/frame_000005.jpg", names)

    def test_detail_skips_a_partial_frame(self) -> None:
        detail = session_detail(self.session, 0, 10)
        shown = [item["name"] for item in detail["frames_page"]["files"]]
        self.assertEqual(
            shown,
            ["frame_000001.jpg", "frame_000002.jpg", "frame_000004.jpg"],
        )
        self.assertEqual(detail["frames_page"]["total"], 4)

    def test_paths_cannot_leave_the_session(self) -> None:
        with self.assertRaises(FileNotFoundError):
            contained_file(self.session, "../secret.txt")
        with self.assertRaises(FileNotFoundError):
            contained_file(self.session, "frames/../../meta.json")
        self.assertTrue(contained_file(self.session, "timelapse.mp4").is_file())

    def test_delete_frames_keeps_the_video_and_session_delete_removes_the_folder(self) -> None:
        delete_stored(self.root, "bench print", "frames")
        self.assertFalse((self.session / "frames").exists())
        self.assertTrue((self.session / "timelapse.mp4").is_file())
        delete_stored(self.root, "bench print", "video")
        self.assertFalse((self.session / "timelapse.mp4").exists())
        delete_stored(self.root, "bench print", "session")
        self.assertFalse(self.session.exists())

    def test_delete_does_not_follow_a_symlink_out_of_the_session(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        frames = self.session / "frames"
        # Replace the real frames directory with a link to a sibling folder.
        for child in frames.iterdir():
            child.unlink()
        frames.rmdir()
        frames.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            delete_stored(self.root, "bench print", "frames")
        self.assertEqual((outside / "keep.txt").read_text(encoding="utf-8"), "keep")
        self.assertTrue(frames.is_symlink())

    def test_download_names_cannot_break_a_header(self) -> None:
        name = download_filename('say "hi"\r\nX', "frames.zip")
        self.assertNotIn('"', name)
        self.assertNotIn("\n", name)
        self.assertNotIn("\r", name)
        self.assertTrue(name.endswith("-frames.zip"))

    def test_byte_ranges(self) -> None:
        self.assertIsNone(parse_byte_range(None, 10))
        self.assertEqual(parse_byte_range("bytes=0-3", 10), (0, 3))
        self.assertEqual(parse_byte_range("bytes=8-", 10), (8, 9))
        self.assertEqual(parse_byte_range("bytes=-4", 10), (6, 9))
        self.assertEqual(parse_byte_range("bytes=0-100", 10), (0, 9))
        with self.assertRaises(ValueError):
            parse_byte_range("bytes=20-30", 10)


if __name__ == "__main__":
    unittest.main()
