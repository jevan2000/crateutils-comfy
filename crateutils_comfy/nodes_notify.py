"""Notify: send a message (and a picture) when the workflow reaches this node, and when it fails."""

from __future__ import annotations

import os
import tempfile
import time

import numpy as np
from comfy_api.latest import IO, UI

from . import notify, triggers
from .nodes_triggers import ROOT

CAT = f"{ROOT}/Notify"
PRIORITIES = ["default", "low", "high", "urgent"]


class CUCNotify(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        return IO.Schema(
            node_id="CUCNotify", display_name="Notify", category=CAT,
            search_aliases=["notify", "discord", "ntfy", "toast", "notification", "alert", "message me"],
            description="Send a notification when the workflow gets here, with the first image if one is "
                        "connected: a Windows toast on this PC, a Discord channel (webhook) or ntfy (phone app). "
                        "With on_failure it also tells you when the workflow fails. Keep webhook URLs out of "
                        "shared workflows by naming them in the CrateUtils-Comfy config: "
                        '{"notify": {"phone": "https://ntfy.sh/...", "team": "https://discord.com/api/webhooks/..."}} '
                        "and typing the name here.",
            is_output_node=True,
            inputs=[IO.String.Input("destination", default="toast",
                                    tooltip="toast, a name from the CrateUtils-Comfy config, or a Discord webhook / ntfy "
                                            "URL; several separated by commas"),
                    IO.String.Input("title", default="ComfyUI"),
                    IO.String.Input("message", default="Finished {trigger} run {run} {file}", multiline=True,
                                    tooltip="{time}, {trigger}, {run}, {file}, {event} are filled in"),
                    IO.Image.Input("images", optional=True, tooltip="The first image is attached"),
                    IO.AnyType.Input("after", optional=True,
                                     tooltip="Anything: notify only after it is computed (e.g. Export File's paths)"),
                    IO.Boolean.Input("on_failure", default=True,
                                     tooltip="Also notify the same destination if the workflow fails"),
                    IO.String.Input("failure_message", default="The workflow failed", advanced=True),
                    IO.Combo.Input("priority", options=PRIORITIES, default="default", advanced=True,
                                   tooltip="ntfy priority"),
                    IO.Boolean.Input("fail_if_not_sent", default=False, advanced=True,
                                     tooltip="Stop the workflow with an error if a notification can't be sent")],
            outputs=[IO.String.Output(display_name="report")],
            hidden=[IO.Hidden.prompt],
        )

    @classmethod
    def fingerprint_inputs(cls, **kw):
        return time.time_ns()  # always send

    @classmethod
    def execute(cls, destination, title, message, on_failure, failure_message="", priority="default",
                fail_if_not_sent=False, images=None, after=None):
        from . import comfy_glue

        run = triggers.find_run(cls.hidden.prompt)
        dests = notify.resolve(destination, comfy_glue.named_destinations())
        text = notify.fill(message, run)
        png, path, tmpdir = None, None, None
        if images is not None and len(images):
            arr = np.round(np.clip(images[0].detach().cpu().float().numpy()[..., :3], 0, 1) * 255).astype(np.uint8)
            png = notify.png_preview(arr)
            if any(s == "toast" for s, _ in dests):
                tmpdir = tempfile.mkdtemp(prefix="crateutils_comfy_notify_")
                path = os.path.join(tmpdir, "preview.png")
                with open(path, "wb") as f:
                    f.write(png)
        try:
            lines = notify.send(dests, notify.fill(title, run), text, png, path, priority)
        finally:
            if tmpdir:
                # Windows reads the toast picture after the call returns; leave it a moment, then tidy up
                import shutil
                import threading

                threading.Timer(30, shutil.rmtree, args=(tmpdir, True)).start()
        report = "\n".join(lines)
        if fail_if_not_sent and "FAILED" in report:
            raise RuntimeError(report)
        return IO.NodeOutput(report, ui=UI.PreviewText(report))


NODES = [CUCNotify]
