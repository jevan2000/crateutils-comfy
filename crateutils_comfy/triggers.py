"""Trigger manager: armed schedules, watchers, webhooks and folder batches that queue a stored workflow.

Arming stores the workflow (the API prompt plus the UI workflow for metadata) and the
trigger node's settings. A background thread checks every trigger about twice a
second; when one fires, a copy of the prompt is queued with the firing details
written into the trigger node's inputs under ``RUN_KEY``, where the node reads them.

Every queued run is then tracked until ComfyUI finishes it, for the run log (a CSV),
the run history shown in the Trigger Manager panel, and failure notifications.

No ComfyUI imports: queueing, the busy and history checks, notifications and the path
policy are passed in.
"""

from __future__ import annotations

import copy
import csv
import json
import logging
import os
import random
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import schedule as sch
from . import watch

log = logging.getLogger("CrateUtils-Comfy")

RUN_KEY = "_crateutils_comfy_run"
KINDS = {"CUCScheduler": "schedule", "CUCFolderWatcher": "folder", "CUCFileWatcher": "file",
         "CUCWebhook": "webhook", "CUCFolderBatch": "batch"}
RUN_MODES = ["one run per file", "one run per batch"]
BATCH_SORT = ["name", "oldest first", "newest first"]
SEED_KEYS = ("seed", "noise_seed")
HISTORY = 20
INBOX_MAX = 100
TRACK_POLL = 3.0               # seconds between run-status checks
TRACK_LOST_AFTER = 60.0        # not queued and not in history for this long: cancelled
TRACK_GIVE_UP = 24 * 3600.0
LOG_COLUMNS = ["fired_at", "trigger", "title", "kind", "run_index", "event", "input_files", "prompt_id", "status",
               "duration_s", "outputs", "error"]


@dataclass
class Trigger:
    key: str
    kind: str
    node_id: str
    prompt: dict
    extra_pnginfo: dict | None
    settings: dict
    local_id: str = ""
    graph_id: str = ""
    state: dict = field(default_factory=dict)
    title: str = ""
    # runtime only
    sched: sch.Schedule | None = None
    watcher: watch.Watcher | None = None
    last_poll: float = 0.0
    held: list = field(default_factory=list)   # watcher events waiting while the last run is busy
    inbox: list = field(default_factory=list)  # runs asked for from outside (Fire now, webhook calls)
    ready: bool = False

    def to_json(self) -> dict:
        return {"key": self.key, "kind": self.kind, "node_id": self.node_id, "local_id": self.local_id,
                "graph_id": self.graph_id, "title": self.title, "prompt": self.prompt,
                "extra_pnginfo": self.extra_pnginfo, "settings": self.settings, "state": self.state}


def _literal(inputs: dict, name: str, default=None):
    v = inputs.get(name, default)
    if isinstance(v, list):  # a link: [node_id, slot]
        raise ValueError(f"'{name}' is connected to another node; trigger settings must be typed into the node")
    return v


def settings_from_prompt(kind: str, inputs: dict) -> dict:
    g = lambda n, d=None: _literal(inputs, n, d)  # noqa: E731
    common = {"new_seeds": bool(g("new_seeds", kind == "schedule")),
              "skip_if_busy": bool(g("skip_if_busy", kind == "batch"))}
    if kind == "schedule":
        return {**common, "start": str(g("start", "now")), "every": float(g("every", 1.0)),
                "unit": str(g("unit", "hours")), "count": int(g("count", 1)), "days": str(g("days", "")),
                "end": str(g("end", "")), "missed": str(g("missed", sch.MISSED[0]))}
    if kind == "webhook":
        return {**common, "max_runs": int(g("max_runs", 0))}
    if kind == "batch":
        return {**common, "folder": str(g("folder", "")), "patterns": str(g("patterns", "")),
                "recursive": bool(g("recursive", False)), "sort": str(g("sort", BATCH_SORT[0])),
                "max_files": int(g("max_files", 0))}
    w = {**common, "events": str(g("events")), "settle": float(g("settle", 2.0)), "poll": float(g("poll", 2.0)),
         "max_runs": int(g("max_runs", 0))}
    if kind == "folder":
        return {**w, "folder": str(g("folder", "")), "patterns": str(g("patterns", "")),
                "recursive": bool(g("recursive", False)), "run_mode": str(g("run_mode", RUN_MODES[0]))}
    return {**w, "file": str(g("file", ""))}


def read_run(prompt: dict | None, unique_id) -> dict | None:
    """The firing details a trigger node finds in its own inputs (None for a manual run)."""
    try:
        raw = (prompt or {})[str(unique_id)]["inputs"].get(RUN_KEY)
    except (KeyError, TypeError, AttributeError):
        return None
    if not raw:
        return None
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (ValueError, TypeError):
        return None


def find_run(prompt: dict | None) -> dict | None:
    """The firing details of whichever trigger started this prompt (None for a manual run)."""
    for nid in (prompt or {}):
        r = read_run(prompt, nid)
        if r:
            return r
    return None


def reroll_seeds(prompt: dict, rng: random.Random, skip: str = "") -> int:
    n = 0
    for nid, node in prompt.items():
        if nid == skip or not isinstance(node, dict):
            continue
        ins = node.get("inputs", {})
        for k in SEED_KEYS:
            if isinstance(ins.get(k), int) and not isinstance(ins.get(k), bool):
                ins[k] = rng.randint(0, 2**53 - 1)
                n += 1
    return n


def summarize_history(entry: dict) -> dict:
    """status / duration / outputs / error from a ComfyUI /history entry."""
    st = entry.get("status") or {}
    msgs = st.get("messages") or []
    ts = {}
    error = ""
    for m in msgs:
        if isinstance(m, (list, tuple)) and len(m) == 2 and isinstance(m[1], dict):
            ts.setdefault(m[0], m[1].get("timestamp"))
            if m[0] == "execution_error":
                d = m[1]
                error = f"{d.get('node_type', '')} (node {d.get('node_id', '')}): {d.get('exception_message', '')}".strip()
            elif m[0] == "execution_interrupted":
                error = "interrupted"
    status = st.get("status_str") or ("success" if st.get("completed") else "unknown")
    if error == "interrupted":
        status = "interrupted"
    dur = None
    end = ts.get("execution_success") or ts.get("execution_error") or ts.get("execution_interrupted")
    if ts.get("execution_start") and end:
        dur = round((end - ts["execution_start"]) / 1000.0, 2)
    outputs = []
    for node_out in (entry.get("outputs") or {}).values():
        for v in node_out.values() if isinstance(node_out, dict) else []:
            for item in v if isinstance(v, list) else []:
                if isinstance(item, dict) and item.get("filename"):
                    sub = item.get("subfolder") or ""
                    outputs.append(f"{sub}/{item['filename']}" if sub else item["filename"])
    return {"status": status, "duration": dur, "outputs": outputs, "error": error.strip()}


class Manager:
    def __init__(self, store: str | os.PathLike | None, queue_fn: Callable[[dict, dict | None], str],
                 busy_fn: Callable[[str], bool] | None = None,
                 resolve_watch_path: Callable[[str, str], Path] | None = None,
                 now_fn: Callable[[], datetime] = datetime.now, mono_fn: Callable[[], float] = time.monotonic,
                 history_fn: Callable[[str], dict | None] | None = None,
                 notify_fn: Callable[[dict, str, str], None] | None = None,
                 log_path: str | os.PathLike | None = None):
        self.store = Path(store) if store else None
        self.log_path = Path(log_path) if log_path else None
        self.queue_fn, self.busy_fn = queue_fn, busy_fn or (lambda pid: False)
        self.history_fn = history_fn or (lambda pid: None)
        self.notify_fn = notify_fn
        self.resolve = resolve_watch_path or (lambda p, what: Path(p))
        self.now, self.mono = now_fn, mono_fn
        self.triggers: dict[str, Trigger] = {}
        self.tracked: dict[str, dict] = {}  # prompt_id -> what to do when it finishes
        self.last_track = 0.0
        self.lock = threading.RLock()
        self.rng = random.Random()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ----- arm / disarm -------------------------------------------------------------
    def arm(self, key: str, node_id: str, prompt: dict, extra_pnginfo: dict | None = None,
            local_id: str = "", graph_id: str = "", title: str = "") -> dict:
        node_id = str(node_id)
        if node_id not in prompt:
            raise ValueError(f"node {node_id} is not in the workflow (is it muted or bypassed?)")
        kind = KINDS.get(prompt[node_id].get("class_type"))
        if kind is None:
            raise ValueError(f"node {node_id} is not a CrateUtils-Comfy trigger")
        settings = settings_from_prompt(kind, prompt[node_id].get("inputs", {}))
        clean = copy.deepcopy(prompt)
        clean[node_id].get("inputs", {}).pop(RUN_KEY, None)
        t = Trigger(key, kind, node_id, clean, extra_pnginfo, settings, str(local_id or node_id), str(graph_id),
                    title=str(title or ""))
        old = self.triggers.get(key)
        t.state = {"armed_at": self.now().isoformat(timespec="seconds"), "fired": 0, "runs": []}
        if kind == "webhook":  # re-arming keeps the URL, so callers don't need updating
            prev = (old.state if old and old.kind == "webhook" else {})
            t.state["hook_id"] = prev.get("hook_id") or secrets.token_hex(6)
            t.state["token"] = prev.get("token") or secrets.token_urlsafe(24)
        self._build(t, fresh=True)
        with self.lock:
            self.triggers[key] = t
            self.save()
        log.info("CrateUtils-Comfy: armed %s %s (%s)", kind, key, self.summary(t))
        return self.status(t, secrets_too=True)

    def disarm(self, key: str) -> bool:
        with self.lock:
            ok = self.triggers.pop(key, None) is not None
            self.save()
        if ok:
            log.info("CrateUtils-Comfy: disarmed %s", key)
        return ok

    def disarm_all(self) -> int:
        with self.lock:
            n = len(self.triggers)
            self.triggers.clear()
            self.save()
        return n

    def pause(self, key: str, paused: bool) -> bool:
        with self.lock:
            t = self.triggers.get(key)
            if t is None:
                return False
            t.state["paused"] = bool(paused)
            self.save()
        log.info("CrateUtils-Comfy: %s %s", "paused" if paused else "resumed", key)
        return True

    def request_fire(self, key: str, info: dict | None = None) -> bool:
        """Queue a run on the next tick (from the trigger thread, never from a web request).
        Without ``info`` it is an uncounted test run (Fire now)."""
        with self.lock:
            t = self.triggers.get(key)
            if t is None:
                return False
            if len(t.inbox) >= INBOX_MAX:
                raise OverflowError("too many runs waiting for this trigger")
            t.inbox.append(info or {"event": "test", "paths": []})
            return True

    def find_hook(self, hook_id: str) -> Trigger | None:
        with self.lock:
            for t in self.triggers.values():
                if t.kind == "webhook" and t.state.get("hook_id") == hook_id:
                    return t
        return None

    def track_external(self, prompt_id: str, notify: dict) -> None:
        """Watch a run queued by someone else (a workflow with a Notify node set to report failures)."""
        with self.lock:
            entry = self.tracked.setdefault(prompt_id, {"since": self.mono(), "fired_at": None})
            entry["notify"] = notify

    # ----- runtime ------------------------------------------------------------------
    def _build(self, t: Trigger, fresh: bool) -> None:
        s = t.settings
        t.ready = False
        if t.kind == "schedule":
            now = self.now()
            armed = datetime.fromisoformat(t.state.get("armed_at", now.isoformat()))
            # "now" and "in 10 minutes" are relative to when it was armed, not to a restart
            t.sched = sch.build(s["start"], s["every"], s["unit"], s["count"], s["days"], s["end"], armed)
            if fresh:
                nxt = t.sched.next_at_or_after(now.replace(microsecond=0))
                t.state["next_k"], t.state["next_at"] = (nxt[0], nxt[1].isoformat()) if nxt else (None, None)
                if nxt is None:
                    raise ValueError(f"this schedule never runs again ({sch.describe(t.sched)})")
            t.ready = True
            return
        if t.kind == "webhook":
            t.ready = True
            return
        if t.kind == "batch":
            if fresh:
                folder = self.resolve(s["folder"], "batch folder")
                if not folder.is_dir():
                    raise ValueError(f"folder {folder} does not exist")
                if s["sort"] not in BATCH_SORT:
                    raise ValueError(f"sort must be one of {', '.join(BATCH_SORT)}")
                snap = watch.snapshot(str(folder), watch.pattern_matcher(s["patterns"]), s["recursive"])
                if s["sort"] == "name":
                    files = sorted(snap, key=lambda p: p.lower())
                else:
                    files = sorted(snap, key=lambda p: snap[p][1], reverse=s["sort"] == "newest first")
                if s["max_files"]:
                    files = files[: s["max_files"]]
                if not files:
                    raise ValueError(f"no matching files in {folder}")
                t.state["pending_files"], t.state["total"] = files, len(files)
            t.ready = True
            return
        if t.kind == "folder":
            folder = self.resolve(s["folder"], "watched folder")
            if not folder.is_dir():
                raise ValueError(f"folder {folder} does not exist")
            match, recursive = watch.pattern_matcher(s["patterns"]), s["recursive"]
            events = watch.FOLDER_EVENTS.get(s["events"])
        else:
            path = self.resolve(s["file"], "watched file")
            if not path.parent.is_dir():
                raise ValueError(f"folder {path.parent} does not exist")
            folder, recursive = path.parent, False
            target = path.name.lower()
            match = lambda n: n.lower() == target  # noqa: E731
            events = watch.FILE_EVENTS.get(s["events"])
        if events is None:
            raise ValueError(f"unknown events setting {s['events']!r}")
        if s["poll"] < 0.5:
            raise ValueError("poll must be at least 0.5 seconds")
        t.watcher = watch.Watcher(str(folder), match, recursive, events, s["settle"], self.mono())
        t.last_poll = self.mono()
        t.ready = True

    def tick(self) -> None:
        with self.lock:
            items = list(self.triggers.values())
        changed = False
        for t in items:
            try:
                changed |= self._tick_one(t)
            except Exception as e:  # one bad trigger must not stop the others
                t.state["last_error"] = str(e)
                changed = True
                log.warning("CrateUtils-Comfy trigger %s: %s", t.key, e)
        try:
            changed |= self._track()
        except Exception as e:
            log.warning("CrateUtils-Comfy run tracking: %s", e)
        if changed:
            with self.lock:
                self.save()

    def _busy(self, t: Trigger) -> bool:
        pid = t.state.get("last_prompt_id")
        return bool(t.settings.get("skip_if_busy") and pid and self.busy_fn(pid))

    def _limit_reached(self, t: Trigger) -> bool:
        lim = t.settings.get("max_runs") or 0
        return bool(lim and t.state["fired"] >= lim)

    def _tick_one(self, t: Trigger) -> bool:
        if not t.ready:  # not restored after a restart; see last_error
            t.inbox.clear()
            return False
        changed = False
        paused = t.state.get("paused")
        while t.inbox and not paused:
            info = t.inbox[0]
            if info.get("event") == "test":
                t.inbox.pop(0)
                self._fire(t, info, count=False)
                changed = True
                continue
            if t.state.get("done") or self._limit_reached(t):
                t.inbox.clear()
                t.state["done"] = True
                return True
            if self._busy(t):
                break
            t.inbox.pop(0)
            self._fire(t, info)
            changed = True
            if self._limit_reached(t):
                t.state["done"] = True
        if t.kind == "schedule":
            return self._tick_schedule(t) or changed
        if t.kind == "batch":
            return self._tick_batch(t) or changed
        if t.kind == "webhook":
            return changed
        return self._tick_watch(t) or changed

    def _tick_schedule(self, t: Trigger) -> bool:
        st, s = t.state, t.settings
        if st.get("next_at") is None:
            return False
        now = self.now()
        due = datetime.fromisoformat(st["next_at"])
        if now < due:
            return False
        k = st["next_k"]
        late = (now - due).total_seconds()
        if st.get("paused"):
            st["skipped_paused"] = st.get("skipped_paused", 0) + 1
            late = sch.GRACE + 1  # move on to the next future run
        elif late > sch.GRACE and s["missed"] == sch.MISSED[0]:
            st["missed"] = st.get("missed", 0) + 1
            log.info("CrateUtils-Comfy: %s missed the run due %s (%.0f s late)", t.key, due, late)
        elif self._busy(t):
            st["skipped_busy"] = st.get("skipped_busy", 0) + 1
            log.info("CrateUtils-Comfy: %s skipped a run, the last one is still queued", t.key)
        else:
            self._fire(t, {"event": "schedule", "due": due.isoformat(timespec="seconds"), "paths": []})
        if s["count"] and st["fired"] >= s["count"]:
            nxt = None
        else:
            # after a late run, the next one is strictly in the future (never a second catch-up run)
            after = (now.replace(microsecond=0) + timedelta(seconds=1)) if late > sch.GRACE else due
            nxt = t.sched.next_at_or_after(after, k + 1)
        st["next_k"], st["next_at"] = (nxt[0], nxt[1].isoformat()) if nxt else (None, None)
        if nxt is None:
            st["done"] = True
        return True

    def _tick_batch(self, t: Trigger) -> bool:
        st = t.state
        pending = st.get("pending_files") or []
        if st.get("paused") or st.get("done"):
            return False
        if not pending:
            st["done"] = True
            return True
        fired = 0
        while pending and fired < 25:  # at most 25 per tick keeps the thread responsive
            if self._busy(t):
                break
            p = pending.pop(0)
            if not os.path.isfile(p):
                st["skipped_missing"] = st.get("skipped_missing", 0) + 1
                continue
            self._fire(t, {"event": "batch", "paths": [p], "total": st.get("total", 0)})
            fired += 1
        if not pending:
            st["done"] = True
        return True

    def _tick_watch(self, t: Trigger) -> bool:
        s, st = t.settings, t.state
        if st.get("done"):
            return False
        m = self.mono()
        if m - t.last_poll < s["poll"]:
            return False
        t.last_poll = m
        events = t.held + t.watcher.poll(m)
        t.held = []
        was_ok = st.get("watch_ok", True)
        st["watch_ok"] = t.watcher.folder_ok
        if st.get("paused"):  # keep the baseline current; changes while paused are dropped
            if events:
                st["dropped_paused"] = st.get("dropped_paused", 0) + len(events)
            return bool(events) or was_ok != st["watch_ok"]
        if not events:
            return was_ok != st["watch_ok"]
        if self._busy(t):
            t.held = events  # keep them for when the last run is done
            return False
        if s.get("run_mode") == RUN_MODES[1]:
            groups = [events]
        else:
            groups = [[e] for e in events]
        for g in groups:
            if self._limit_reached(t):
                st["done"] = True
                break
            evs = sorted({e for e, _ in g})
            self._fire(t, {"event": evs[0] if len(evs) == 1 else "+".join(evs), "paths": [p for _, p in g],
                           "events": [e for e, _ in g]})
        if self._limit_reached(t):
            st["done"] = True
        return True

    def _fire(self, t: Trigger, info: dict, count: bool = True) -> None:
        st = t.state
        index = st["fired"] + 1 if count else 0
        now = self.now()
        run = {"trigger": t.key, "kind": t.kind, "title": t.title, "index": index,
               "time": now.isoformat(timespec="seconds"), "seed": self.rng.randint(0, 2**53 - 1), **info}
        prompt = copy.deepcopy(t.prompt)
        prompt[t.node_id].setdefault("inputs", {})[RUN_KEY] = json.dumps(run)
        if t.settings.get("new_seeds"):
            reroll_seeds(prompt, self.rng, skip=t.node_id)
        rec = {"time": run["time"], "index": index, "event": info.get("event"),
               "paths": info.get("paths", [])[:5], "status": "queued"}
        try:
            pid = self.queue_fn(prompt, t.extra_pnginfo)
        except Exception as e:
            st["last_error"] = f"{now:%H:%M:%S} queue failed: {e}"
            log.warning("CrateUtils-Comfy: %s could not queue the workflow: %s", t.key, e)
            if count:  # a failed run still uses up its slot, so a broken workflow can't retry forever
                st["fired"] = index
            rec.update(status="not queued", error=str(e)[:300])
            st["runs"] = (st.get("runs", []) + [rec])[-HISTORY:]
            self._log_row(t, rec, "", "not queued", None, [], str(e))
            return
        st.pop("last_error", None)
        if count:
            st["fired"] = index
        st["last_fired"], st["last_prompt_id"] = run["time"], pid
        rec["prompt_id"] = pid
        st["runs"] = (st.get("runs", []) + [rec])[-HISTORY:]
        with self.lock:
            entry = self.tracked.setdefault(pid, {"since": self.mono()})
            entry.update(key=t.key, fired_at=run["time"], index=index, event=info.get("event"),
                         paths=info.get("paths", []))
        log.info("CrateUtils-Comfy: %s fired run %s (%s) -> prompt %s", t.key, index, info.get("event"), pid)

    # ----- run tracking -------------------------------------------------------------
    def _track(self) -> bool:
        m = self.mono()
        if m - self.last_track < TRACK_POLL or not self.tracked:
            return False
        self.last_track = m
        changed = False
        for pid, entry in list(self.tracked.items()):
            h = self.history_fn(pid)
            if h is None:
                age = m - entry["since"]
                if age > TRACK_GIVE_UP or (age > TRACK_LOST_AFTER and not self.busy_fn(pid)):
                    self._finish(pid, entry, {"status": "cancelled", "duration": None, "outputs": [], "error": ""})
                    changed = True
                continue
            res = summarize_history(h)
            if res["status"] in ("success", "error", "interrupted"):
                self._finish(pid, entry, res)
                changed = True
        return changed

    def _finish(self, pid: str, entry: dict, res: dict) -> None:
        with self.lock:
            self.tracked.pop(pid, None)
            t = self.triggers.get(entry.get("key", ""))
        if t is not None:
            for rec in t.state.get("runs", []):
                if rec.get("prompt_id") == pid:
                    rec.update(status=res["status"], duration=res["duration"], outputs=res["outputs"][:10])
                    if res["error"]:
                        rec["error"] = res["error"][:300]
            self._log_row(t, entry, pid, res["status"], res["duration"], res["outputs"], res["error"])
        if entry.get("notify") and res["status"] in ("error", "interrupted") and self.notify_fn:
            try:
                self.notify_fn(entry["notify"], pid, res["error"] or res["status"])
            except Exception as e:
                log.warning("CrateUtils-Comfy: failure notification for %s not sent: %s", pid, e)

    def _log_row(self, t: Trigger, entry: dict, pid: str, status: str, dur, outputs: list, error: str) -> None:
        if not self.log_path:
            return
        row = [entry.get("fired_at") or entry.get("time", ""), t.key, t.title, t.kind, entry.get("index", ""),
               entry.get("event", ""), " | ".join(entry.get("paths", [])), pid, status,
               "" if dur is None else dur, " | ".join(outputs), (error or "").replace("\n", " ")[:500]]
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            new = not self.log_path.is_file()
            with open(self.log_path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(LOG_COLUMNS)
                w.writerow(row)
        except OSError as e:
            log.warning("CrateUtils-Comfy: can't write the run log %s: %s", self.log_path, e)

    # ----- status / persistence -----------------------------------------------------
    def summary(self, t: Trigger) -> str:
        s = t.settings
        if t.kind == "schedule":
            return sch.describe(t.sched) if t.sched else ""
        if t.kind == "webhook":
            lim = f", max {s['max_runs']} runs" if s.get("max_runs") else ""
            return f"webhook /crateutils-comfy/hook/{t.state.get('hook_id', '')}{lim}"
        if t.kind == "batch":
            return f"{t.state.get('total', 0)} files in {s['folder']} ({s['sort']})"
        what = s.get("folder") or s.get("file")
        lim = f", max {s['max_runs']} runs" if s.get("max_runs") else ""
        return f"watching {what} for {s['events']}{lim}"

    def status(self, t: Trigger, secrets_too: bool = True) -> dict:
        st = t.state
        if t.kind == "schedule":
            limit = t.settings.get("count")
        elif t.kind == "batch":
            limit = st.get("total")
        else:
            limit = t.settings.get("max_runs")
        out = {"key": t.key, "kind": t.kind, "title": t.title, "node_id": t.node_id, "local_id": t.local_id,
               "graph_id": t.graph_id, "summary": self.summary(t), "armed_at": st.get("armed_at"),
               "fired": st.get("fired", 0), "limit": limit or 0, "next_at": st.get("next_at"),
               "done": bool(st.get("done")), "paused": bool(st.get("paused")), "last_fired": st.get("last_fired"),
               "last_error": st.get("last_error"), "missed": st.get("missed", 0),
               "watch_ok": st.get("watch_ok", True), "held": len(t.held) + len(t.inbox),
               "pending": len(st.get("pending_files") or []), "runs": st.get("runs", [])}
        if t.kind == "webhook":
            out["hook_path"] = f"/crateutils-comfy/hook/{st.get('hook_id', '')}"
            if secrets_too:
                out["token"] = st.get("token")
        return out

    def list(self) -> list[dict]:
        with self.lock:
            return [self.status(t) for t in self.triggers.values()]

    def save(self) -> None:
        if not self.store:
            return
        with self.lock:
            data = {"version": 1, "triggers": [t.to_json() for t in self.triggers.values()]}
        self.store.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self.store)

    def load(self) -> int:
        if not self.store or not self.store.is_file():
            return 0
        try:
            data = json.loads(self.store.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("CrateUtils-Comfy: can't read %s: %s", self.store, e)
            return 0
        n = 0
        for d in data.get("triggers", []):
            t = Trigger(d["key"], d["kind"], d["node_id"], d["prompt"], d.get("extra_pnginfo"), d["settings"],
                        d.get("local_id", ""), d.get("graph_id", ""), d.get("state", {}), d.get("title", ""))
            try:
                self._build(t, fresh=False)
            except Exception as e:
                t.state["last_error"] = f"not restored: {e}"
                log.warning("CrateUtils-Comfy: trigger %s not restored: %s", t.key, e)
            with self.lock:
                self.triggers[t.key] = t
            n += 1
        return n

    # ----- thread -------------------------------------------------------------------
    def start(self, interval: float = 0.5) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop():
            while not self._stop.wait(interval):
                try:
                    self.tick()
                except Exception:
                    log.exception("CrateUtils-Comfy: trigger loop error")

        self._thread = threading.Thread(target=loop, name="CrateUtils-Comfy-triggers", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
