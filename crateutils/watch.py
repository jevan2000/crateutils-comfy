"""Polling folder/file watcher.

Polling (not OS notifications) so it works the same on local disks, USB drives and
network shares. Files that exist when a watch starts are the baseline and do not
trigger. A new or changed file is reported only after its size and modification time
have stayed the same for ``settle`` seconds, so a file still being copied or rendered
is not picked up half-written.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from typing import Callable

EVENTS = ["added", "modified", "removed"]
FOLDER_EVENTS = {
    "added": ("added",),
    "added or modified": ("added", "modified"),
    "modified": ("modified",),
    "removed": ("removed",),
    "any change": ("added", "modified", "removed"),
}
FILE_EVENTS = {
    "changed (created or modified)": ("added", "modified"),
    "modified": ("modified",),
    "created": ("added",),
    "removed": ("removed",),
    "any change": ("added", "modified", "removed"),
}
# Temp and system files that should never start a workflow.
IGNORE = ("*.tmp", "*.temp", "*.part", "*.partial", "*.crdownload", "*.download", "*.!ut", "~$*", ".~*",
          "*.swp", "*~", "Thumbs.db", "desktop.ini", ".DS_Store", "._*")

Sig = tuple  # (size, mtime_ns)


def split_patterns(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"[;,|\n]+", text or "") if p.strip()]


def pattern_matcher(patterns: str) -> Callable[[str], bool]:
    pats = [p.lower() for p in split_patterns(patterns)]
    ign = [p.lower() for p in IGNORE]

    def match(name: str) -> bool:
        n = name.lower()
        if any(fnmatch.fnmatchcase(n, p) for p in ign):
            return False
        return not pats or any(fnmatch.fnmatchcase(n, p) for p in pats)

    return match


def snapshot(folder: str, match: Callable[[str], bool], recursive: bool = False) -> dict[str, Sig]:
    out: dict[str, Sig] = {}
    stack = [folder]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if recursive and not e.name.startswith("."):
                            stack.append(e.path)
                    elif e.is_file() and match(e.name):
                        st = e.stat()
                        out[os.path.normpath(e.path)] = (st.st_size, st.st_mtime_ns)
                except OSError:
                    continue
    return out


@dataclass
class _Pending:
    event: str
    sig: Sig
    since: float


class Watcher:
    def __init__(self, folder: str, match: Callable[[str], bool], recursive: bool = False,
                 events=("added", "modified"), settle: float = 2.0, now: float = 0.0):
        self.folder, self.match, self.recursive = folder, match, recursive
        self.events, self.settle = tuple(events), float(settle)
        self.prev = snapshot(folder, match, recursive)
        self.pending: dict[str, _Pending] = {}
        self.folder_ok = os.path.isdir(folder)

    def poll(self, now: float) -> list[tuple[str, str]]:
        """Changes that have settled since the last poll, as (event, path), oldest first."""
        self.folder_ok = os.path.isdir(self.folder)
        if not self.folder_ok:  # unplugged drive / share offline: keep the baseline, report nothing
            return []
        cur = snapshot(self.folder, self.match, self.recursive)
        ready: list[tuple[float, str, str]] = []
        for p, sig in cur.items():
            old = self.prev.get(p)
            if old == sig:
                continue
            pe = self.pending.get(p)
            if pe is None:
                self.pending[p] = _Pending("added" if old is None else "modified", sig, now)
            elif pe.sig != sig:
                pe.sig, pe.since = sig, now
        for p in self.prev.keys() - cur.keys():
            pe = self.pending.pop(p, None)
            if pe is None or pe.event != "added":  # a file that appeared and vanished again is not news
                ready.append((now, "removed", p))
        for p in [p for p in self.pending if p not in cur]:
            self.pending.pop(p)
        for p, pe in list(self.pending.items()):
            if cur.get(p) == pe.sig and now - pe.since >= self.settle:
                ready.append((pe.since, pe.event, p))
                del self.pending[p]
        self.prev = cur
        ready.sort(key=lambda r: (r[0], r[2]))
        return [(ev, p) for _, ev, p in ready if ev in self.events]


def newest(folder: str, match: Callable[[str], bool], recursive: bool = False) -> str:
    snap = snapshot(folder, match, recursive)
    if not snap:
        return ""
    return max(snap.items(), key=lambda kv: kv[1][1])[0]
