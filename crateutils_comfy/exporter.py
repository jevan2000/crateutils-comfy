"""Export frames (straight-alpha float RGBA, H x W x 4, 0..1) to image, animation and video files.

Pure Python + numpy + Pillow (ffmpeg for video, OpenCV only for 16/32-bit TIFF), no
ComfyUI imports, so it can be unit-tested on its own. 16-bit PNG and EXR are written
by small built-in writers, so they need nothing extra.
"""

from __future__ import annotations

import io
import os
import shutil
import struct
import subprocess
import tempfile
import wave
import zlib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageFile


@dataclass(frozen=True)
class Fmt:
    ext: str
    kind: str              # still (one file per image), multi (one file for the batch), video, audio
    alpha: bool            # can keep transparency
    lossy: bool            # has a quality setting
    depths: tuple = ("8",)
    lossless_ok: bool = True  # has a truly lossless mode
    browser: bool = False  # ComfyUI can preview it


FORMATS: dict[str, Fmt] = {
    "png": Fmt(".png", "still", True, False, ("8", "16"), browser=True),
    "jpeg": Fmt(".jpg", "still", False, True, lossless_ok=False, browser=True),
    "webp": Fmt(".webp", "still", True, True, browser=True),
    "avif": Fmt(".avif", "still", True, True, lossless_ok=False, browser=True),
    "tiff": Fmt(".tif", "still", True, False, ("8", "16", "32 float")),
    "jpeg 2000": Fmt(".jp2", "still", True, True),
    "exr (float)": Fmt(".exr", "still", True, False, ("16 float", "32 float")),
    "bmp": Fmt(".bmp", "still", True, False),
    "tga": Fmt(".tga", "still", True, False),
    "ico": Fmt(".ico", "still", True, False),
    "gif (animated)": Fmt(".gif", "multi", True, False, browser=True),
    "webp (animated)": Fmt(".webp", "multi", True, True, browser=True),
    "apng (animated png)": Fmt(".png", "multi", True, False, browser=True),
    "pdf (pages)": Fmt(".pdf", "multi", False, True, lossless_ok=True),
    "mp4 (H.264)": Fmt(".mp4", "video", False, True),
    "mp4 (H.265 / HEVC)": Fmt(".mp4", "video", False, True),
    "webm (VP9, alpha)": Fmt(".webm", "video", True, True),
    "mov (ProRes 4444, alpha)": Fmt(".mov", "video", True, True, lossless_ok=False),
    "mkv (FFV1, lossless)": Fmt(".mkv", "video", True, False),
    "wav (audio)": Fmt(".wav", "audio", False, False, ("16", "24", "32 float")),
    "flac (audio, lossless)": Fmt(".flac", "audio", False, False, ("16", "24")),
    "mp3 (audio)": Fmt(".mp3", "audio", False, True, lossless_ok=False),
    "opus (audio, .ogg)": Fmt(".ogg", "audio", False, True, lossless_ok=False),
    "m4a (audio, AAC)": Fmt(".m4a", "audio", False, True, lossless_ok=False),
}
PRESETS = ["custom", "lossless", "max quality", "high", "balanced", "small"]
SUBSAMPLING = ["auto", "4:4:4", "4:2:2", "4:2:0"]
TIFF_COMPRESSION = ["deflate", "lzw", "none", "jpeg (8-bit only)"]
DEPTHS = ["8", "16", "24", "32 float"]
MIN_QUALITY = 10
X26X_PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]


@dataclass
class Settings:
    fmt: str = "png"
    quality: int = 92            # lossy formats, 1..100
    lossless: bool = False       # WebP, JPEG 2000, video codecs
    compression: int = 6         # PNG / APNG zlib level 0..9; EXR: 0 = none, else zip
    effort: int = 60             # encoder effort 0..100 (slower = smaller): WebP method, AVIF speed, x264 preset
    bit_depth: str = "8"
    subsampling: str = "auto"
    progressive: bool = True     # JPEG
    tiff_compression: str = "deflate"
    transparent: bool = True
    matte: str = "#ffffff"
    dpi: float = 0.0
    max_kb: int = 0              # lossy stills: lower the quality until the file fits
    fps: float = 24.0
    loop: int = 0                # animations: 0 = forever, n = play n times
    srgb_icc: bool = True
    exr_linear: bool = True      # EXR: convert sRGB to linear light (what compositors expect)
    meta: dict = field(default_factory=dict)  # text metadata, e.g. {"prompt": json, "workflow": json}


@dataclass
class Result:
    path: str
    bytes: int
    width: int
    height: int
    frames: int
    detail: str

    @property
    def kb(self) -> float:
        return self.bytes / 1024.0

    def line(self) -> str:
        fr = f", {self.frames} frames" if self.frames > 1 else ""
        size = f"{self.width}x{self.height}{fr}, " if self.width else ""
        return f"{os.path.basename(self.path)}: {size}{self.kb:,.0f} KB, {self.detail}"


def apply_preset(s: Settings, preset: str) -> Settings:
    """Quality settings for a preset ("custom" keeps the node's own values)."""
    if preset == "custom":
        return s
    f = FORMATS[s.fmt]
    deep = "16" if "16" in f.depths else ("16 float" if "16 float" in f.depths else "8")
    table = {
        "lossless": dict(quality=100, lossless=True, compression=9, effort=80, subsampling="4:4:4", bit_depth=deep,
                         max_kb=0),
        "max quality": dict(quality=100, lossless=False, compression=9, effort=80, subsampling="4:4:4",
                            bit_depth=deep),
        "high": dict(quality=92, lossless=False, compression=6, effort=60, subsampling="4:4:4", bit_depth="8"
                     if s.fmt != "exr (float)" else "16 float"),
        "balanced": dict(quality=82, lossless=False, compression=6, effort=60, subsampling="auto", bit_depth="8"
                         if s.fmt != "exr (float)" else "16 float"),
        "small": dict(quality=65, lossless=False, compression=9, effort=100, subsampling="4:2:0", bit_depth="8"
                      if s.fmt != "exr (float)" else "16 float"),
    }
    if preset not in table:
        raise ValueError(f"unknown preset {preset!r}")
    return replace(s, **table[preset])


# ----------------------------------------------------------------- pixel conversion
def parse_color(s: str) -> tuple[float, float, float]:
    h = (s or "#ffffff").strip().lstrip("#")
    if len(h) in (3, 4):
        h = "".join(c * 2 for c in h[:3])
    try:
        return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError as e:
        raise ValueError(f"matte {s!r} is not a #rrggbb colour") from e


def flatten(rgba: np.ndarray, matte: str) -> np.ndarray:
    a = np.clip(rgba[..., 3:4], 0, 1)
    m = np.array(parse_color(matte), np.float32)
    rgb = np.clip(rgba[..., :3], 0, 1) * a + m * (1 - a)
    return np.concatenate([rgb, np.ones_like(a)], axis=-1).astype(np.float32)


def has_alpha(rgba: np.ndarray) -> bool:
    return bool((rgba[..., 3] < 0.999).any())


def to_pil(rgba: np.ndarray, keep_alpha: bool, matte: str) -> Image.Image:
    if keep_alpha:
        return Image.fromarray(np.round(np.clip(rgba, 0, 1) * 255).astype(np.uint8), "RGBA")
    return Image.fromarray(np.round(flatten(rgba, matte)[..., :3] * 255).astype(np.uint8), "RGB")


def srgb_to_linear(c: np.ndarray) -> np.ndarray:
    c = np.clip(c, 0, None)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4).astype(np.float32)


_ICC: bytes | None = None


def srgb_icc() -> bytes:
    global _ICC
    if _ICC is None:
        from PIL import ImageCms

        _ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    return _ICC


# ----------------------------------------------------------------- built-in writers
def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def png16(rgba: np.ndarray, keep_alpha: bool, matte: str, level: int = 6, dpi: float = 0.0,
          meta: dict | None = None) -> bytes:
    """A 16-bit-per-channel PNG (RGB or RGBA). Pillow can't write 16-bit colour PNGs."""
    px = rgba if keep_alpha else flatten(rgba, matte)[..., :3]
    h, w, c = px.shape
    u16 = np.round(np.clip(px, 0, 1) * 65535).astype(">u2")
    rows = np.concatenate([np.zeros((h, 1), np.uint8), u16.reshape(h, w * c).view(np.uint8)], axis=1)
    out = [b"\x89PNG\r\n\x1a\n",
           _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 16, 6 if c == 4 else 2, 0, 0, 0)),
           _chunk(b"sRGB", b"\x00")]
    if dpi:
        ppm = int(round(dpi / 0.0254))
        out.append(_chunk(b"pHYs", struct.pack(">IIB", ppm, ppm, 1)))
    for k, v in (meta or {}).items():
        out.append(_chunk(b"tEXt", k.encode("latin-1") + b"\x00" + v.encode("latin-1", "replace")))
    out.append(_chunk(b"IDAT", zlib.compress(rows.tobytes(), max(0, min(9, level)))))
    out.append(_chunk(b"IEND", b""))
    return b"".join(out)


def _exr_attr(name: str, typ: str, data: bytes) -> bytes:
    return name.encode() + b"\x00" + typ.encode() + b"\x00" + struct.pack("<i", len(data)) + data


def _exr_zip(raw: bytes) -> bytes:
    """OpenEXR ZIP: interleave even/odd bytes, delta-predict, deflate."""
    b = np.frombuffer(raw, np.uint8)
    t = np.concatenate([b[0::2], b[1::2]])
    d = t.astype(np.int16)
    d[1:] = (t[1:].astype(np.int16) - t[:-1].astype(np.int16) + 128) & 0xFF
    return zlib.compress(d.astype(np.uint8).tobytes(), 6)


def exr(rgba: np.ndarray, keep_alpha: bool, half: bool = True, compress: bool = True, linear: bool = True) -> bytes:
    """A scanline OpenEXR file (half or float), NONE or ZIP compression, premultiplied alpha."""
    h, w = rgba.shape[:2]
    rgb = rgba[..., :3].astype(np.float32)
    if linear:
        rgb = srgb_to_linear(rgb)
    a = np.clip(rgba[..., 3], 0, 1).astype(np.float32)
    if keep_alpha:
        rgb = rgb * a[..., None]
        planes = {"A": a, "B": rgb[..., 2], "G": rgb[..., 1], "R": rgb[..., 0]}
    else:
        planes = {"B": rgb[..., 2], "G": rgb[..., 1], "R": rgb[..., 0]}
    names = sorted(planes)  # channels are stored in alphabetical order
    dt, ptype = (np.dtype("<f2"), 1) if half else (np.dtype("<f4"), 2)
    lines = 16 if compress else 1
    chlist = b"".join(n.encode() + b"\x00" + struct.pack("<iB3xii", ptype, 0, 1, 1) for n in names) + b"\x00"
    box = struct.pack("<iiii", 0, 0, w - 1, h - 1)
    header = (struct.pack("<ii", 20000630, 2)
              + _exr_attr("channels", "chlist", chlist)
              + _exr_attr("compression", "compression", bytes([3 if compress else 0]))
              + _exr_attr("dataWindow", "box2i", box) + _exr_attr("displayWindow", "box2i", box)
              + _exr_attr("lineOrder", "lineOrder", b"\x00")
              + _exr_attr("pixelAspectRatio", "float", struct.pack("<f", 1.0))
              + _exr_attr("screenWindowCenter", "v2f", struct.pack("<ff", 0.0, 0.0))
              + _exr_attr("screenWindowWidth", "float", struct.pack("<f", 1.0)) + b"\x00")
    stacked = np.stack([planes[n] for n in names], axis=1).astype(dt)  # h x C x w
    blocks = []
    for y in range(0, h, lines):
        raw = stacked[y:y + lines].tobytes()
        data = raw
        if compress:
            z = _exr_zip(raw)
            if len(z) < len(raw):
                data = z
        blocks.append(struct.pack("<ii", y, len(data)) + data)
    table_start = len(header)
    pos = table_start + 8 * len(blocks)
    offsets = []
    for blk in blocks:
        offsets.append(pos)
        pos += len(blk)
    return header + struct.pack(f"<{len(offsets)}Q", *offsets) + b"".join(blocks)


def _cv2():
    try:
        import cv2  # type: ignore

        return cv2
    except ImportError as e:
        raise RuntimeError("16-bit and 32-bit TIFF need OpenCV: pip install opencv-python-headless "
                           "(or export 16-bit PNG / EXR, which need nothing extra)") from e


def tiff_deep(rgba: np.ndarray, keep_alpha: bool, matte: str, depth: str, compression: str) -> bytes:
    cv2 = _cv2()
    px = rgba if keep_alpha else flatten(rgba, matte)[..., :3]
    px = px[..., [2, 1, 0, 3]] if px.shape[-1] == 4 else px[..., ::-1]  # OpenCV wants BGR(A)
    arr = np.ascontiguousarray(px.astype(np.float32) if depth == "32 float"
                               else np.round(np.clip(px, 0, 1) * 65535).astype(np.uint16))
    code = {"none": 1, "lzw": 5, "deflate": 8}.get(compression, 8)
    ok, buf = cv2.imencode(".tiff", arr, [cv2.IMWRITE_TIFF_COMPRESSION, code])
    if not ok:
        raise RuntimeError("OpenCV could not encode the TIFF")
    return buf.tobytes()


# ----------------------------------------------------------------- stills
def _webp_exif(meta: dict) -> bytes | None:
    """ComfyUI's own convention for workflow metadata in WebP/AVIF (EXIF Make / Model)."""
    if not meta:
        return None
    ex = Image.Exif()
    if "prompt" in meta:
        ex[0x0110] = "prompt:" + meta["prompt"]
    if "workflow" in meta:
        ex[0x010F] = "workflow:" + meta["workflow"]
    return ex.tobytes()


def _subsampling(s: Settings) -> str:
    if s.subsampling != "auto":
        return s.subsampling
    return "4:4:4" if s.quality >= 90 else "4:2:0"


def _encode_still(rgba: np.ndarray, s: Settings, quality: int) -> tuple[bytes, str]:
    f = FORMATS[s.fmt]
    keep = s.transparent and f.alpha
    icc = {"icc_profile": srgb_icc()} if s.srgb_icc else {}
    dpi = {"dpi": (s.dpi, s.dpi)} if s.dpi else {}
    buf = io.BytesIO()
    if s.fmt == "png":
        if s.bit_depth == "16":
            return png16(rgba, keep, s.matte, s.compression, s.dpi, s.meta), f"16-bit, zlib {s.compression}"
        from PIL.PngImagePlugin import PngInfo

        info = PngInfo()
        for k, v in s.meta.items():
            info.add_text(k, v)
        to_pil(rgba, keep, s.matte).save(buf, "PNG", compress_level=max(0, min(9, s.compression)), pnginfo=info,
                                         **icc, **dpi)
        return buf.getvalue(), f"8-bit, zlib {s.compression}"
    if s.fmt == "jpeg":
        img = to_pil(rgba, False, s.matte)
        sub = _subsampling(replace(s, quality=quality))
        old = ImageFile.MAXBLOCK
        ImageFile.MAXBLOCK = max(old, img.width * img.height * 4 + 65536)  # busy 4:4:4 images need a bigger buffer
        try:
            img.save(buf, "JPEG", quality=quality, optimize=True, progressive=s.progressive,
                     subsampling={"4:4:4": 0, "4:2:2": 1, "4:2:0": 2}[sub], **icc, **dpi)
        finally:
            ImageFile.MAXBLOCK = old
        return buf.getvalue(), f"quality {quality}, {sub}{', progressive' if s.progressive else ''}"
    if s.fmt == "webp":
        img = to_pil(rgba, keep, s.matte)
        exif = _webp_exif(s.meta)
        img.save(buf, "WEBP", lossless=s.lossless, quality=100 if s.lossless else quality,
                 method=round(s.effort / 100 * 6), exact=s.lossless, **({"exif": exif} if exif else {}), **icc)
        return buf.getvalue(), "lossless" if s.lossless else f"quality {quality}"
    if s.fmt == "avif":
        from PIL import features

        if not features.check("avif"):
            raise RuntimeError("this Pillow has no AVIF support; upgrade Pillow to 11.3 or newer")
        img = to_pil(rgba, keep, s.matte)
        exif = _webp_exif(s.meta)
        sub = _subsampling(replace(s, quality=quality))
        img.save(buf, "AVIF", quality=quality, speed=round(10 - s.effort / 10), subsampling=sub,
                 **({"exif": exif} if exif else {}), **icc)
        return buf.getvalue(), f"quality {quality}, {sub}"
    if s.fmt == "tiff":
        comp = s.tiff_compression.split(" ")[0]
        if s.bit_depth in ("16", "32 float"):
            if comp == "jpeg":
                raise ValueError("TIFF JPEG compression is 8-bit only; pick deflate, lzw or none")
            return tiff_deep(rgba, keep, s.matte, s.bit_depth, comp), f"{s.bit_depth}-bit, {comp}"
        jpeg = comp == "jpeg"
        img = to_pil(rgba, keep and not jpeg, s.matte)
        kw = {"compression": {"deflate": "tiff_adobe_deflate", "lzw": "tiff_lzw", "none": "raw", "jpeg": "jpeg"}[comp]}
        if jpeg:
            kw["quality"] = quality
        img.save(buf, "TIFF", **kw, **icc, **dpi)
        return buf.getvalue(), f"8-bit, {comp}"
    if s.fmt == "jpeg 2000":
        img = to_pil(rgba, keep, s.matte)
        if s.lossless:
            img.save(buf, "JPEG2000", irreversible=False)
            return buf.getvalue(), "lossless"
        img.save(buf, "JPEG2000", irreversible=True, quality_mode="dB", quality_layers=[20 + quality * 0.35])
        return buf.getvalue(), f"quality {quality}"
    if s.fmt == "exr (float)":
        half = s.bit_depth != "32 float"
        data = exr(rgba, keep, half, s.compression > 0, s.exr_linear)
        return data, (f"{'half' if half else '32-bit'} float, {'zip' if s.compression > 0 else 'uncompressed'}, "
                      f"{'linear' if s.exr_linear else 'sRGB'}")
    if s.fmt == "bmp":
        to_pil(rgba, keep, s.matte).save(buf, "BMP", **dpi)
        return buf.getvalue(), "uncompressed"
    if s.fmt == "tga":
        to_pil(rgba, keep, s.matte).save(buf, "TGA", rle=s.compression > 0)
        return buf.getvalue(), "RLE" if s.compression > 0 else "uncompressed"
    if s.fmt == "ico":
        img = to_pil(rgba, keep, s.matte)
        side = min(256, max(img.size))
        sizes = [(n, n) for n in (16, 24, 32, 48, 64, 128, 256) if n <= side] or [(side, side)]
        img.save(buf, "ICO", sizes=sizes)
        return buf.getvalue(), "sizes " + ",".join(str(a) for a, _ in sizes)
    raise ValueError(f"{s.fmt} is not a single-image format")


def encode_still(rgba: np.ndarray, s: Settings) -> tuple[bytes, str, bool]:
    """(data, detail, fits). With max_kb, lossy formats use the highest quality that fits."""
    f = FORMATS[s.fmt]
    q = max(1, min(100, int(s.quality)))
    data, detail = _encode_still(rgba, s, q)
    limit = s.max_kb * 1024
    lossy = f.lossy and not (s.lossless and f.lossless_ok)
    if not limit or len(data) <= limit:
        return data, detail, True
    if not lossy:
        return data, detail + f" (over {s.max_kb} KB; this format has no quality to lower)", False
    lo, hi, best = MIN_QUALITY, q - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        d, det = _encode_still(rgba, s, mid)
        if len(d) <= limit:
            best, lo = (d, det), mid + 1
        else:
            hi = mid - 1
    if best:
        return best[0], best[1] + f" (lowered to fit {s.max_kb} KB)", True
    d, det = _encode_still(rgba, s, MIN_QUALITY)
    return d, det + f" (still over {s.max_kb} KB at the lowest quality)", False


# ----------------------------------------------------------------- animations / pages
def encode_multi(frames: list[np.ndarray], s: Settings) -> tuple[bytes, str]:
    f = FORMATS[s.fmt]
    keep = s.transparent and f.alpha and any(has_alpha(fr) for fr in frames)
    dur = max(1, round(1000.0 / max(0.01, s.fps)))
    buf = io.BytesIO()
    if s.fmt == "gif (animated)":
        if keep:
            imgs = [to_pil(fr, True, s.matte) for fr in frames]
        else:
            colors = max(2, min(256, round(256 * s.quality / 100))) if s.quality < 100 else 256
            imgs = [to_pil(fr, False, s.matte).quantize(colors, Image.Quantize.MEDIANCUT,
                                                         dither=Image.Dither.FLOYDSTEINBERG) for fr in frames]
        kw = {} if s.loop == 1 else {"loop": 0 if s.loop == 0 else s.loop - 1}
        imgs[0].save(buf, "GIF", save_all=True, append_images=imgs[1:], duration=dur, disposal=2, **kw)
        return buf.getvalue(), f"{s.fps:g} fps, {'1-bit transparency' if keep else '256 colours'}"
    if s.fmt == "apng (animated png)":
        imgs = [to_pil(fr, keep, s.matte) for fr in frames]
        from PIL.PngImagePlugin import PngInfo

        info = PngInfo()
        for k, v in s.meta.items():
            info.add_text(k, v)
        imgs[0].save(buf, "PNG", save_all=True, append_images=imgs[1:], duration=dur, loop=s.loop,
                     compress_level=s.compression, pnginfo=info, default_image=False)
        return buf.getvalue(), f"{s.fps:g} fps, zlib {s.compression}"
    if s.fmt == "webp (animated)":
        imgs = [to_pil(fr, keep, s.matte) for fr in frames]
        exif = _webp_exif(s.meta)
        imgs[0].save(buf, "WEBP", save_all=True, append_images=imgs[1:], duration=dur, loop=s.loop,
                     lossless=s.lossless, quality=100 if s.lossless else s.quality,
                     method=round(s.effort / 100 * 6), **({"exif": exif} if exif else {}))
        return buf.getvalue(), f"{s.fps:g} fps, " + ("lossless" if s.lossless else f"quality {s.quality}")
    if s.fmt == "pdf (pages)":
        imgs = [to_pil(fr, False, s.matte) for fr in frames]
        imgs[0].save(buf, "PDF", save_all=True, append_images=imgs[1:], resolution=s.dpi or 72.0,
                     quality=s.quality)
        return buf.getvalue(), f"{len(imgs)} page(s) at {s.dpi or 72:g} dpi"
    raise ValueError(f"{s.fmt} is not an animation format")


# ----------------------------------------------------------------- video
def ffmpeg_path() -> str:
    for env in ("CRATEUTILS_COMFY_FFMPEG", "CRATENODE_FFMPEG"):
        p = os.environ.get(env)
        if p and Path(p).is_file():
            return p
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    raise RuntimeError("ffmpeg not found: install it on PATH or set CRATEUTILS_COMFY_FFMPEG to the ffmpeg executable")


def crf(quality: int, lo: int, hi: int) -> int:
    """quality 1..100 (100 best) onto a CRF range (lo best .. hi worst)."""
    q = max(1, min(100, int(quality)))
    return round(hi - (hi - lo) * (q - 1) / 99)


def _ffmeta_escape(v: str) -> str:
    for ch in ("\\", "=", ";", "#", "\n"):
        v = v.replace(ch, "\\" + ch)
    return v


def video_args(s: Settings, alpha: bool) -> tuple[list[str], str]:
    preset = X26X_PRESETS[round(s.effort / 100 * (len(X26X_PRESETS) - 1))]
    even = ["-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2"]
    if s.fmt == "mp4 (H.264)":
        if s.lossless:
            return ["-c:v", "libx264rgb", "-crf", "0", "-preset", preset, "-pix_fmt", "rgb24"], "lossless RGB"
        pix = "yuv444p" if s.subsampling == "4:4:4" else "yuv422p" if s.subsampling == "4:2:2" else "yuv420p"
        c = crf(s.quality, 12, 40)
        return ["-c:v", "libx264", "-crf", str(c), "-preset", preset, "-pix_fmt", pix] + even, f"CRF {c}, {pix}"
    if s.fmt == "mp4 (H.265 / HEVC)":
        pix = "yuv444p" if s.subsampling == "4:4:4" else "yuv420p"
        base = ["-c:v", "libx265", "-preset", preset, "-tag:v", "hvc1", "-pix_fmt", pix] + even
        if s.lossless:
            return base + ["-x265-params", "lossless=1:log-level=error"], "lossless"
        c = crf(s.quality, 14, 40)
        return base + ["-crf", str(c), "-x265-params", "log-level=error"], f"CRF {c}, {pix}"
    if s.fmt == "webm (VP9, alpha)":
        pix = "yuva420p" if alpha else "yuv420p"
        base = ["-c:v", "libvpx-vp9", "-pix_fmt", pix, "-row-mt", "1", "-deadline", "good",
                "-cpu-used", str(round(5 - s.effort / 25))] + even
        if s.lossless:
            return base + ["-lossless", "1"], f"lossless{', alpha' if alpha else ''}"
        c = crf(s.quality, 15, 50)
        return base + ["-crf", str(c), "-b:v", "0"], f"CRF {c}{', alpha' if alpha else ''}"
    if s.fmt == "mov (ProRes 4444, alpha)":
        xq = s.quality >= 95
        qs = max(1, round(1 + (100 - s.quality) * 0.12))
        pix = "yuva444p10le" if alpha else "yuv444p10le"
        return (["-c:v", "prores_ks", "-profile:v", "5" if xq else "4", "-vendor", "apl0", "-qscale:v", str(qs),
                 "-pix_fmt", pix], f"ProRes 4444{' XQ' if xq else ''}, q {qs}{', alpha' if alpha else ''}")
    if s.fmt == "mkv (FFV1, lossless)":
        return (["-c:v", "ffv1", "-level", "3", "-g", "1", "-slicecrc", "1", "-pix_fmt", "bgra" if alpha else "bgr0"],
                "lossless")
    raise ValueError(f"{s.fmt} is not a video format")


AUDIO_CODEC = {".mp4": ["-c:a", "aac", "-b:a", "192k"], ".mov": ["-c:a", "pcm_s16le"],
               ".webm": ["-c:a", "libopus", "-b:a", "160k"], ".mkv": ["-c:a", "flac"]}


def write_wav(path: str, waveform: np.ndarray, sample_rate: int) -> None:
    """waveform: channels x samples, float -1..1."""
    w = np.clip(np.asarray(waveform, np.float32), -1, 1)
    if w.ndim == 1:
        w = w[None]
    pcm = (w.T * 32767).astype("<i2")
    with wave.open(path, "wb") as f:
        f.setnchannels(w.shape[0])
        f.setsampwidth(2)
        f.setframerate(int(sample_rate))
        f.writeframes(pcm.tobytes())


def write_video(frames: list[np.ndarray], s: Settings, path: str, audio: tuple | None = None,
                ffmpeg: str | None = None) -> str:
    """Encode frames with ffmpeg (piped as raw RGBA). audio = (waveform C x S, sample_rate)."""
    f = FORMATS[s.fmt]
    alpha = s.transparent and f.alpha and any(has_alpha(fr) for fr in frames)
    h, w = frames[0].shape[:2]
    args, detail = video_args(s, alpha)
    ff = ffmpeg or ffmpeg_path()
    with tempfile.TemporaryDirectory(prefix="crateutils_comfy_") as tmp:
        cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgba",
               "-s", f"{w}x{h}", "-r", f"{s.fps:g}", "-i", "-"]
        n_in = 1
        maps = ["-map", "0:v"]
        if audio is not None:
            wav = os.path.join(tmp, "audio.wav")
            write_wav(wav, audio[0], audio[1])
            cmd += ["-i", wav]
            maps += ["-map", f"{n_in}:a"]
            n_in += 1
        if s.meta:
            meta = os.path.join(tmp, "meta.txt")
            with open(meta, "w", encoding="utf-8") as fh:
                fh.write(";FFMETADATA1\n")
                for k, v in s.meta.items():
                    fh.write(f"{_ffmeta_escape(k)}={_ffmeta_escape(v)}\n")
            cmd += ["-f", "ffmetadata", "-i", meta]
            maps += ["-map_metadata", str(n_in)]
            n_in += 1
        cmd += maps + args
        if audio is not None:
            cmd += AUDIO_CODEC.get(f.ext, [])
        if f.ext in (".mp4", ".mov"):
            cmd += ["-movflags", "+faststart+use_metadata_tags"]
        cmd += [path]
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            for fr in frames:
                px = fr if alpha else flatten(fr, s.matte)
                p.stdin.write(np.round(np.clip(px, 0, 1) * 255).astype(np.uint8).tobytes())
            p.stdin.close()
        except BrokenPipeError:
            pass
        err = p.stderr.read().decode("utf-8", "replace")
        if p.wait() != 0:
            raise RuntimeError(f"ffmpeg failed for {s.fmt}: {err.strip()[-800:]}")
    return f"{detail}, {s.fps:g} fps" + (", with audio" if audio is not None else "")


# ----------------------------------------------------------------- audio
def audio_args(s: Settings) -> tuple[list[str], str]:
    q = max(1, min(100, int(s.quality)))
    best = s.lossless  # "lossless" on a lossy codec: its best setting
    if s.fmt == "wav (audio)":
        codec = {"24": "pcm_s24le", "32 float": "pcm_f32le"}.get(s.bit_depth, "pcm_s16le")
        return ["-c:a", codec], f"PCM {s.bit_depth if s.bit_depth != '8' else '16'}-bit"
    if s.fmt == "flac (audio, lossless)":
        lvl = max(0, min(12, s.compression))
        if s.bit_depth == "24":
            return ["-c:a", "flac", "-compression_level", str(lvl), "-sample_fmt", "s32",
                    "-bits_per_raw_sample", "24"], f"lossless 24-bit, level {lvl}"
        return ["-c:a", "flac", "-compression_level", str(lvl), "-sample_fmt", "s16"], f"lossless 16-bit, level {lvl}"
    if s.fmt == "mp3 (audio)":
        if best:
            return ["-c:a", "libmp3lame", "-b:a", "320k"], "320 kbps CBR"
        v = max(0, min(9, round((100 - q) / 11)))
        return ["-c:a", "libmp3lame", "-q:a", str(v)], f"VBR V{v}"
    if s.fmt == "opus (audio, .ogg)":
        kb = 256 if best else round(24 + q * 2.32)
        return ["-c:a", "libopus", "-b:a", f"{kb}k"], f"{kb} kbps"
    if s.fmt == "m4a (audio, AAC)":
        kb = 320 if best else round(64 + q * 2.56)
        return ["-c:a", "aac", "-b:a", f"{kb}k", "-movflags", "+faststart"], f"{kb} kbps"
    raise ValueError(f"{s.fmt} is not an audio format")


def write_audio(waveform: np.ndarray, sample_rate: int, s: Settings, path: str, ffmpeg: str | None = None) -> str:
    """waveform: channels x samples, float -1..1. Encoded by ffmpeg (16-bit WAV works without it)."""
    w = np.clip(np.asarray(waveform, np.float32), -1, 1)
    if w.ndim == 1:
        w = w[None]
    if w.shape[-1] == 0:
        raise ValueError("the audio is empty")
    args, detail = audio_args(s)
    try:
        ff = ffmpeg or ffmpeg_path()
    except RuntimeError:
        if s.fmt == "wav (audio)" and args[1] == "pcm_s16le":
            write_wav(path, w, sample_rate)
            return detail
        raise
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-f", "f32le", "-ar", str(int(sample_rate)),
           "-ac", str(w.shape[0]), "-i", "-"] + args + [path]
    p = subprocess.run(cmd, input=np.ascontiguousarray(w.T).astype("<f4").tobytes(), capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed for {s.fmt}: {p.stderr.decode('utf-8', 'replace').strip()[-600:]}")
    secs = w.shape[1] / float(sample_rate)
    return f"{detail}, {w.shape[0]} ch, {int(sample_rate)} Hz, {secs:.1f} s"


def save_audio(waveforms: list[np.ndarray], sample_rate: int, s: Settings,
               path_for: Callable[[int, str], str]) -> list[Result]:
    """One file per waveform (channels x samples)."""
    f = FORMATS.get(s.fmt)
    if f is None or f.kind != "audio":
        raise ValueError(f"{s.fmt} is not an audio format")
    if s.bit_depth not in f.depths:
        s = replace(s, bit_depth=f.depths[0])
    out = []
    for i, wf in enumerate(waveforms):
        p = path_for(i, f.ext)
        detail = write_audio(wf, sample_rate, s, p)
        out.append(Result(p, os.path.getsize(p), 0, 0, 1, detail))
    return out


# ----------------------------------------------------------------- one call for everything
def save(frames: list[np.ndarray], s: Settings, path_for: Callable[[int, str], str],
         audio: tuple | None = None) -> list[Result]:
    """Write ``frames`` in ``s.fmt``. Still formats write one file per frame (path_for(i, ext));
    animation, page and video formats write one file for all frames (path_for(0, ext))."""
    if not frames:
        raise ValueError("nothing to export")
    f = FORMATS.get(s.fmt)
    if f is None:
        raise ValueError(f"unknown format {s.fmt!r}")
    if f.kind == "audio":
        raise ValueError(f"{s.fmt} is an audio format; use save_audio")
    if s.bit_depth not in f.depths:
        s = replace(s, bit_depth=f.depths[0])
    h, w = frames[0].shape[:2]
    if f.kind != "still" and any(fr.shape[:2] != (h, w) for fr in frames):
        raise ValueError(f"{s.fmt} needs every frame the same size")
    out: list[Result] = []
    if f.kind == "still":
        for i, fr in enumerate(frames):
            data, detail, _ = encode_still(fr, s)
            p = path_for(i, f.ext)
            Path(p).write_bytes(data)
            out.append(Result(p, len(data), fr.shape[1], fr.shape[0], 1, detail))
        return out
    p = path_for(0, f.ext)
    if f.kind == "multi":
        data, detail = encode_multi(frames, s)
        Path(p).write_bytes(data)
        return [Result(p, len(data), w, h, len(frames), detail)]
    detail = write_video(frames, s, p, audio)
    return [Result(p, os.path.getsize(p), w, h, len(frames), detail)]
