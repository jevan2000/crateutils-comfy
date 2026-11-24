"""Export tests (no ComfyUI needed): every format writes, reads back, and keeps what it promises.

    python -m unittest discover -s tests -v
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crateutils_comfy import exporter as ex  # noqa: E402


def gradient(w=64, h=48, alpha=True):
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    rgba = np.stack([x / (w - 1), y / (h - 1), np.full_like(x, 0.25), np.ones_like(x)], axis=-1)
    if alpha:
        rgba[:, w // 2:, 3] = 0.0
    return rgba


def noisy(w=320, h=240, seed=1):
    rng = np.random.default_rng(seed)
    a = rng.random((h, w, 4), dtype=np.float32)
    a[..., 3] = 1
    return a


class Formats(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.dir = Path(self.d.name)

    def tearDown(self):
        self.d.cleanup()

    def save(self, frames, **kw):
        s = ex.Settings(**kw)
        return ex.save(frames, s, lambda i, ext: str(self.dir / f"out_{i}{ext}"))

    def test_every_pillow_still_format_reads_back(self):
        for fmt in ("png", "jpeg", "webp", "tiff", "bmp", "tga", "ico", "jpeg 2000", "avif"):
            with self.subTest(fmt=fmt):
                r = self.save([gradient()], fmt=fmt)
                self.assertEqual(len(r), 1)
                with Image.open(r[0].path) as im:
                    im.load()
                    if fmt != "ico":
                        self.assertEqual(im.size, (64, 48))

    def test_png_alpha_kept_and_flattened(self):
        r = self.save([gradient()], fmt="png")
        with Image.open(r[0].path) as im:
            self.assertEqual(im.mode, "RGBA")
            self.assertEqual(im.getpixel((60, 10))[3], 0)
        r = self.save([gradient()], fmt="png", transparent=False, matte="#00ff00")
        with Image.open(r[0].path) as im:
            self.assertEqual(im.mode, "RGB")
            self.assertEqual(im.getpixel((60, 10))[1], 255)

    def test_png16_is_16_bit_and_precise(self):
        src = gradient(alpha=False)
        r = self.save([src], fmt="png", bit_depth="16", meta={"prompt": '{"a": 1}'})
        cv2 = _cv2()
        if cv2 is not None:
            arr = cv2.imdecode(np.fromfile(r[0].path, np.uint8), cv2.IMREAD_UNCHANGED)
            self.assertEqual(arr.dtype, np.uint16)
            self.assertLess(np.abs(arr[..., 2::-1] / 65535.0 - src[..., :3]).max(), 1e-4)
        with Image.open(r[0].path) as im:  # Pillow reads the header and the text chunk
            self.assertEqual(im.size, (64, 48))
            self.assertEqual(im.info.get("prompt"), '{"a": 1}')

    def test_exr_roundtrip(self):
        """Decoded by ffmpeg's own EXR reader (an independent implementation)."""
        src = gradient()
        a = src[..., 3]
        expect = np.concatenate([src[..., :3] * a[..., None], a[..., None]], -1)  # premultiplied
        for depth, comp, tol in (("16 float", 6, 2e-3), ("16 float", 0, 2e-3), ("32 float", 0, 1e-6),
                                 ("32 float", 6, 1e-6)):
            with self.subTest(depth=depth, comp=comp):
                r = self.save([src], fmt="exr (float)", bit_depth=depth, compression=comp, exr_linear=False)
                self.assertEqual(Path(r[0].path).read_bytes()[:4], b"\x76\x2f\x31\x01")
                try:
                    ff = ex.ffmpeg_path()
                except RuntimeError:
                    continue
                p = subprocess.run([ff, "-v", "error", "-i", r[0].path, "-f", "rawvideo", "-pix_fmt", "gbrapf32le",
                                    "-"], capture_output=True)
                self.assertEqual(p.returncode, 0, p.stderr)
                g, b, rr, al = np.frombuffer(p.stdout, np.float32).reshape(4, 48, 64)
                self.assertLess(np.abs(np.stack([rr, g, b, al], -1) - expect).max(), tol)

    def test_tiff_16(self):
        if _cv2() is None:
            self.skipTest("no OpenCV")
        r = self.save([gradient()], fmt="tiff", bit_depth="16")
        self.assertIn("16-bit", r[0].detail)
        arr = _cv2().imdecode(np.fromfile(r[0].path, np.uint8), -1)
        self.assertEqual((arr.dtype, arr.shape), (np.uint16, (48, 64, 4)))

    def test_max_kb_lowers_quality(self):
        r = self.save([noisy()], fmt="jpeg", quality=95, max_kb=40)
        self.assertLessEqual(os.path.getsize(r[0].path), 40 * 1024)
        self.assertIn("lowered", r[0].detail)

    def test_presets(self):
        s = ex.apply_preset(ex.Settings(fmt="png"), "lossless")
        self.assertEqual(s.bit_depth, "16")
        s = ex.apply_preset(ex.Settings(fmt="webp"), "lossless")
        self.assertTrue(s.lossless)
        s = ex.apply_preset(ex.Settings(fmt="jpeg", quality=50), "high")
        self.assertEqual((s.quality, s.subsampling), (92, "4:4:4"))

    def test_webp_lossless_is_exact(self):
        src = gradient(alpha=False)
        r = self.save([src], fmt="webp", lossless=True)
        with Image.open(r[0].path) as im:
            got = np.asarray(im.convert("RGB"), np.float32) / 255
        self.assertLess(np.abs(got - src[..., :3]).max(), 1 / 255 + 1e-6)

    def test_batch_is_one_file_per_image(self):
        r = self.save([gradient(), gradient()], fmt="png")
        self.assertEqual(len(r), 2)

    def test_animations_and_pdf(self):
        frames = [gradient(), np.roll(gradient(), 8, axis=1), np.roll(gradient(), 16, axis=1)]
        for fmt in ("gif (animated)", "webp (animated)", "apng (animated png)", "pdf (pages)"):
            with self.subTest(fmt=fmt):
                r = self.save(frames, fmt=fmt, fps=10)
                self.assertEqual(len(r), 1)
                if fmt == "pdf (pages)":
                    self.assertTrue(Path(r[0].path).read_bytes().startswith(b"%PDF"))
                    continue
                with Image.open(r[0].path) as im:
                    self.assertEqual(getattr(im, "n_frames", 1), 3)

    def test_animation_loop_counts(self):
        # loop=n means "play n times": GIF stores n-1 (or omits it for once),
        # APNG and WebP store n directly (0 = forever).
        frames = [gradient(), np.roll(gradient(), 8, axis=1)]
        cases = {
            "gif (animated)": {0: 0, 1: None, 2: 1, 3: 2},
            "apng (animated png)": {0: 0, 1: 1, 2: 2, 3: 3},
            "webp (animated)": {0: 0, 1: 1, 2: 2, 3: 3},
        }
        for fmt, wanted in cases.items():
            for n, stored in wanted.items():
                with self.subTest(fmt=fmt, loop=n):
                    r = self.save(frames, fmt=fmt, loop=n)
                    with Image.open(r[0].path) as im:
                        self.assertEqual(im.info.get("loop"), stored)

    def test_presets_reset_max_kb_but_custom_keeps_it(self):
        s = ex.Settings(fmt="jpeg", max_kb=500)
        for preset in ("max quality", "high", "balanced", "small", "lossless"):
            with self.subTest(preset=preset):
                self.assertEqual(ex.apply_preset(s, preset).max_kb, 0)
        self.assertEqual(ex.apply_preset(s, "custom").max_kb, 500)

    def test_batch_num_keeps_the_counter_moving(self):
        def save_path(prefix, base_dir, width, height):
            sub = os.path.dirname(os.path.normpath(prefix))
            base = os.path.basename(os.path.normpath(prefix))
            folder = os.path.join(base_dir, sub)
            os.makedirs(folder, exist_ok=True)
            counter = 1
            for name in os.listdir(folder):
                if name.startswith(base + "_"):
                    digits = [p for p in os.path.splitext(name)[0].split("_") if p.isdigit()]
                    if digits:
                        counter = max(counter, int(digits[-1]) + 1)
            return folder, base, counter, sub, prefix

        with tempfile.TemporaryDirectory() as d:
            for run in range(3):
                with self.subTest(run=run):
                    out, base, counter, _, has = ex.batch_plan("shots/%batch_num%", d, 8, 8, save_path)
                    self.assertTrue(has)
                    for i in range(2):
                        p = Path(out) / f"{ex.batch_name(base, i, has)}_{counter + i:05}_.png"
                        self.assertFalse(p.exists(), f"run {run} would overwrite {p.name}")
                        p.write_bytes(b"x")
            with self.assertRaises(ValueError):
                ex.batch_plan("shots/%batch_num%/x", d, 8, 8, save_path)

    def test_mixed_sizes_refused_for_animation(self):
        with self.assertRaises(ValueError):
            self.save([gradient(), gradient(32, 32)], fmt="gif (animated)")


@unittest.skipUnless(shutil.which("ffmpeg") or os.environ.get("CRATEUTILS_COMFY_FFMPEG"), "ffmpeg not installed")
class Video(unittest.TestCase):
    def test_codecs(self):
        frames = [np.roll(gradient(65, 49), i * 4, axis=1) for i in range(6)]  # odd size: needs padding
        wave = np.sin(np.linspace(0, 400, 8000))[None].repeat(2, 0)
        with tempfile.TemporaryDirectory() as d:
            for fmt in [f for f, v in ex.FORMATS.items() if v.kind == "video"]:
                for lossless in (False, True):
                    with self.subTest(fmt=fmt, lossless=lossless):
                        s = ex.Settings(fmt=fmt, fps=12, lossless=lossless, meta={"prompt": '{"x": "a=b;c"}'})
                        r = ex.save(frames, s, lambda i, ext: os.path.join(d, f"v{lossless}{ext}"), (wave, 8000))
                        ff = Path(ex.ffmpeg_path())
                        fp = str(ff.with_name(ff.name.replace("ffmpeg", "ffprobe")))
                        if not os.path.isfile(fp):
                            continue
                        probe = subprocess.run([fp, "-v", "error",
                                                "-count_frames", "-show_entries", "stream=codec_type,nb_read_frames",
                                                "-of", "csv=p=0", r[0].path], capture_output=True, text=True)
                        if probe.returncode == 0:
                            self.assertIn("video,6", probe.stdout.replace("\r", ""))
                            self.assertIn("audio", probe.stdout)


def _cv2(exr=False):
    if exr:
        os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
    try:
        import cv2

        return cv2
    except ImportError:
        return None


if __name__ == "__main__":
    unittest.main()
