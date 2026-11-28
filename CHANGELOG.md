# Changelog

## 0.4.1 (2026-11-27)

- SVG Page: the **Browse for .svg…** button now keeps its slot in `widgets_values` on every
  frontend version. Frontend 1.51 writes a `null` placeholder for a widget marked
  `serialize = false` while 1.55 omits it, so a workflow saved on one instance shifted
  `page` / `page_label` / `width` / `height` when opened on the other. The button still stays
  out of the API prompt (`options.serialize = false`), so ComfyUI never sees it.
- Checked on both installed instances (0.42.5 with frontend 1.55.18, and 0.34.0 with 1.51.9):
  identical `widgets_values`, prompt keys unchanged, save/reload restores all six inputs and
  the preview, and the 7-fixture sweep is 7/7 pixel-identical on both.
- Also installs on :8191 for the first time (clone of tag `v0.4.0` + this fix).

## 0.4.0 (2026-11-27)

- New node **SVG Page (Inkscape)**: draws one page of an Inkscape `.svg` in the browser,
  previews it on the node, and outputs `image` plus `mask` (1 = transparent, like Load Image).
  Page by number or by Inkscape page label, optional width/height, and a transparent, white or
  page-colour background. Saving in Inkscape repaints the node.
- The browser is the only renderer, so the preview and the output are the same pixels: a run
  queued from the ComfyUI tab draws the page before the node executes, and a run with no
  browser open uses the last render of that exact file, page and size, stopping with a clear
  message when the file has changed. The node also warns about fonts that are not installed
  (measured, not asked of `document.fonts.check()`) and about `device-cmyk()` spot colours.
- New routes under `/crateutils-comfy/svg/`: `source` (the text the node previews), `pending`
  (pages a queued run is waiting for) and `render` (the PNG the browser drew). They serve only
  files the allowed-folders policy permits and write only into `temp/crateutils-svg`, pruned
  to 300 pages or 30 days.
- SVG Page: **Browse for .svg…** opens the Windows Open dialog on the machine running ComfyUI and
  fills `svg_path` with the real path (a browser file picker cannot supply one). Picking from a
  folder that is not allowed yet offers to add that folder to `allowed_folders` once, leaving the
  rest of `config.json` alone. New routes `/crateutils-comfy/svg/pick` and `/svg/allow`.
- 28 new tests (`tests/test_svg.py`, `tests/test_desktop.py`), 92 in total.

## 0.3.2 (2026-11-24)

Fixes from the 2026-11-24 review (64 tests, up from 55):

- Export File: quality presets now reset `max_kb` too, so "max quality" / "balanced" / "small"
  produce a deterministic result after a custom size limit. `%batch_num%` no longer restarts
  the counter at 1 on a second batch (which could overwrite the first); it must be in the
  file name, not a folder name.
- Notify: templates fill `{time}` / `{trigger}` / `{run}` / `{file}` / `{event}` in one pass,
  so a value that itself contains a token is left alone. Failure alerts now also work when
  `destination` is linked, and every Notify node with `on_failure` gets an alert (not just
  the first).
- Triggers: **Fire now** runs immediately even while the trigger is paused. The CSV run log
  escapes cells starting with `=`, `+`, `-` or `@` so a title or path can't be run as a
  spreadsheet formula.
- Copy / Move Files: unknown `mode` / `if_exists` values are refused instead of silently
  overwriting.
- Trigger Manager panel: only rebuilds when the data changed, and keeps its scroll position
  when it does (it used to rebuild every 3 s).
- Docs: `paths.py` no longer claims ComfyUI's user folder is allowed by default.
- The animation `loop` semantics are now tested: GIF stores n-1 (omitted for once), APNG and
  WebP store n (0 = forever), per RFC 9649; no code change was needed.

## 0.3.1 (2026-11-24)

- Triggered runs now call before-queue hooks that other packs register in
  `sys.modules["crate_before_queue"].hooks` (`fn(prompt, rng) -> int`), on every run
  whatever `new_seeds` is set to. A failing hook is logged and skipped. CrateNode-Comfy
  uses it so Palette Generator (`new_each_run`) and Fluid Gradient (`randomize`) make new
  colours and looks on triggered runs, as they do when run from the browser.
- Housekeeping: CI uses the current GitHub Actions; the tests and the video writer close their
  files and pipes, so nothing prints ResourceWarnings any more.

## 0.3.0 (2026-11-23)

Every internal name now matches CrateUtils-Comfy as well (breaking, before any real use):

- Node IDs `CU*` → `CUC*` (e.g. `CUCScheduler`, `CUCExport`).
- Routes `/crateutils/...` → `/crateutils-comfy/...` (webhook URLs too).
- Settings, armed triggers and run log: `user/crateutils/` → `user/crateutils-comfy/`.
- Webhook header `X-CrateUtils-Token` → `X-CrateUtils-Comfy-Token`.
- Environment variables `CRATEUTILS_ALLOWED_FOLDERS` / `CRATEUTILS_FFMPEG` →
  `CRATEUTILS_COMFY_ALLOWED_FOLDERS` / `CRATEUTILS_COMFY_FFMPEG`.
- Python package `crateutils` → `crateutils_comfy`; `web/crateutils.js` → `web/crateutils_comfy.js`.
- Export File's default prefix `CrateUtils/export` → `CrateUtils-Comfy/export`.

## 0.2.1 (2026-11-23)

- Renamed the pack to CrateUtils-Comfy: GitHub repo `jevan2000/crateutils-comfy` (the old
  `crate-utilities` URL redirects), node menu category "CrateUtils-Comfy", Comfy Registry
  display name. Node IDs, routes (`/crateutils/...`), the `user/crateutils` folder, the
  `X-CrateUtils-Token` header and `CRATEUTILS_*` variables are unchanged, so saved workflows,
  armed triggers, webhook URLs and configs keep working.

## 0.2.0 (2026-11-23)

- Triggers side panel: every armed trigger with Pause / Resume, Fire now, Disarm, Show node
  and its last 20 runs; Pause for every trigger kind.
- Run tracking: each triggered run is followed to the end; status, time taken and output files
  go to the panel and to a CSV run log (`user/crateutils/runs.csv`, downloadable).
- Webhook Trigger (secret-token URL; body and query come out as JSON) and JSON Field.
- Folder Batch: one run per existing file, one at a time and pausable.
- Copy / Move Files, with an `after` input for ordering.
- Notify: Windows toast, Discord or ntfy with the picture, on finish and on failure;
  destinations can be named in the config.
- Export File: audio formats WAV (16/24/32f), FLAC (16/24), MP3, Opus, AAC; the image input
  is now optional.

## 0.1.0 (2026-11-23)

First version.

- Task Scheduler, Folder Watcher and File Watcher nodes with Arm / Disarm / Fire now buttons;
  armed triggers run in the background and survive a restart.
- Load Image (Path).
- Export File: PNG 8/16-bit, JPEG, WebP, AVIF, TIFF 8/16/32-bit, JPEG 2000, EXR, BMP, TGA, ICO,
  animated GIF / WebP / APNG, multi-page PDF, MP4 H.264/H.265, WebM VP9, MOV ProRes 4444,
  MKV FFV1; quality presets and a file size limit.
