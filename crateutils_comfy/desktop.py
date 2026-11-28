"""Things that only work on the machine running ComfyUI: the native file dialog, and
letting the user allow the folder they just picked from.

The browser cannot tell a page the path of a file it chooses (only the bytes), and the
SVG node needs a path so a save in Inkscape repaints it. So the Open dialog is drawn by
Windows itself, in the ComfyUI process's own desktop session, and the path comes back
through this module. Notify already uses PowerShell on Windows, so this adds no new
dependency.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000
PICK_TIMEOUT = 120.0  # a dialog left open blocks one request; this closes it

KINDS = {
    "svg": ("Choose an SVG for CrateUtils-Comfy", "SVG files (*.svg)|*.svg|All files (*.*)|*.*"),
}

# One dialog at a time: two WinForms dialogs from two requests would fight for focus.
_busy = threading.Lock()

_PS = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.StartPosition = 'Manual'
$owner.Location = New-Object System.Drawing.Point(-32000, -32000)
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.Opacity = 0
$owner.Show()
$dlg = New-Object System.Windows.Forms.OpenFileDialog
$dlg.Title = $env:CUC_PICK_TITLE
$dlg.Filter = $env:CUC_PICK_FILTER
$dlg.CheckFileExists = $true
$dlg.Multiselect = $false
if ($env:CUC_PICK_START -and (Test-Path -LiteralPath $env:CUC_PICK_START)) {
    $dlg.InitialDirectory = $env:CUC_PICK_START
}
$result = $dlg.ShowDialog($owner)
$owner.Close()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
    # Written as a file, not to stdout: console encoding mangles non-ASCII paths.
    [System.IO.File]::WriteAllText($env:CUC_PICK_RESULT, $dlg.FileName,
                                  (New-Object System.Text.UTF8Encoding($false)))
}
"""


def available() -> bool:
    return sys.platform == "win32"


def start_dir(start_at: str) -> str:
    """The folder the dialog should open in: the current file's folder, or that folder itself."""
    raw = (start_at or "").strip().strip('"')
    if not raw:
        return ""
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if p.is_file():
        return str(p.parent)
    return str(p) if p.is_dir() else ""


def pick_file(kind: str = "svg", start_at: str = "", timeout: float = PICK_TIMEOUT) -> str | None:
    """Open the Windows file dialog and return the chosen path, or None if cancelled.

    Raises ValueError for anything the caller should show as a message, RuntimeError when
    another dialog is already open, and TimeoutError when the dialog was left open too long.
    """
    if not available():
        raise ValueError("picking a file needs the Windows dialog on the machine running ComfyUI; "
                         "type the path instead")
    title, filt = KINDS.get(kind, KINDS["svg"])
    if not _busy.acquire(blocking=False):
        raise RuntimeError("a file dialog is already open")
    fd, result_name = tempfile.mkstemp(prefix="cuc-pick-", suffix=".txt")
    os.close(fd)
    try:
        os.unlink(result_name)  # the dialog writes it only when something was chosen
        env = dict(os.environ)
        env.update(CUC_PICK_TITLE=title, CUC_PICK_FILTER=filt, CUC_PICK_RESULT=result_name,
                   CUC_PICK_START=start_dir(start_at))
        try:
            subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-STA",
                            "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", _PS],
                           env=env, timeout=timeout, capture_output=True, creationflags=CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise TimeoutError("the file dialog was left open too long; closed it") from None
        except OSError as e:
            raise ValueError(f"could not open the file dialog: {e}") from None
        try:
            chosen = Path(Path(result_name).read_text(encoding="utf-8").strip())
        except OSError:
            return None
        if not chosen.is_file():
            return None
        if kind == "svg" and chosen.suffix.lower() != ".svg":
            raise ValueError(f"{chosen.name} is not an .svg file")
        return str(chosen)
    finally:
        try:
            Path(result_name).unlink()
        except OSError:
            pass
        _busy.release()


def allow_folder(folder: str) -> str:
    """Add a folder to ``allowed_folders`` in the CrateUtils-Comfy config. Returns it.

    Read-modify-write, so the ``notify`` names in the same file survive; written without a
    BOM and replaced atomically, so a half-written config can never be left behind.
    """
    from . import comfy_glue

    raw = str(folder or "").strip().strip('"')
    if not raw:
        raise ValueError("no folder given")
    p = Path(os.path.realpath(os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))))
    if not p.is_dir():
        raise ValueError(f"{p} is not a folder")
    f = comfy_glue.user_dir() / "config.json"
    data = comfy_glue.config()
    roots = [str(x) for x in (data.get("allowed_folders") or []) if str(x).strip()]
    if any(os.path.normcase(os.path.realpath(r)) == os.path.normcase(str(p)) for r in roots):
        return str(p)
    roots.append(str(p))
    data["allowed_folders"] = roots
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f".{f.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, f)
    return str(p)
