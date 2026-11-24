# CrateUtils-Comfy

Utility nodes for ComfyUI that run your workflow on their own and save it the way you want:

| Node | What it does |
|---|---|
| **Task Scheduler** | Runs the workflow at a date and time, then repeats it: every N seconds / minutes / hours / days / weeks, a set number of times, on chosen weekdays, until an end date. |
| **Folder Watcher** | A hot folder: runs the workflow when files are added to (or changed or removed in) a folder, and outputs the file's path. |
| **File Watcher** | Runs the workflow when one file changes, e.g. every time you save a sketch or a prompt text file. |
| **Webhook Trigger** | Runs the workflow when a secret URL is called (phone shortcut, Stream Deck, script, bot); what the caller sends comes out as JSON. |
| **Folder Batch** | Runs the workflow once for every file already in a folder, one at a time, pausable. |
| **Load Image (Path)** | Loads an image from a path, such as the path a watcher outputs. |
| **JSON Field** | Picks one value out of JSON, e.g. a webhook's payload. |
| **Copy / Move Files** | Copies or moves files to another folder: finished exports to a share, a processed input into "done". |
| **Notify** | A Windows toast, Discord message or ntfy push (with the picture) when the workflow finishes, and when it fails. |
| **Export File** | Saves images as PNG (8/16-bit), JPEG, WebP, AVIF, TIFF (8/16/32-bit), JPEG 2000, EXR (half/float), BMP, TGA, ICO; a batch as an animated GIF / WebP / APNG or a multi-page PDF; as MP4 (H.264/H.265), WebM (VP9 with alpha), MOV (ProRes 4444) or MKV (FFV1 lossless) with optional audio; or audio alone as WAV, FLAC, MP3, Opus or AAC. Quality presets from lossless to small, or every setting by hand. |

Plus the **Triggers** side panel: every armed trigger with pause, fire and disarm, its run
history, and a CSV log of every triggered run.

Version 0.3.0. Changes are in [CHANGELOG.md](CHANGELOG.md).

## Install

```
cd ComfyUI/custom_nodes
git clone https://github.com/jevan2000/crateutils-comfy
```

Restart ComfyUI. The nodes are under **CrateUtils-Comfy** in the node menu. Nothing to pip install:
it uses numpy and Pillow, which ComfyUI already has. Optional extras:

- **ffmpeg** on PATH (or `CRATEUTILS_COMFY_FFMPEG`) for the video formats.
- **opencv-python-headless** for 16-bit and 32-bit TIFF (16-bit PNG and EXR need nothing).
- Pillow 11.3 or newer for AVIF.

Tested with ComfyUI 0.34.0 and 0.42.5 on Windows (Python 3.11).

## Triggers: Task Scheduler, Folder Watcher, File Watcher, Webhook Trigger, Folder Batch

1. Build the workflow and add a trigger node. Wire its outputs into the workflow where they
   matter: a watcher's `path` into **Load Image (Path)**, the scheduler's `seed` into a sampler,
   `run_index` into a filename, and so on.
2. Queue it once by hand to test it. A hand-queued run is a *manual* run: `run_index` is 0, and a
   Folder Watcher outputs the newest matching file.
3. Press **Arm** on the node. The server stores the workflow and runs it in the background
   from then on, even with the browser closed. Armed triggers survive a ComfyUI restart.
4. The button shows the status (`● Armed, next 09:00, 2/5 runs`). Press it again to **Disarm**.
   **Fire now** queues one test run without counting it.
5. The **Triggers** tab in the side bar (clock icon) lists every armed trigger, including those
   from other workflows, with Pause / Resume, Fire now, Disarm, Show node and the last 20 runs
   (status, time taken, output files). **Run log (CSV)** downloads every triggered run.

Arming takes a snapshot of the workflow: after you change the workflow, press Disarm and
Arm again. Deleting the node does not disarm it (switching workflows also removes nodes from
the canvas), so disarm first.

**Repeated runs and the cache.** ComfyUI skips nodes whose inputs have not changed, so an
identical workflow queued again does nothing. The trigger nodes always run, so anything wired
to them runs again; with `new_seeds` on (the scheduler's default), every `seed` / `noise_seed`
in the workflow also gets a new random value on each run.

### Task Scheduler settings

- `start`: `now`, `in 10 minutes`, `14:30` (today, or tomorrow if that time has passed),
  `2026-12-24 09:00`. Local time.
- `every` + `unit`: time between runs (the shortest is 10 seconds).
- `count`: runs in total; 0 = until you disarm it (or until `end`).
- `days`: empty = any day; `weekdays`, `weekends`, `mon,wed,fri`, `mon-fri`.
- `end` (advanced): stop after this date/time.
- `missed` (advanced): if ComfyUI was off or the PC asleep when a run was due, skip it, or run
  once to catch up.
- `skip_if_busy` (advanced): skip a run while the previous one is still queued.

### Watcher settings

- `folder` / `file`: relative paths are inside ComfyUI's input folder.
- `patterns`: e.g. `*.png;*.jpg`; empty = every file. Temp files (`*.tmp`, `*.part`,
  `*.crdownload`, `~$*`...) are always ignored.
- `events`: added, modified, removed, or any change.
- `settle`: a file must stay unchanged this many seconds before it counts, so files still being
  copied or rendered wait until they are complete.
- `poll`: seconds between checks. Watching is by polling, so it works on network shares too.
- `run_mode`: a run per file, or one run per batch of changes (the `paths` output lists them).
- `max_runs`: stop after this many runs; 0 = keep watching.
- `skip_if_busy` (advanced): hold changes while the previous run is queued, then send them.

Files already in the folder when you arm it are ignored. Changes made while ComfyUI is not
running are not seen. While paused, changes are dropped.

### Webhook Trigger

Arm it, then copy the URL from the Triggers panel (**Copy URL** / **Copy curl**). The URL
carries a secret token; send it as `?token=...`, an `X-CrateUtils-Comfy-Token` header or
`Authorization: Bearer ...`. A JSON or form body and any other query parameters come out as
`payload` (JSON); `text` is the `text` / `prompt` / `message` field or a plain-text body.
Use **JSON Field** (`field` like `prompt` or `settings.steps`) to pick values out.

```
curl -X POST "http://127.0.0.1:8188/crateutils-comfy/hook/<id>?token=<token>" -H "Content-Type: application/json" -d "{\"text\": \"a red fox\"}"
```

Replies: 202 queued, 403 wrong token, 409 paused, 410 out of runs, 413 body over 1 MB, 429 too
many waiting. Calls made while paused wait and run on Resume. Re-arming keeps the URL. The URL
works wherever ComfyUI is reachable: only this PC unless ComfyUI runs with `--listen`. Anyone
who can open ComfyUI can see the token in the panel (they could run any workflow anyway).

### Folder Batch

Press **Start batch**: one run per matching file, sorted by name, oldest or newest first, up to
`max_files`. With `skip_if_busy` on (the default) the next file is queued when the last one
is done, so Pause stops the batch between files; off queues everything at once. A manual run
uses the first file.

## Copy / Move Files

`paths` (one per line; e.g. a watcher's `path` or Export File's `paths`) go to `destination`
(relative = inside the output folder). Connect `after` to the step that must finish first:
to move a watched input into `done` after it is processed, wire the watcher's `path` into
`paths` and Export File's `paths` into `after`. `if_exists`: add a number, overwrite, or skip.

## Notify

`destination`: `toast` (a Windows notification on the PC running ComfyUI), a Discord webhook
URL, an ntfy topic URL, or a name from the config (below); several separated by commas. The
first connected image is attached. `message` fills in `{time}`, `{trigger}`, `{run}`, `{file}`
and `{event}`. With `on_failure` (default on) the same destination also hears when the
workflow fails anywhere, with the error. Connect `after` so the message waits for, say, the
export.

Webhook URLs are secrets, so name them in the config instead of typing them into workflows
you share (`ComfyUI/user/crateutils-comfy/config.json`):

```json
{"notify": {"phone": "https://ntfy.sh/my-topic", "team": "https://discord.com/api/webhooks/..."}}
```

## Export File

Pick a `format` and a `preset`:

| Preset | Sets |
|---|---|
| lossless | no loss where the format has it: 16-bit PNG/TIFF, lossless WebP/JPEG 2000, half-float EXR, lossless H.264/H.265/VP9, FFV1; highest quality otherwise (JPEG, AVIF, ProRes) |
| max quality | quality 100, 4:4:4 colour, 16-bit where possible |
| high | quality 92, 4:4:4 colour (the default) |
| balanced | quality 82 |
| small | quality 65, 4:2:0 colour, maximum encoder effort |
| custom | the node's own fields |

Other settings: `max_kb` lowers the quality of lossy still images until each file fits (e.g.
2000 for a YouTube thumbnail); `transparent` keeps alpha where the format has it (connect a
`mask`; Load Image's mask is 1 = transparent, so set `mask_meaning`); `fps` and `loop` for
animations and video; `audio` for video; `embed_workflow` puts the workflow into PNG, APNG,
WebP, AVIF and video files the way Save Image does. EXR is written premultiplied and, by
default, in linear light (`exr_linear`), which is what compositing apps expect.

A batch is saved as one file per image, except for the animation, PDF and video formats, which
make one file from the whole batch.

**Audio:** pick wav, flac, mp3, opus or m4a and connect `audio` (no image needed). WAV is 16,
24 or 32-bit float; FLAC 16 or 24-bit (`compression` sets the FLAC level); MP3 is VBR from
`quality` (or 320 kbps with `lossless`); Opus and AAC bitrates follow `quality`. Needs ffmpeg
(except 16-bit WAV).

## Allowed folders

Workflows are shared files, so CrateUtils-Comfy only reads, watches and writes inside ComfyUI's
input, output and temp folders by default. To allow more, create
`ComfyUI/user/crateutils-comfy/config.json`:

```json
{"allowed_folders": ["D:/Renders", "S:/Shared/Inbox"]}
```

or set `CRATEUTILS_COMFY_ALLOWED_FOLDERS` (`;`-separated on Windows), then restart ComfyUI.

## API

The Arm button calls these routes; scripts can call them too:

- `GET /crateutils-comfy/triggers`: armed triggers and their status.
- `POST /crateutils-comfy/triggers` `{"key", "node_id", "prompt", "extra_pnginfo"?}`: arm the trigger
  node `node_id` of an API-format `prompt`.
- `POST /crateutils-comfy/triggers/disarm` `{"key"}` or `{"all": true}`.
- `POST /crateutils-comfy/triggers/fire` `{"key"}`: one test run.
- `POST /crateutils-comfy/triggers/pause` `{"key", "paused": true|false}`.
- `GET|POST /crateutils-comfy/hook/<id>`: a Webhook Trigger's URL (needs its token).
- `GET /crateutils-comfy/runs.csv`: the run log.

Armed triggers are stored in `ComfyUI/user/crateutils-comfy/triggers.json` and the run log in
`ComfyUI/user/crateutils-comfy/runs.csv` (fired at, trigger, title, kind, run, event, input files,
prompt id, status, seconds, outputs, error).

## Development

```
python -m unittest discover -s tests -v
```

The core (`schedule.py`, `watch.py`, `triggers.py`, `exporter.py`, `paths.py`, `notify.py`, `fileops.py`) has no ComfyUI
imports and is tested on its own.

## Licence

MIT
