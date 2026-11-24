"""Schedules: a start date/time, an interval, a run count, optional weekdays and an end.

All times are naive local wall-clock times, so "every day at 09:00" stays at 09:00
across daylight-saving changes.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

UNITS = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400, "weeks": 604800}
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MISSED = ["skip missed runs", "run once for missed runs"]
GRACE = 90.0  # seconds late before a run counts as missed

_DATE_FORMATS = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                 "%Y-%m-%d %I:%M %p", "%Y-%m-%d %I:%M%p", "%Y-%m-%d", "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p",
                 "%m/%d/%Y"]
_TIME_FORMATS = ["%H:%M:%S", "%H:%M", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p"]


def parse_when(text: str, now: datetime) -> datetime:
    """A start time: empty or "now", a date and time, a date (midnight), or a time of
    day alone (today, or tomorrow if that time has passed)."""
    s = (text or "").strip()
    if not s or s.lower() == "now":
        return now.replace(microsecond=0)
    m = re.fullmatch(r"(?i)in\s+(\d+(?:\.\d+)?)\s*(s|sec|seconds?|m|min|minutes?|h|hours?|d|days?)", s)
    if m:
        n, u = float(m.group(1)), m.group(2).lower()
        secs = n * (1 if u.startswith("s") else 60 if u.startswith("m") else 3600 if u.startswith("h") else 86400)
        return (now + timedelta(seconds=secs)).replace(microsecond=0)
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(s.upper() if "%p" in f else s, f)
        except ValueError:
            pass
    for f in _TIME_FORMATS:
        try:
            t = datetime.strptime(s.upper(), f).time()
        except ValueError:
            continue
        at = datetime.combine(now.date(), t)
        return at if at >= now.replace(microsecond=0) else at + timedelta(days=1)
    raise ValueError(f"can't read {text!r} as a date/time; use YYYY-MM-DD HH:MM, HH:MM, 'now' or 'in 10 minutes'")


def parse_end(text: str, now: datetime) -> datetime | None:
    s = (text or "").strip()
    if not s or s.lower() in ("never", "none"):
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):  # a date alone means the end of that day
        return datetime.strptime(s, "%Y-%m-%d") + timedelta(days=1, microseconds=-1)
    return parse_when(s, now)


def parse_days(text: str) -> frozenset[int] | None:
    """Weekdays: "", "every day", "weekdays", "weekends", "mon,wed,fri", "mon-fri"."""
    s = (text or "").strip().lower()
    if s in ("", "every day", "daily", "all", "any", "*"):
        return None
    if s in ("weekdays", "workdays"):
        return frozenset(range(5))
    if s in ("weekends", "weekend"):
        return frozenset({5, 6})
    out: set[int] = set()
    for tok in re.split(r"[,\s;/]+", s):
        if not tok:
            continue
        if "-" in tok:
            a, b = (_day(x) for x in tok.split("-", 1))
            i = a
            while True:
                out.add(i)
                if i == b:
                    break
                i = (i + 1) % 7
        else:
            out.add(_day(tok))
    return frozenset(out)


def _day(tok: str) -> int:
    t = tok.strip()[:3]
    if t not in DAY_NAMES:
        raise ValueError(f"unknown weekday {tok!r}; use mon, tue, wed, thu, fri, sat, sun")
    return DAY_NAMES.index(t)


@dataclass(frozen=True)
class Schedule:
    start: datetime
    every: float = 0.0          # seconds between runs; 0 = run once
    count: int = 1              # runs to make; 0 = no limit
    days: frozenset[int] | None = None
    end: datetime | None = None

    def occurrence(self, k: int) -> datetime:
        return self.start + timedelta(seconds=self.every * k)

    def next_at_or_after(self, t: datetime, k_from: int = 0) -> tuple[int, datetime] | None:
        """The first occurrence with index >= k_from at or after ``t`` on an allowed day."""
        if self.every <= 0 or self.count == 1:
            if k_from > 0:
                return None
            at = self.start
            ok = at >= t and self._day_ok(at) and (self.end is None or at <= self.end)
            return (0, at) if ok else None
        k = k_from
        if t > self.start:
            k = max(k, math.ceil((t - self.start).total_seconds() / self.every - 1e-9))
        for _ in range(100_000):
            at = self.occurrence(k)
            if self.end is not None and at > self.end:
                return None
            if at >= t and self._day_ok(at):
                return k, at
            k += 1
        return None

    def _day_ok(self, at: datetime) -> bool:
        return self.days is None or at.weekday() in self.days


def build(start: str, every: float, unit: str, count: int, days: str, end: str, now: datetime) -> Schedule:
    if unit not in UNITS:
        raise ValueError(f"unit must be one of {', '.join(UNITS)}")
    if every < 0 or count < 0:
        raise ValueError("every and count can't be negative")
    secs = float(every) * UNITS[unit]
    if count != 1 and secs <= 0:
        raise ValueError("set 'every' above 0 to run more than once")
    if 0 < secs < 10:
        raise ValueError("the shortest interval is 10 seconds")
    return Schedule(parse_when(start, now), secs, int(count), parse_days(days), parse_end(end, now))


def describe(s: Schedule) -> str:
    def fmt(d):
        return d.strftime("%Y-%m-%d %H:%M:%S")

    parts = [f"from {fmt(s.start)}"]
    if s.every > 0 and s.count != 1:
        for unit, n in reversed(UNITS.items()):
            if s.every % n == 0:
                v = int(s.every // n)
                parts.append(f"every {v} {unit if v != 1 else unit[:-1]}")
                break
        parts.append("no run limit" if s.count == 0 else f"{s.count} runs")
    else:
        parts.append("once")
    if s.days is not None:
        parts.append("on " + ",".join(DAY_NAMES[d] for d in sorted(s.days)))
    if s.end is not None:
        parts.append(f"until {fmt(s.end)}")
    return ", ".join(parts)
