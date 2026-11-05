"""Settings files, session names, and output folders."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from prusa_timelapse.capture import allocate_session_dir
from prusa_timelapse.settings import (
    SettingsStore,
    app_config_dir,
    data_root,
    default_output_dir,
    normalize_settings,
    sanitize_session_name,
)
from tests.support import Env


class SettingsTests(unittest.TestCase):
    def test_round_trip_and_corrupt_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            store = SettingsStore(path)
            saved = store.save({"url": "rtsp://10.0.0.5/live", "interval_s": 5, "video_fps": 24})
            self.assertEqual(saved["url"], "rtsp://10.0.0.5/live")
            self.assertEqual(saved["video_fps"], 24)
            self.assertTrue(saved["auto_encode"])
            self.assertEqual(store.load()["interval_s"], 5)
            path.write_text("{", encoding="utf-8")
            self.assertEqual(store.load()["interval_s"], 10)

    def test_rejects_bad_interval_and_names(self) -> None:
        with self.assertRaises(ValueError):
            normalize_settings({"interval_s": 0})
        with self.assertRaises(ValueError):
            normalize_settings({"transport": "sctp"})
        self.assertEqual(
            sanitize_session_name("  Bench print  "),
            "Bench print",
        )
        self.assertEqual(
            sanitize_session_name("", now=datetime(2026, 11, 4, 15, 1, 2)),
            "print-20261104-150102",
        )
        with self.assertRaises(ValueError):
            sanitize_session_name("CON")
        with self.assertRaises(ValueError):
            sanitize_session_name("..")

    def test_session_folder_gets_a_suffix_when_taken(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "print").mkdir()
            self.assertEqual(allocate_session_dir(root, "print"), root / "print-2")

    def test_data_dir_env_keeps_settings_and_sessions_on_one_volume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with Env(PRUSA_TIMELAPSE_DATA=str(root)):
                self.assertEqual(data_root(), root)
                self.assertEqual(app_config_dir(), root / "config")
                self.assertEqual(default_output_dir(), root / "timelapses")
            self.assertTrue((root / "config").is_dir())
            self.assertTrue((root / "timelapses").is_dir())
            self.assertIsNone(data_root())

    def test_saved_json_is_an_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            SettingsStore(path).save({"url": "rtsp://10.0.0.5/live"})
            self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), dict)


if __name__ == "__main__":
    unittest.main()
