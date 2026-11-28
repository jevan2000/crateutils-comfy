"""Native file dialog helpers (no dialog is opened): allowing a picked folder, and the
refusals that happen before anything is launched.

    python -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crateutils_comfy import comfy_glue, desktop  # noqa: E402


class AllowFolder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.user = Path(self.tmp.name) / "user"
        self.user.mkdir()
        self.patch = mock.patch.object(comfy_glue, "user_dir", lambda: self.user)
        self.patch.start()
        self.config = self.user / "config.json"
        self.folder = Path(self.tmp.name) / "art"
        self.folder.mkdir()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def read(self):
        return json.loads(self.config.read_text(encoding="utf-8"))

    def test_creates_the_config(self):
        self.assertEqual(desktop.allow_folder(str(self.folder)), str(self.folder))
        self.assertEqual(self.read()["allowed_folders"], [str(self.folder)])
        self.assertEqual(sorted(p.name for p in self.user.iterdir()), ["config.json"])  # no temp left

    def test_keeps_notify_and_existing_folders(self):
        self.config.write_text(json.dumps({"notify": {"phone": "https://ntfy.sh/x"},
                                           "allowed_folders": ["D:/old"]}), encoding="utf-8")
        desktop.allow_folder(str(self.folder))
        data = self.read()
        self.assertEqual(data["notify"], {"phone": "https://ntfy.sh/x"})
        self.assertEqual(data["allowed_folders"], ["D:/old", str(self.folder)])

    def test_does_not_add_the_same_folder_twice(self):
        desktop.allow_folder(str(self.folder))
        desktop.allow_folder(str(self.folder) + "/")
        self.assertEqual(len(self.read()["allowed_folders"]), 1)

    def test_written_without_a_bom(self):
        desktop.allow_folder(str(self.folder))
        self.assertNotEqual(self.config.read_bytes()[:3], b"\xef\xbb\xbf")

    def test_refuses_a_file_instead_of_a_folder(self):
        f = Path(self.tmp.name) / "notes.txt"
        f.write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            desktop.allow_folder(str(f))
        self.assertFalse(self.config.exists())

    def test_refuses_an_empty_folder(self):
        with self.assertRaises(ValueError):
            desktop.allow_folder("  ")
        with self.assertRaises(ValueError):
            desktop.allow_folder(str(Path(self.tmp.name) / "nope"))


class Pick(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_start_dir_is_the_files_folder(self):
        f = self.folder / "a.svg"
        f.write_text("<svg/>", encoding="utf-8")
        self.assertEqual(Path(desktop.start_dir(str(f))), self.folder)
        self.assertEqual(Path(desktop.start_dir(str(self.folder))), self.folder)
        self.assertEqual(desktop.start_dir(""), "")
        self.assertEqual(desktop.start_dir(str(self.folder / "missing")), "")

    def test_says_why_off_windows(self):
        if desktop.available():
            self.skipTest("this machine can open the dialog")
        with self.assertRaises(ValueError) as caught:
            desktop.pick_file("svg")
        self.assertIn("Windows", str(caught.exception))

    def test_a_second_dialog_is_refused(self):
        self.assertTrue(desktop._busy.acquire(blocking=False))
        try:
            with mock.patch.object(desktop, "available", lambda: True):
                with self.assertRaises(RuntimeError):
                    desktop.pick_file("svg")
        finally:
            desktop._busy.release()


if __name__ == "__main__":
    unittest.main()
