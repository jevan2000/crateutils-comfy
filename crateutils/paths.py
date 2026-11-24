"""Which folders a workflow may read, watch or write.

A workflow is a file anyone can share, so its paths are checked: by default only
ComfyUI's input, output, temp and user folders are allowed. More folders can be
added in ``<ComfyUI user folder>/crateutils/config.json``::

    {"allowed_folders": ["D:/Renders", "S:/Shared/Inbox"]}

or with the environment variable ``CRATEUTILS_ALLOWED_FOLDERS`` (separated by ``;``
on Windows, ``:`` elsewhere).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ENV = "CRATEUTILS_ALLOWED_FOLDERS"


def _norm(p: str | os.PathLike) -> Path:
    return Path(os.path.realpath(os.path.abspath(os.path.expandvars(os.path.expanduser(str(p))))))


def config_roots(config_file: str | os.PathLike | None) -> list[Path]:
    """Folders from the config file and the environment variable."""
    out: list[Path] = []
    if config_file and Path(config_file).is_file():
        try:
            data = json.loads(Path(config_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ValueError(f"CrateUtils config {config_file} is not valid JSON: {e}") from e
        for p in data.get("allowed_folders", []) or []:
            if str(p).strip():
                out.append(_norm(p))
    for p in os.environ.get(ENV, "").split(os.pathsep):
        if p.strip():
            out.append(_norm(p.strip()))
    return out


def inside(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(str(path)), os.path.normcase(str(root))]) == os.path.normcase(str(root))
    except ValueError:  # different drives on Windows
        return False


def check(path: str, roots: list[Path], base: Path | None = None, what: str = "path") -> Path:
    """Resolve ``path`` (relative paths are taken from ``base``) and make sure it is inside
    one of ``roots``. Returns the resolved path or raises ValueError saying how to allow it."""
    raw = (path or "").strip().strip('"').strip("'")
    if not raw:
        raise ValueError(f"the {what} is empty")
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not p.is_absolute() and base is not None:
        p = base / p
    p = _norm(p)
    for r in roots:
        if inside(p, _norm(r)):
            return p
    allowed = "\n  ".join(str(r) for r in roots) or "(none)"
    raise ValueError(
        f"{what} {p} is outside the folders CrateUtils may use:\n  {allowed}\n"
        f"Add its folder to \"allowed_folders\" in the CrateUtils config.json (in ComfyUI's user folder, "
        f"under crateutils/) or to the {ENV} environment variable, then restart ComfyUI.")
