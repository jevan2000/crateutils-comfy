"""SVG Page: one artboard of an Inkscape SVG, drawn by the browser, as IMAGE and MASK.

The browser is the renderer and the node waits for it (see ``svgcache``): a workflow
queued from the ComfyUI tab draws the page before the node reads it. That is the only
way to match Inkscape's own rendering, because gradients, filters, masks and text all
come from the same engine that shows the preview.
"""

from __future__ import annotations

import os

from comfy_api.latest import IO

from . import svgcache
from .nodes_files import _lines, _load
from .nodes_triggers import ROOT

CAT = f"{ROOT}/Files"
RENDER_TIMEOUT = 20.0

NO_BROWSER = ("The browser has not drawn this SVG yet. This node renders SVG pages in the browser, so the "
              "preview and the output are the same picture: open this workflow in ComfyUI, wait for the node "
              "to show the page, and run again. A run queued from the browser always draws it first; a run "
              "with no browser open (a watcher, a schedule, or the API) uses the last picture drawn for this "
              "exact file, page and size, and stops here when the file has changed since.")


class CUCSvgPage(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        return IO.Schema(
            node_id="CUCSvgPage", display_name="SVG Page (Inkscape)", category=CAT,
            search_aliases=["svg", "inkscape", "artboard", "page", "vector", "logo"],
            description="Render one page of an Inkscape .svg in the browser and output it as an image. The "
                        "node body previews the page, and a save in Inkscape repaints it. Pages come from the "
                        "document's Inkscape pages, so pick one by number or by its label. The IMAGE is RGB: "
                        "transparent areas are black, and transparency is on the mask instead (1 = "
                        "transparent), like Load Image.",
            inputs=[
                IO.String.Input("svg_path", default="", multiline=True,
                                tooltip="The .svg file; relative paths are inside ComfyUI's input folder. Paths "
                                        "outside the input, output and temp folders need adding to the "
                                        "CrateUtils-Comfy config (see README)"),
                IO.Int.Input("page", default=1, min=1, max=1000,
                             tooltip="Which Inkscape page, counting from 1. Ignored when page_label matches one"),
                IO.String.Input("page_label", default="",
                                tooltip="Page label from Inkscape's Pages panel, e.g. 'Home Page'. Leave empty "
                                        "to use the page number"),
                IO.Int.Input("width", default=0, min=0, max=16384,
                             tooltip="Output width in pixels; 0 = the page's own width"),
                IO.Int.Input("height", default=0, min=0, max=16384,
                             tooltip="Output height in pixels; 0 = keep the page's proportions"),
                IO.Combo.Input("background", options=svgcache.BACKGROUNDS, default="transparent",
                               tooltip="What to draw behind the artwork: white or the Inkscape page colour make "
                                       "an opaque image; transparent keeps alpha, which arrives on the mask"),
            ],
            outputs=[IO.Image.Output(display_name="image"),
                     IO.Mask.Output(display_name="mask", tooltip="Like Load Image: 1 = transparent"),
                     IO.Int.Output(display_name="width"), IO.Int.Output(display_name="height"),
                     IO.String.Output(display_name="name", tooltip="File name without folder or extension")],
        )

    @classmethod
    def fingerprint_inputs(cls, svg_path="", page=1, page_label="", width=0, height=0,
                           background="transparent", **kw):
        from . import comfy_glue

        sig = []
        for p in _lines(svg_path)[:1]:
            try:
                st = os.stat(comfy_glue.check_read(p, "SVG path"))
                sig.append((p, st.st_size, st.st_mtime_ns))
            except (OSError, ValueError):
                sig.append((p, None))
        return tuple(sig) + (page, page_label, width, height, background)

    @classmethod
    def execute(cls, svg_path, page=1, page_label="", width=0, height=0, background="transparent"):
        import torch

        from . import comfy_glue

        lines = _lines(svg_path)
        if not lines:
            raise ValueError("svg_path is empty (type the path of an Inkscape .svg)")
        if len(lines) > 1:
            raise ValueError("svg_path takes one file: this node renders one page; use one node per file")
        f = comfy_glue.check_read(lines[0], "SVG path")
        if not f.is_file():
            raise ValueError(f"{f} does not exist")
        if f.suffix.lower() != ".svg":
            raise ValueError(f"{f.name} is not an .svg file")
        try:
            size = f.stat().st_size
        except OSError as e:
            raise ValueError(f"cannot read {f}: {e}") from None
        if size > svgcache.MAX_SVG_BYTES:
            raise ValueError(f"{f.name} is {size // (1024 * 1024)} MB, over the "
                             f"{svgcache.MAX_SVG_BYTES // (1024 * 1024)} MB limit")

        key = svgcache.cache_key(svgcache.file_hash(f), page, page_label, width, height, background)
        png = svgcache.cache_path(key)
        if not png.is_file():
            svgcache.request(key, str(f), page, page_label, width, height, background)
            drawn = svgcache.wait(key, RENDER_TIMEOUT)
            if drawn is None:
                raise ValueError(NO_BROWSER)
            png = drawn

        rgb, a = _load(png)
        h, w = rgb.shape[:2]
        return IO.NodeOutput(torch.from_numpy(rgb[None]), torch.from_numpy((1.0 - a)[None]), w, h, f.stem)


NODES = [CUCSvgPage]
