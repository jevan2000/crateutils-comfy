"""File helpers: load an image from a path (what the watchers hand on)."""

from __future__ import annotations

import os

import numpy as np
from comfy_api.latest import IO

from .nodes_triggers import ROOT

CAT = f"{ROOT}/Files"


def _lines(text: str) -> list[str]:
    return [ln.strip().strip('"') for ln in (text or "").splitlines() if ln.strip()]


def _load(path) -> tuple[np.ndarray, np.ndarray]:
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode == "I;16" or im.mode == "I":
            arr = np.asarray(im, np.float32) / (65535.0 if im.mode == "I;16" else max(1.0, float(np.max(im))))
            rgb = np.repeat(arr[..., None], 3, axis=2)
            a = np.ones(arr.shape, np.float32)
        else:
            rgba = np.asarray(im.convert("RGBA"), np.float32) / 255.0
            rgb, a = rgba[..., :3], rgba[..., 3]
    return rgb, a


class CULoadImagePath(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        return IO.Schema(
            node_id="CULoadImagePath", display_name="Load Image (Path)", category=CAT,
            search_aliases=["load image from path", "image from file", "load path"],
            description="Load an image from a file path, e.g. the path a Folder or File Watcher outputs. Several "
                        "paths (one per line) load as a batch; they must be the same size. Reloads when the file "
                        "changes. Paths outside ComfyUI's input/output folders need adding to the CrateUtils-Comfy config.",
            inputs=[IO.String.Input("path", default="", multiline=True,
                                    tooltip="One path per line; relative paths are inside ComfyUI's input folder")],
            outputs=[IO.Image.Output(display_name="image"),
                     IO.Mask.Output(display_name="mask", tooltip="Like Load Image: 1 = transparent"),
                     IO.Int.Output(display_name="width"), IO.Int.Output(display_name="height"),
                     IO.String.Output(display_name="name", tooltip="File name without folder or extension")],
        )

    @classmethod
    def fingerprint_inputs(cls, path="", **kw):  # a linked path isn't known here; its source decides
        from . import comfy_glue

        sig = []
        for p in _lines(path):
            try:
                st = os.stat(comfy_glue.check_read(p, "image path"))
                sig.append((p, st.st_size, st.st_mtime_ns))
            except (OSError, ValueError):
                sig.append((p, None))
        return tuple(sig)

    @classmethod
    def execute(cls, path):
        import torch

        from . import comfy_glue

        files = [comfy_glue.check_read(p, "image path") for p in _lines(path)]
        if not files:
            raise ValueError("path is empty (connect a watcher's path output or type a file path)")
        imgs, masks = [], []
        for f in files:
            if not f.is_file():
                raise ValueError(f"{f} does not exist")
            rgb, a = _load(f)
            if imgs and rgb.shape != imgs[0].shape:
                raise ValueError(f"{f.name} is {rgb.shape[1]}x{rgb.shape[0]}, the first image is "
                                 f"{imgs[0].shape[1]}x{imgs[0].shape[0]}; a batch must be one size")
            imgs.append(rgb)
            masks.append(1.0 - a)
        h, w = imgs[0].shape[:2]
        return IO.NodeOutput(torch.from_numpy(np.stack(imgs)), torch.from_numpy(np.stack(masks)), w, h,
                             files[0].stem)


from .fileops import IF_EXISTS, copy_or_move  # noqa: E402


class CUCopyFiles(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        return IO.Schema(
            node_id="CUCopyFiles", display_name="Copy / Move Files", category=CAT,
            search_aliases=["copy file", "move file", "send to folder", "done folder", "archive input"],
            description="Copy or move files to another folder: send finished exports to a share, or move a "
                        "watched input file into a 'done' folder once it is processed. Connect 'after' (e.g. to "
                        "Export File's paths) so this runs only after that step has finished. Folders outside "
                        "ComfyUI's input/output need adding to the CrateUtils-Comfy config (see README).",
            is_output_node=True,
            inputs=[IO.String.Input("paths", default="", multiline=True,
                                    tooltip="Files, one per line (e.g. a watcher's path or Export File's paths); "
                                            "relative paths are inside the input folder"),
                    IO.String.Input("destination", default="done",
                                    tooltip="Folder to put them in; relative paths are inside the output folder"),
                    IO.Combo.Input("mode", options=["copy", "move"], default="copy"),
                    IO.Combo.Input("if_exists", options=IF_EXISTS, default=IF_EXISTS[0]),
                    IO.AnyType.Input("after", optional=True,
                                     tooltip="Anything: this node waits until it is computed")],
            outputs=[IO.String.Output(display_name="paths", tooltip="Where the files are now, one per line"),
                     IO.String.Output(display_name="report")],
        )

    @classmethod
    def fingerprint_inputs(cls, **kw):
        import time

        return time.time_ns()  # a file operation: always do it

    @classmethod
    def execute(cls, paths, destination, mode, if_exists, after=None):
        from comfy_api.latest import UI

        from . import comfy_glue

        sources = [comfy_glue.check_read(p, "file to copy") for p in _lines(paths)]
        if not sources:
            raise ValueError("no files given")
        dest = comfy_glue.check_write_folder(destination, "destination folder")
        new, lines = copy_or_move(sources, dest, mode, if_exists)
        report = "\n".join(lines)
        return IO.NodeOutput("\n".join(new), report, ui=UI.PreviewText(report))


NODES = [CULoadImagePath, CUCopyFiles]
