# Changelog

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
