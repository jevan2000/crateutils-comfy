"""Browser-rendered SVG cache and the render requests the SVG node waits on.

The browser is the only renderer (an SVGs Inkscape features -- gradients, filters,
masks, text -- need a real SVG engine). The frontend draws the page to a canvas,
POSTs the PNG to a route, and this module stores it under ComfyUI's temp folder
keyed by file content plus the page and size that were drawn. The node loads that
PNG through the pack's own PNG-to-tensor helper, so the preview and the output are
the same pixels by construction.

The key format is shared with ``web/crateutils_comfy.js``; change one, change the
other. Keeping it a plain string (no JSON, no hashing of parameters) makes the two
sides easy to keep identical during a review.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

MAX_SVG_BYTES = 32 * 1024 * 1024
MAX_PNG_BYTES = 64 * 1024 * 1024
KEEP = 300  # cache entries; the oldest go first once there are more
MAX_AGE = 30 * 24 * 3600  # seconds; a page not drawn for a month is not worth keeping
MAX_PENDING = 16
PENDING_TTL = 120.0  # a request older than this is not answered; the node has given up

BACKGROUNDS = ["transparent", "white", "page colour"]
BACKGROUND_KEY = {"transparent": "transparent", "white": "white", "page colour": "page"}
KEY_RE = re.compile(r"^v1_[0-9a-f]{16}_p\d+_l[a-z0-9-]*_\d+x\d+_(transparent|white|page)$")


def cache_dir() -> Path:
    import folder_paths

    d = Path(folder_paths.get_temp_directory()) / "crateutils-svg"
    d.mkdir(parents=True, exist_ok=True)
    return d


def file_hash(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def label_slug(label: str) -> str:
    """Same rule as ``slugLabel`` in the frontend: "Über uns" -> "ber-uns"."""
    s = re.sub(r"[^a-z0-9]+", "-", (label or "").strip().lower()).strip("-")
    return s[:24] or "x"


def cache_key(digest: str, page: int, page_label: str, width: int, height: int, background: str) -> str:
    bg = BACKGROUND_KEY.get(background, "transparent")
    return f"v1_{digest[:16]}_p{max(1, int(page))}_l{label_slug(page_label)}_{int(width)}x{int(height)}_{bg}"


def cache_path(key: str) -> Path:
    if not KEY_RE.match(key):
        raise ValueError(f"not a CrateUtils-Comfy SVG cache key: {key[:60]}")
    return cache_dir() / f"{key}.png"


def prune(keep: int = KEEP) -> int:
    """Drop the oldest entries. Never touches a file that was just written."""
    d = cache_dir()
    files = []
    for p in d.glob("*.png"):
        try:
            st = p.stat()
        except OSError:
            continue
        files.append((st.st_mtime, p))
    files.sort(reverse=True)
    now = time.time()
    removed = 0
    for i, (mtime, p) in enumerate(files):
        if i < keep and now - mtime < MAX_AGE:
            continue
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def store(key: str, png: bytes) -> Path:
    """Write a rendered page, resolving anyone waiting on it. Raises ValueError on bad input."""
    if not KEY_RE.match(key):
        raise ValueError(f"not a CrateUtils-Comfy SVG cache key: {key[:60]}")
    if not png:
        raise ValueError("the render was empty")
    if len(png) > MAX_PNG_BYTES:
        raise ValueError(f"the render is {len(png) // (1024 * 1024)} MB, over the {MAX_PNG_BYTES // (1024 * 1024)} MB limit")
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("the render is not a PNG")
    path = cache_path(key)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_bytes(png)
    os.replace(tmp, path)
    resolve(key)
    prune()
    return path


@dataclass
class Render:
    key: str
    path: str
    page: int
    page_label: str
    width: int
    height: int
    background: str
    created: float = field(default_factory=time.time)
    done: threading.Event = field(default_factory=threading.Event, repr=False)

    def as_request(self) -> dict:
        return {"key": self.key, "path": self.path, "page": self.page, "page_label": self.page_label,
                "width": self.width, "height": self.height, "background": self.background}


_lock = threading.Lock()
_pending: dict[str, Render] = {}


def request(key: str, path: str, page: int, page_label: str, width: int, height: int,
            background: str) -> Render:
    """Register a page to draw, or return the one already registered for this key."""
    now = time.time()
    with _lock:
        for k in [k for k, r in _pending.items() if now - r.created > PENDING_TTL]:
            _pending.pop(k, None)
        r = _pending.get(key)
        if r is None:
            while len(_pending) >= MAX_PENDING:
                oldest = min(_pending.values(), key=lambda x: x.created)
                _pending.pop(oldest.key, None)
            r = Render(key=key, path=str(path), page=int(page), page_label=str(page_label or ""),
                       width=int(width), height=int(height), background=str(background or "transparent"))
            _pending[key] = r
        return r


def resolve(key: str) -> None:
    with _lock:
        r = _pending.get(key)
    if r is not None:
        r.done.set()


def wait(key: str, timeout: float) -> Path | None:
    """Block until a browser has drawn this page, or the timeout runs out."""
    with _lock:
        r = _pending.get(key)
    if r is None:
        p = cache_path(key)
        return p if p.is_file() else None
    try:
        r.done.wait(timeout)
    finally:
        with _lock:
            _pending.pop(key, None)
    p = cache_path(key)
    return p if p.is_file() else None


def list_pending() -> list[dict]:
    now = time.time()
    with _lock:
        return [r.as_request() for r in _pending.values() if now - r.created <= PENDING_TTL]
