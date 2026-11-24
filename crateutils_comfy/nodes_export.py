"""Export File: images, animations, pages and video in many formats, with quality presets."""

from __future__ import annotations

import json
import os
from dataclasses import replace

import numpy as np
from comfy_api.latest import IO, UI

from . import exporter as ex
from .nodes_triggers import ROOT

CAT = f"{ROOT}/Export"
MASK_MEANINGS = ["alpha (1 = opaque)", "LoadImage (1 = transparent)"]
PRESET_TIP = ("Sets the quality fields for the format: lossless (no loss: PNG/TIFF/WebP/EXR/FFV1, 16-bit where "
              "the format has it), max quality, high, balanced, small. 'custom' uses the fields below as set.")


def _frames(image, mask, meaning) -> list[np.ndarray]:
    img = image.detach().cpu().float().numpy()
    if img.ndim == 3:
        img = img[None]
    b, h, w, c = img.shape
    if c >= 4:
        alpha = img[..., 3]
    else:
        alpha = np.ones((b, h, w), np.float32)
    if mask is not None:
        m = mask.detach().cpu().float().numpy()
        if m.ndim == 2:
            m = m[None]
        if m.shape[-2:] != (h, w):
            raise ValueError(f"mask is {m.shape[-1]}x{m.shape[-2]} but the image is {w}x{h}")
        if meaning == MASK_MEANINGS[1]:
            m = 1.0 - m
        alpha = np.broadcast_to(m if m.shape[0] in (1, b) else m[:1], (b, h, w))
    rgb = img[..., :3] if c >= 3 else np.repeat(img[..., :1], 3, axis=-1)
    return [np.concatenate([rgb[i], alpha[i][..., None]], axis=-1).astype(np.float32) for i in range(b)]


class CUCExport(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        return IO.Schema(
            node_id="CUCExport", display_name="Export File", category=CAT,
            search_aliases=["save as", "export", "save jpeg", "save webp", "save tiff", "save exr", "save mp4",
                            "save gif", "16-bit png", "avif"],
            description="Save images as PNG (8/16-bit), JPEG, WebP, AVIF, TIFF (8/16/32-bit), JPEG 2000, EXR "
                        "(half/float), BMP, TGA, ICO; a batch as an animated GIF / WebP / APNG or a multi-page "
                        "PDF; or as video: MP4 (H.264/H.265), WebM (VP9 with alpha), MOV (ProRes 4444) or MKV "
                        "(FFV1 lossless), with optional audio; or audio alone as WAV, FLAC, MP3, Opus or AAC. Pick a preset or set quality by hand; max_kb "
                        "lowers lossy quality until the file fits.",
            is_output_node=True,
            inputs=[
                IO.Image.Input("image", optional=True,
                               tooltip="An image or a batch (a batch is one file per image, or one animation / "
                                       "video / PDF for those formats). Not needed for the audio formats"),
                IO.Combo.Input("format", options=list(ex.FORMATS), default="png"),
                IO.Combo.Input("preset", options=ex.PRESETS, default="high", tooltip=PRESET_TIP),
                IO.String.Input("filename_prefix", default="CrateUtils-Comfy/export",
                                tooltip="Like Save Image: a/b makes a subfolder; %date:yyyy-MM-dd% and "
                                        "%width%/%height% work"),
                IO.Mask.Input("mask", optional=True, tooltip="Transparency for the image"),
                IO.Combo.Input("mask_meaning", options=MASK_MEANINGS, default=MASK_MEANINGS[0], optional=True,
                               tooltip="Load Image's mask is 1 = transparent; most others are 1 = opaque"),
                IO.Audio.Input("audio", optional=True, tooltip="Sound for video formats, or the audio to save for "
                               "the audio formats (wav, flac, mp3, opus, m4a)"),
                IO.Int.Input("quality", default=92, min=1, max=100, tooltip="custom: JPEG, WebP, AVIF, "
                             "JPEG 2000, video (100 best)"),
                IO.Boolean.Input("lossless", default=False, tooltip="custom: WebP, JPEG 2000 and video codecs"),
                IO.Combo.Input("bit_depth", options=ex.DEPTHS, default="8",
                               tooltip="custom: PNG 8/16, TIFF 8/16/32 float, EXR 16 (half) / 32 float, WAV 16/24/32 "
                                       "float, FLAC 16/24; other formats are 8-bit"),
                IO.Int.Input("compression", default=6, min=0, max=9,
                             tooltip="custom: PNG/APNG zlib level (lossless; higher = smaller, slower); TGA RLE "
                                     "and EXR zip when above 0", advanced=True),
                IO.Int.Input("effort", default=60, min=0, max=100, advanced=True,
                             tooltip="custom: encoder effort, higher = smaller and slower (WebP method, AVIF "
                                     "speed, x264/x265 preset, VP9 cpu-used)"),
                IO.Combo.Input("subsampling", options=ex.SUBSAMPLING, default="auto", advanced=True,
                               tooltip="custom: chroma subsampling for JPEG, AVIF and video. 4:4:4 keeps "
                                       "coloured edges and text crisp; 4:2:0 is smaller"),
                IO.Boolean.Input("progressive", default=True, advanced=True, tooltip="JPEG"),
                IO.Combo.Input("tiff_compression", options=ex.TIFF_COMPRESSION, default="deflate", advanced=True),
                IO.Boolean.Input("transparent", default=True,
                                 tooltip="Keep transparency where the format has it; otherwise flatten onto matte"),
                IO.String.Input("matte", default="#ffffff", advanced=True,
                                tooltip="Colour behind transparent areas when flattening"),
                IO.Int.Input("max_kb", default=0, min=0, max=10_000_000,
                             tooltip="File size limit for lossy still images; 0 = none"),
                IO.Float.Input("fps", default=24.0, min=0.1, max=240.0, step=0.5,
                               tooltip="Animations and video"),
                IO.Int.Input("loop", default=0, min=0, max=1000, advanced=True,
                             tooltip="Animations: 0 = loop forever, n = play n times"),
                IO.Float.Input("dpi", default=0.0, min=0.0, max=2400.0, advanced=True,
                               tooltip="Written to PNG, JPEG, TIFF, BMP, PDF; 0 = none (PDF: 72)"),
                IO.Boolean.Input("embed_workflow", default=True, advanced=True,
                                 tooltip="Put the workflow in PNG, APNG, WebP, AVIF and video files, like Save Image"),
                IO.Boolean.Input("exr_linear", default=True, advanced=True,
                                 tooltip="EXR: convert to linear light (what compositing apps expect)"),
                IO.String.Input("folder", default="", advanced=True,
                                tooltip="Save somewhere other than ComfyUI's output folder (it must be an allowed "
                                        "folder, see README); empty = output folder"),
            ],
            outputs=[IO.String.Output(display_name="paths", tooltip="Saved files, one per line"),
                     IO.String.Output(display_name="report")],
            hidden=[IO.Hidden.prompt, IO.Hidden.extra_pnginfo],
        )

    @classmethod
    def execute(cls, format, preset, filename_prefix, quality, lossless, bit_depth, compression, effort,
                subsampling, progressive, tiff_compression, transparent, matte, max_kb, fps, loop, dpi,
                embed_workflow, exr_linear, folder, image=None, mask=None, mask_meaning=MASK_MEANINGS[0],
                audio=None):
        import folder_paths

        from . import comfy_glue

        fmt = ex.FORMATS[format]
        meta = {}
        if embed_workflow:
            if cls.hidden.prompt is not None:
                meta["prompt"] = json.dumps(cls.hidden.prompt)
            for k, v in (cls.hidden.extra_pnginfo or {}).items():
                meta[k] = json.dumps(v)
        s = ex.Settings(fmt=format, quality=quality, lossless=lossless, compression=compression, effort=effort,
                        bit_depth=bit_depth, subsampling=subsampling, progressive=progressive,
                        tiff_compression=tiff_compression, transparent=transparent, matte=matte, dpi=dpi,
                        max_kb=max_kb, fps=fps, loop=loop, exr_linear=exr_linear, meta=meta)
        s = ex.apply_preset(s, preset)
        if s.bit_depth not in fmt.depths:
            s = replace(s, bit_depth=fmt.depths[0])
        if fmt.kind == "audio":
            if audio is None:
                raise ValueError(f"{format} saves audio: connect an AUDIO input")
            frames, w, h = None, 0, 0
        else:
            if image is None:
                raise ValueError(f"{format} needs an image: connect the image input")
            frames = _frames(image, mask, mask_meaning)
            h, w = frames[0].shape[:2]
        out_root = folder_paths.get_output_directory()
        base_dir = str(comfy_glue.check_write_folder(folder)) if folder.strip() else out_root
        os.makedirs(base_dir, exist_ok=True)
        out_dir, base, counter, subfolder, _ = folder_paths.get_save_image_path(filename_prefix, base_dir, w, h)
        os.makedirs(out_dir, exist_ok=True)

        def path_for(i, ext):
            return os.path.join(out_dir, f"{base.replace('%batch_num%', str(i))}_{counter + i:05}_{ext}")

        aud = None
        if audio is not None and fmt.kind == "video":
            wf = audio["waveform"]
            aud = (wf[0].detach().cpu().float().numpy(), int(audio["sample_rate"]))
        if fmt.kind == "audio":
            wf = audio["waveform"].detach().cpu().float().numpy()
            if wf.ndim == 2:
                wf = wf[None]
            results = ex.save_audio(list(wf), int(audio["sample_rate"]), s, path_for)
        else:
            results = ex.save(frames, s, path_for, aud)
        notes = []
        if preset != "custom":
            notes.append(f"preset {preset}")
        if preset == "lossless" and not fmt.lossless_ok:
            notes.append(f"{format} has no lossless mode: used its highest quality")
        if audio is not None and fmt.kind not in ("video", "audio"):
            notes.append("audio ignored (not a video format)")
        report = "\n".join([r.line() for r in results] + notes)
        ui = None
        in_output = os.path.normcase(os.path.abspath(base_dir)) == os.path.normcase(os.path.abspath(out_root))
        if in_output:
            saved = [UI.SavedResult(os.path.basename(r.path), subfolder, IO.FolderType.output) for r in results]
            if fmt.kind == "audio" and format.startswith(("wav", "flac", "mp3", "opus")):
                ui = UI.SavedAudios(saved)
            elif fmt.kind == "video" and format.startswith(("mp4", "webm")):
                ui = UI.PreviewVideo(saved)
            elif fmt.browser:
                ui = UI.SavedImages(saved, is_animated=fmt.kind == "multi")
        if ui is None:
            ui = UI.PreviewText(report)
        return IO.NodeOutput("\n".join(r.path for r in results), report, ui=ui)


NODES = [CUCExport]
