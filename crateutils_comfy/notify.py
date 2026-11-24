"""Notifications: Discord webhooks, ntfy topics and Windows toasts.

A destination is one of:
  - ``toast``: a Windows notification on the machine running ComfyUI;
  - a name from ``"notify"`` in the CrateUtils-Comfy config.json, e.g.
    ``{"notify": {"phone": "https://ntfy.sh/my-topic", "team": "https://discord.com/api/webhooks/..."}}``
    (keeps webhook URLs, which are secrets, out of workflows you share);
  - a URL typed straight into the node (it is then saved in the workflow).
Several destinations can be given, separated by commas.

No ComfyUI imports.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import urllib.request
import uuid

TIMEOUT = 20
UA = "CrateUtils-Comfy/0.3"


def resolve(destinations: str, named: dict[str, str]) -> list[tuple[str, str]]:
    """[(service, target)] for a comma-separated destination list."""
    out = []
    for d in re.split(r"[,;\n]+", destinations or ""):
        d = d.strip()
        if not d:
            continue
        if d.lower() in ("toast", "windows", "windows toast"):
            out.append(("toast", ""))
            continue
        url = named.get(d, d)
        if url.lower() in ("toast", "windows"):
            out.append(("toast", ""))
        elif not re.match(r"https?://", url, re.I):
            known = ", ".join(sorted(named)) or "none set"
            raise ValueError(f"unknown destination {d!r}: use 'toast', a URL, or a name from the config ({known})")
        elif re.match(r"https://(\w+\.)?(discord|discordapp)\.com/api/webhooks/", url, re.I):
            out.append(("discord", url))
        else:
            out.append(("ntfy", url))
    if not out:
        raise ValueError("no destination: type 'toast', a URL, or a name from the CrateUtils-Comfy config")
    return out


def _post(url: str, data: bytes, headers: dict, method: str = "POST") -> None:
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": UA, **headers})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        r.read()


def send_discord(url: str, title: str, message: str, image: bytes | None = None, image_name: str = "image.png"):
    content = (f"**{title}**\n" if title else "") + message
    payload = {"content": content[:1990], "allowed_mentions": {"parse": []}}
    if image is None:
        _post(url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
        return
    b = uuid.uuid4().hex
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="payload_json"\r\nContent-Type: application/json\r\n\r\n'
             .encode() + json.dumps(payload).encode() + b"\r\n",
             f'--{b}\r\nContent-Disposition: form-data; name="files[0]"; filename="{image_name}"\r\n'
             f"Content-Type: image/png\r\n\r\n".encode() + image + b"\r\n", f"--{b}--\r\n".encode()]
    _post(url, b"".join(parts), {"Content-Type": f"multipart/form-data; boundary={b}"})


def _header_safe(s: str) -> str:
    return s.encode("latin-1", "replace").decode("latin-1").replace("\n", " ")[:250]


def send_ntfy(url: str, title: str, message: str, image: bytes | None = None, image_name: str = "image.png",
              priority: str = "default"):
    headers = {"Title": _header_safe(title), "Priority": priority}
    if image is None:
        _post(url, message.encode("utf-8"), headers)
    else:  # the attachment is the body; the text goes in a header
        _post(url, image, {**headers, "Filename": image_name, "Message": _header_safe(message)}, method="PUT")


_TOAST_PS = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml([IO.File]::ReadAllText($args[0], [Text.Encoding]::UTF8))
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show(
  (New-Object Windows.UI.Notifications.ToastNotification $xml))
"""


def _xml(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def send_toast(title: str, message: str, image_path: str | None = None):
    if os.name != "nt":
        raise RuntimeError("toast notifications are Windows only")
    img = f'<image placement="hero" src="{_xml(image_path)}"/>' if image_path else ""
    xml = (f'<toast><visual><binding template="ToastGeneric"><text>{_xml(title or "ComfyUI")}</text>'
           f"<text>{_xml(message[:500])}</text>{img}</binding></visual></toast>")
    with tempfile.TemporaryDirectory(prefix="crateutils_comfy_toast_") as d:
        xf, sf = os.path.join(d, "toast.xml"), os.path.join(d, "toast.ps1")
        with open(xf, "w", encoding="utf-8") as f:
            f.write(xml)
        with open(sf, "w", encoding="utf-8-sig") as f:
            f.write(_TOAST_PS)
        p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-File", sf, xf], capture_output=True, text=True, timeout=TIMEOUT,
                           creationflags=0x08000000)  # CREATE_NO_WINDOW
        if p.returncode != 0:
            raise RuntimeError(f"toast failed: {(p.stderr or p.stdout).strip()[-300:]}")


def send(dests: list[tuple[str, str]], title: str, message: str, image: bytes | None = None,
         image_path: str | None = None, priority: str = "default") -> list[str]:
    """Send to every destination; returns one result line per destination (never raises)."""
    out = []
    for service, target in dests:
        try:
            if service == "toast":
                send_toast(title, message, image_path)
            elif service == "discord":
                send_discord(target, title, message, image)
            else:
                send_ntfy(target, title, message, image, priority=priority)
            out.append(f"{service}: sent")
        except Exception as e:
            out.append(f"{service}: FAILED ({e})")
    return out


def png_preview(rgb_u8, max_side: int = 1280) -> bytes:
    """A PNG of an H x W x 3 uint8 array, scaled down to max_side for sending."""
    import io

    from PIL import Image

    im = Image.fromarray(rgb_u8)
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()
