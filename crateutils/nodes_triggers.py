"""Trigger nodes: Task Scheduler, Folder Watcher, File Watcher.

Each node has an "Arm" button (web/crateutils.js). Arming stores this workflow with
the server, which queues it again when the schedule is due or the watched folder/file
changes. On those runs the node outputs what fired it; on a normal run it outputs a
manual run (index 0), so the workflow can be tested by hand.
"""

from __future__ import annotations

import os
import random
import time

from comfy_api.latest import IO, UI

from . import schedule as sch
from . import triggers, watch

ROOT = "CrateUtils-Comfy"
CAT = f"{ROOT}/Triggers"
ARM = ("Press Arm on the node to start (it stores this workflow and runs it in the background, even with the "
       "browser closed, and after a ComfyUI restart); Disarm to stop. Queueing the workflow yourself is a "
       "manual test run: index 0.")


def _common_inputs(new_seeds_default: bool) -> list:
    return [
        IO.Boolean.Input("new_seeds", default=new_seeds_default,
                         tooltip="Give every 'seed' / 'noise_seed' in the workflow a new random value on each "
                                 "triggered run, so repeated runs make new results instead of hitting the cache"),
        IO.Boolean.Input("skip_if_busy", default=False, advanced=True,
                         tooltip="Don't queue another run while this trigger's last run is still queued or running "
                                 "(a schedule skips that run; a watcher waits and sends the changes after)"),
    ]


def _run(cls) -> dict | None:
    return triggers.read_run(cls.hidden.prompt, cls.hidden.unique_id)


def _seed(run: dict | None) -> int:
    return int(run["seed"]) if run and "seed" in run else random.randint(0, 2**53 - 1)


class CUScheduler(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        return IO.Schema(
            node_id="CUScheduler", display_name="Task Scheduler", category=CAT,
            search_aliases=["schedule", "timer", "cron", "run at", "repeat", "every hour"],
            description="Run this workflow at a date and time, and repeat it a number of times. " + ARM,
            inputs=[
                IO.String.Input("start", default="now",
                                tooltip="When the first run is: 'now', 'in 10 minutes', '14:30' (today, or tomorrow "
                                        "if past), '2026-12-24 09:00' (local time)"),
                IO.Float.Input("every", default=1.0, min=0.0, max=100000.0, step=0.5,
                               tooltip="Time between runs (0 with count 1 = run once)"),
                IO.Combo.Input("unit", options=list(sch.UNITS), default="hours"),
                IO.Int.Input("count", default=1, min=0, max=1_000_000,
                             tooltip="How many runs in total; 0 = keep going until Disarm (or 'end')"),
                IO.String.Input("days", default="",
                                tooltip="Only on these weekdays: empty = any day, 'weekdays', 'weekends', "
                                        "'mon,wed,fri', 'mon-fri'"),
                IO.String.Input("end", default="", advanced=True,
                                tooltip="Stop after this date/time (empty = no end; a date alone = the end of that day)"),
                IO.Combo.Input("missed", options=sch.MISSED, default=sch.MISSED[0], advanced=True,
                               tooltip="If ComfyUI was off (or the PC asleep) when a run was due"),
            ] + _common_inputs(True),
            outputs=[IO.Int.Output(display_name="run_index", tooltip="1, 2, 3... on triggered runs; 0 = manual"),
                     IO.Int.Output(display_name="seed", tooltip="A new random seed for each run"),
                     IO.String.Output(display_name="time", tooltip="When this run fired (YYYY-MM-DD HH:MM:SS)"),
                     IO.String.Output(display_name="info")],
            hidden=[IO.Hidden.prompt, IO.Hidden.unique_id],
        )

    @classmethod
    def fingerprint_inputs(cls, **kw):
        return time.time_ns()  # always run: every run is a new firing

    @classmethod
    def validate_inputs(cls, **kw):
        need = ("start", "every", "unit", "count", "days")
        if any(k not in kw for k in need):  # linked values are checked when the node runs
            return True
        try:
            sch.build(kw["start"], kw["every"], kw["unit"], kw["count"], kw["days"], kw.get("end", "") or "",
                      sch.datetime.now())
        except ValueError as e:
            return str(e)
        return True

    @classmethod
    def execute(cls, start, every, unit, count, days, end="", missed=sch.MISSED[0], new_seeds=True,
                skip_if_busy=False):
        run = _run(cls)
        now = sch.datetime.now()
        if run:
            t = run.get("time", now.isoformat(timespec="seconds")).replace("T", " ")
            info = f"scheduled run {run.get('index')}" + (f" of {count}" if count else "") + f" at {t}"
            if run.get("event") == "test":
                info = f"test run (Fire now) at {t}"
            idx = int(run.get("index", 0))
        else:
            plan = sch.describe(sch.build(start, every, unit, count, days, end, now))
            t = now.strftime("%Y-%m-%d %H:%M:%S")
            info, idx = f"manual run at {t}; when armed: {plan}", 0
        return IO.NodeOutput(idx, _seed(run), t, info, ui=UI.PreviewText(info))


def _watch_outputs(folder: bool) -> list:
    outs = [IO.String.Output(display_name="path", tooltip="The changed file (the first one, for a batch)")]
    if folder:
        outs.append(IO.String.Output(display_name="paths", tooltip="Every changed file in this run, one per line"))
    return outs + [IO.String.Output(display_name="event", tooltip="added, modified, removed (manual run: manual)"),
                   IO.Int.Output(display_name="run_index", tooltip="1, 2, 3... on triggered runs; 0 = manual"),
                   IO.Int.Output(display_name="seed"), IO.String.Output(display_name="info")]


def _watch_inputs(event_options: list[str], default_event: str) -> list:
    return [
        IO.Combo.Input("events", options=event_options, default=default_event),
        IO.Float.Input("settle", default=2.0, min=0.0, max=600.0, step=0.5,
                       tooltip="Seconds a file must stay unchanged before it counts (so half-copied files wait)"),
        IO.Float.Input("poll", default=2.0, min=0.5, max=3600.0, step=0.5, tooltip="Seconds between checks"),
        IO.Int.Input("max_runs", default=0, min=0, max=1_000_000,
                     tooltip="Stop after this many runs; 0 = keep watching until Disarm"),
    ]


class CUFolderWatcher(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        return IO.Schema(
            node_id="CUFolderWatcher", display_name="Folder Watcher", category=CAT,
            search_aliases=["watch folder", "hot folder", "drop folder", "on new file", "folder trigger"],
            description="Run this workflow when files are added to (or changed or removed in) a folder, with the "
                        "file's path as an output: a hot folder. Files already there when you arm it are ignored. "
                        + ARM + " A manual run outputs the newest matching file.",
            inputs=[
                IO.String.Input("folder", default="",
                                tooltip="Folder to watch; relative paths are inside ComfyUI's input folder. Folders "
                                        "outside input/output need adding to the CrateUtils-Comfy config (see README)"),
                IO.String.Input("patterns", default="*.png;*.jpg;*.jpeg;*.webp",
                                tooltip="Which files: patterns separated by ; (empty = all files). Temp files "
                                        "(*.tmp, *.part, *.crdownload...) are always ignored"),
                IO.Boolean.Input("recursive", default=False, tooltip="Include subfolders"),
            ] + _watch_inputs(list(watch.FOLDER_EVENTS), "added") + [
                IO.Combo.Input("run_mode", options=triggers.RUN_MODES, default=triggers.RUN_MODES[0],
                               tooltip="A run for each file, or one run for everything that changed together "
                                       "(the 'paths' output lists them)"),
            ] + _common_inputs(False),
            outputs=_watch_outputs(True),
            hidden=[IO.Hidden.prompt, IO.Hidden.unique_id],
        )

    @classmethod
    def fingerprint_inputs(cls, **kw):
        return time.time_ns()

    @classmethod
    def execute(cls, folder, patterns, recursive, events, settle, poll, max_runs, run_mode, new_seeds=False,
                skip_if_busy=False):
        from . import comfy_glue

        run = _run(cls)
        root = comfy_glue.check_read(folder, "watched folder")
        if run:
            ps = list(run.get("paths", []))
            ev = run.get("event", "")
            info = f"run {run.get('index')}: {ev} {len(ps)} file(s) in {root}" + "".join(f"\n{p}" for p in ps[:20])
            return IO.NodeOutput(ps[0] if ps else "", "\n".join(ps), ev, int(run.get("index", 0)), _seed(run), info,
                                 ui=UI.PreviewText(info))
        if not root.is_dir():
            raise ValueError(f"folder {root} does not exist")
        p = watch.newest(str(root), watch.pattern_matcher(patterns), recursive)
        info = f"manual run: newest matching file is {p or '(none)'}; when armed: watching {root} for {events}"
        return IO.NodeOutput(p, p, "manual", 0, _seed(None), info, ui=UI.PreviewText(info))


class CUFileWatcher(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        return IO.Schema(
            node_id="CUFileWatcher", display_name="File Watcher", category=CAT,
            search_aliases=["watch file", "on file change", "file trigger", "on save"],
            description="Run this workflow when one file changes (saved over, created or removed), e.g. re-run "
                        "whenever you save a sketch or a prompt text file. " + ARM,
            inputs=[IO.String.Input("file", default="",
                                    tooltip="The file to watch (it may not exist yet); relative paths are inside "
                                            "ComfyUI's input folder")]
            + _watch_inputs(list(watch.FILE_EVENTS), "changed (created or modified)") + _common_inputs(False),
            outputs=_watch_outputs(False),
            hidden=[IO.Hidden.prompt, IO.Hidden.unique_id],
        )

    @classmethod
    def fingerprint_inputs(cls, **kw):
        return time.time_ns()

    @classmethod
    def execute(cls, file, events, settle, poll, max_runs, new_seeds=False, skip_if_busy=False):
        from . import comfy_glue

        run = _run(cls)
        path = comfy_glue.check_read(file, "watched file")
        if run:
            ev = run.get("event", "")
            info = f"run {run.get('index')}: {path} {ev}"
            return IO.NodeOutput(str(path), ev, int(run.get("index", 0)), _seed(run), info, ui=UI.PreviewText(info))
        state = "exists" if os.path.isfile(path) else "does not exist yet"
        info = f"manual run: {path} {state}; when armed: watching it for {events}"
        return IO.NodeOutput(str(path), "manual", 0, _seed(None), info, ui=UI.PreviewText(info))


class CUWebhook(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        return IO.Schema(
            node_id="CUWebhook", display_name="Webhook Trigger", category=CAT,
            search_aliases=["webhook", "http trigger", "api trigger", "remote run", "url trigger"],
            description="Run this workflow when a URL is called: from a phone shortcut, a Stream Deck, a script or "
                        "a bot. Arm it, then copy the URL and secret token from the Trigger Manager panel "
                        "(CrateUtils-Comfy in the side bar). Send the token as ?token=..., an X-CrateUtils-Token header "
                        "or 'Authorization: Bearer ...'. A JSON or form body (and other query parameters) comes "
                        "out as 'payload'; use JSON Field to pick values out. The URL works wherever ComfyUI is "
                        "reachable (only this PC unless ComfyUI runs with --listen). " + ARM,
            inputs=[IO.Int.Input("max_runs", default=0, min=0, max=1_000_000,
                                 tooltip="Stop after this many calls; 0 = until Disarm")] + _common_inputs(False),
            outputs=[IO.String.Output(display_name="payload", tooltip="Everything the caller sent, as JSON"),
                     IO.String.Output(display_name="text",
                                      tooltip="The 'text' (or 'prompt' / 'message') field, or a plain-text body"),
                     IO.Int.Output(display_name="run_index"), IO.Int.Output(display_name="seed"),
                     IO.String.Output(display_name="info")],
            hidden=[IO.Hidden.prompt, IO.Hidden.unique_id],
        )

    @classmethod
    def fingerprint_inputs(cls, **kw):
        return time.time_ns()

    @classmethod
    def execute(cls, max_runs, new_seeds=False, skip_if_busy=False):
        import json

        run = _run(cls)
        payload = (run or {}).get("payload") or {}
        text = ""
        for k in ("text", "prompt", "message"):
            if isinstance(payload.get(k), str):
                text = payload[k]
                break
        if run:
            info = f"webhook call {run.get('index')} at {run.get('time', '').replace('T', ' ')}"
            if run.get("event") == "test":
                info = "test run (Fire now): no payload"
        else:
            info = "manual run: no payload; arm the node and call its URL"
        return IO.NodeOutput(json.dumps(payload), text, int((run or {}).get("index", 0)), _seed(run), info,
                             ui=UI.PreviewText(info))


class CUFolderBatch(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        return IO.Schema(
            node_id="CUFolderBatch", display_name="Folder Batch", category=CAT,
            search_aliases=["batch folder", "for each file", "process folder", "loop files", "run per file"],
            description="Run this workflow once for every file already in a folder (the watchers only see new "
                        "ones). Press Start on the node: one run per file, in the order you pick, by default one "
                        "at a time so the batch can be paused from the Trigger Manager panel. A manual run uses "
                        "the first file, so you can test on it.",
            inputs=[IO.String.Input("folder", default="",
                                    tooltip="Relative paths are inside ComfyUI's input folder"),
                    IO.String.Input("patterns", default="*.png;*.jpg;*.jpeg;*.webp",
                                    tooltip="Which files, separated by ; (empty = all)"),
                    IO.Boolean.Input("recursive", default=False, tooltip="Include subfolders"),
                    IO.Combo.Input("sort", options=triggers.BATCH_SORT, default=triggers.BATCH_SORT[0]),
                    IO.Int.Input("max_files", default=0, min=0, max=1_000_000, tooltip="0 = every file"),
                    IO.Boolean.Input("new_seeds", default=False,
                                     tooltip="A new random seed in every seed input for each file"),
                    IO.Boolean.Input("skip_if_busy", default=True, advanced=True,
                                     tooltip="On: queue the next file when the last one is done (pausable). "
                                             "Off: queue every file at once")],
            outputs=[IO.String.Output(display_name="path"),
                     IO.String.Output(display_name="name", tooltip="File name without folder or extension"),
                     IO.Int.Output(display_name="index", tooltip="1, 2, 3... (0 = manual run)"),
                     IO.Int.Output(display_name="total"), IO.Int.Output(display_name="seed"),
                     IO.String.Output(display_name="info")],
            hidden=[IO.Hidden.prompt, IO.Hidden.unique_id],
        )

    @classmethod
    def fingerprint_inputs(cls, **kw):
        return time.time_ns()

    @classmethod
    def execute(cls, folder, patterns, recursive, sort, max_files, new_seeds=False, skip_if_busy=True):
        from pathlib import Path

        from . import comfy_glue

        run = _run(cls)
        if run and run.get("paths"):
            p = run["paths"][0]
            total = int(run.get("total", 0))
            info = f"file {run.get('index')} of {total}: {p}"
            return IO.NodeOutput(p, Path(p).stem, int(run.get("index", 0)), total, _seed(run), info,
                                 ui=UI.PreviewText(info))
        root = comfy_glue.check_read(folder, "batch folder")
        if not root.is_dir():
            raise ValueError(f"folder {root} does not exist")
        snap = watch.snapshot(str(root), watch.pattern_matcher(patterns), recursive)
        if not snap:
            raise ValueError(f"no matching files in {root}")
        files = sorted(snap, key=lambda p: p.lower()) if sort == "name" else \
            sorted(snap, key=lambda p: snap[p][1], reverse=sort == "newest first")
        total = min(len(files), max_files) if max_files else len(files)
        info = f"manual run on the first file of {total}: {files[0]}; press Start to run them all"
        return IO.NodeOutput(files[0], Path(files[0]).stem, 0, total, _seed(None), info, ui=UI.PreviewText(info))


class CUJsonField(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        return IO.Schema(
            node_id="CUJsonField", display_name="JSON Field", category=f"{ROOT}/Files",
            search_aliases=["json", "get field", "payload field", "parse json"],
            description="Pick one value out of JSON, e.g. a Webhook Trigger's payload. 'field' is a dotted path: "
                        "prompt, settings.steps, images.0.url.",
            inputs=[IO.String.Input("json", default="{}", multiline=True),
                    IO.String.Input("field", default="text"),
                    IO.String.Input("default", default="", tooltip="Used when the field is missing")],
            outputs=[IO.String.Output(display_name="string"), IO.Int.Output(display_name="int"),
                     IO.Float.Output(display_name="float"), IO.Boolean.Output(display_name="found")],
        )

    @classmethod
    def execute(cls, json, field, default):
        import json as _json

        try:
            v = _json.loads(json or "null")
        except ValueError as e:
            raise ValueError(f"not valid JSON: {e}") from e
        found = True
        for part in [p for p in (field or "").split(".") if p != ""]:
            if isinstance(v, dict) and part in v:
                v = v[part]
            elif isinstance(v, list) and part.lstrip("-").isdigit() and -len(v) <= int(part) < len(v):
                v = v[int(part)]
            else:
                found, v = False, default
                break
        s = v if isinstance(v, str) else _json.dumps(v)
        try:
            f = float(v)
        except (TypeError, ValueError):
            f = 0.0
        i = int(f) if f == f and abs(f) < 2**63 else 0
        return IO.NodeOutput(s, i, f, found)


NODES = [CUScheduler, CUFolderWatcher, CUFileWatcher, CUWebhook, CUFolderBatch, CUJsonField]
