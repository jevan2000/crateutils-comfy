"""SVG page cache tests (no ComfyUI needed): keys, the on-disk cache, render requests.

    python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crateutils_comfy import svgcache  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"pretend pixels" * 4
DIGEST = "a1b2c3d4e5f6" + "0" * 52


class Keys(unittest.TestCase):
    def test_label_slug_matches_the_frontend(self):
        self.assertEqual(svgcache.label_slug(""), "x")
        self.assertEqual(svgcache.label_slug("Home Page"), "home-page")
        self.assertEqual(svgcache.label_slug("  A/B Test  "), "a-b-test")
        self.assertEqual(svgcache.label_slug("Über uns"), "ber-uns")
        self.assertEqual(svgcache.label_slug("x" * 40), "x" * 24)

    def test_key_shape(self):
        k = svgcache.cache_key(DIGEST, 2, "Home Page", 800, 600, "page colour")
        self.assertEqual(k, f"v1_{DIGEST[:16]}_p2_lhome-page_800x600_page")
        self.assertTrue(svgcache.KEY_RE.match(k))

    def test_page_is_at_least_one(self):
        k = svgcache.cache_key(DIGEST, 0, "", 0, 0, "white")
        self.assertTrue(k.startswith(f"v1_{DIGEST[:16]}_p1_"))

    def test_every_parameter_changes_the_key(self):
        base = svgcache.cache_key(DIGEST, 1, "", 100, 100, "transparent")
        variants = {
            svgcache.cache_key("b" * 64, 1, "", 100, 100, "transparent"),
            svgcache.cache_key(DIGEST, 2, "", 100, 100, "transparent"),
            svgcache.cache_key(DIGEST, 1, "Cover", 100, 100, "transparent"),
            svgcache.cache_key(DIGEST, 1, "", 200, 100, "transparent"),
            svgcache.cache_key(DIGEST, 1, "", 100, 200, "transparent"),
            svgcache.cache_key(DIGEST, 1, "", 100, 100, "white"),
        }
        self.assertEqual(len(variants), 6)
        self.assertNotIn(base, variants)

    def test_unknown_background_falls_back_to_transparent(self):
        self.assertTrue(svgcache.cache_key(DIGEST, 1, "", 1, 1, "chartreuse").endswith("_transparent"))


class Cache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.patch = mock.patch.object(svgcache, "cache_dir", lambda: self.dir)
        self.patch.start()
        svgcache._pending.clear()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()
        svgcache._pending.clear()

    def key(self, **kw):
        args = {"page": 1, "page_label": "", "width": 0, "height": 0, "background": "transparent"}
        args.update(kw)
        return svgcache.cache_key(DIGEST, args["page"], args["page_label"], args["width"],
                                  args["height"], args["background"])

    def test_store_then_read(self):
        k = self.key()
        path = svgcache.store(k, PNG)
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), PNG)
        self.assertEqual(svgcache.cache_path(k), path)

    def test_store_refuses_bad_keys(self):
        for bad in ["", "../../evil", "v1_short_p1_lx_1x1_transparent.png", "x" * 80]:
            with self.assertRaises(ValueError):
                svgcache.store(bad, PNG)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_store_refuses_non_png_and_empty(self):
        k = self.key()
        with self.assertRaises(ValueError):
            svgcache.store(k, b"")
        with self.assertRaises(ValueError):
            svgcache.store(k, b"GIF89a not a png")

    def test_store_leaves_no_temp_files(self):
        svgcache.store(self.key(), PNG)
        self.assertEqual([p.suffix for p in self.dir.iterdir()], [".png"])

    def test_prune_keeps_the_newest(self):
        for i in range(5):
            p = self.dir / f"{i}.png"
            p.write_bytes(PNG)
            os.utime(p, (time.time() - i * 60, time.time() - i * 60))
        self.assertEqual(svgcache.prune(keep=2), 3)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["0.png", "1.png"])

    def test_prune_drops_entries_older_than_the_limit(self):
        old = self.dir / "old.png"
        old.write_bytes(PNG)
        age = time.time() - svgcache.MAX_AGE - 60
        os.utime(old, (age, age))
        self.assertEqual(svgcache.prune(keep=100), 1)
        self.assertFalse(old.exists())

    def test_storing_prunes_old_entries(self):
        old = self.dir / "old.png"
        old.write_bytes(PNG)
        age = time.time() - svgcache.MAX_AGE - 60
        os.utime(old, (age, age))
        svgcache.store(self.key(), PNG)
        self.assertFalse(old.exists())
        self.assertTrue(svgcache.cache_path(self.key()).exists())


class Requests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.patch = mock.patch.object(svgcache, "cache_dir", lambda: self.dir)
        self.patch.start()
        svgcache._pending.clear()
        self.key = svgcache.cache_key(DIGEST, 1, "", 800, 600, "transparent")

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()
        svgcache._pending.clear()

    def test_request_dedupes_by_key(self):
        a = svgcache.request(self.key, "a.svg", 1, "", 800, 600, "transparent")
        b = svgcache.request(self.key, "b.svg", 2, "", 10, 10, "white")
        self.assertIs(a, b)
        self.assertEqual(len(svgcache.list_pending()), 1)

    def test_request_carries_what_the_browser_needs(self):
        svgcache.request(self.key, "D:/art/page.svg", 2, "Home Page", 800, 600, "white")
        req, = svgcache.list_pending()
        self.assertEqual(req, {"key": self.key, "path": "D:/art/page.svg", "page": 2,
                               "page_label": "Home Page", "width": 800, "height": 600,
                               "background": "white"})

    def test_wait_gives_up_when_nothing_draws(self):
        svgcache.request(self.key, "a.svg", 1, "", 0, 0, "transparent")
        started = time.time()
        self.assertIsNone(svgcache.wait(self.key, 0.05))
        self.assertLess(time.time() - started, 2)
        self.assertEqual(svgcache.list_pending(), [])  # the request is not left behind

    def test_a_drawn_page_resolves_the_waiter(self):
        svgcache.request(self.key, "a.svg", 1, "", 0, 0, "transparent")

        def draw():
            time.sleep(0.05)
            svgcache.store(self.key, PNG)

        t = threading.Thread(target=draw)
        t.start()
        path = svgcache.wait(self.key, 2.0)
        t.join()
        self.assertIsNotNone(path)
        self.assertEqual(path.read_bytes(), PNG)

    def test_wait_on_an_already_drawn_page_returns_it(self):
        svgcache.store(self.key, PNG)
        path = svgcache.wait(self.key, 0.01)
        self.assertIsNotNone(path)

    def test_old_requests_are_forgotten(self):
        r = svgcache.request(self.key, "a.svg", 1, "", 0, 0, "transparent")
        r.created -= svgcache.PENDING_TTL + 1
        self.assertEqual(svgcache.list_pending(), [])
        svgcache.request(self.key, "a.svg", 1, "", 0, 0, "transparent")
        self.assertEqual(len(svgcache.list_pending()), 1)

    def test_pending_is_capped(self):
        for i in range(svgcache.MAX_PENDING + 4):
            svgcache.request(svgcache.cache_key(DIGEST, i + 1, "", 0, 0, "transparent"),
                             "a.svg", i + 1, "", 0, 0, "transparent")
        self.assertLessEqual(len(svgcache.list_pending()), svgcache.MAX_PENDING)


if __name__ == "__main__":
    unittest.main()
