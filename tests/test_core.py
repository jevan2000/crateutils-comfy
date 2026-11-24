"""Core tests (no ComfyUI needed): schedules, watcher, trigger manager, path policy.

    python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crateutils import paths, triggers, watch  # noqa: E402
from crateutils import schedule as sch  # noqa: E402

NOW = datetime(2026, 11, 23, 10, 0, 0)  # a Monday


class Parse(unittest.TestCase):
    def test_when(self):
        self.assertEqual(sch.parse_when("now", NOW), NOW)
        self.assertEqual(sch.parse_when("", NOW), NOW)
        self.assertEqual(sch.parse_when("2026-12-24 09:30", NOW), datetime(2026, 12, 24, 9, 30))
        self.assertEqual(sch.parse_when("14:30", NOW), datetime(2026, 11, 23, 14, 30))
        self.assertEqual(sch.parse_when("09:00", NOW), datetime(2026, 11, 24, 9, 0))  # passed: tomorrow
        self.assertEqual(sch.parse_when("2:15 pm", NOW), datetime(2026, 11, 23, 14, 15))
        self.assertEqual(sch.parse_when("in 10 minutes", NOW), NOW + timedelta(minutes=10))
        self.assertEqual(sch.parse_when("in 2h", NOW), NOW + timedelta(hours=2))
        with self.assertRaises(ValueError):
            sch.parse_when("tomorrowish", NOW)

    def test_days(self):
        self.assertIsNone(sch.parse_days(""))
        self.assertEqual(sch.parse_days("weekdays"), frozenset(range(5)))
        self.assertEqual(sch.parse_days("mon-wed, sat"), frozenset({0, 1, 2, 5}))
        self.assertEqual(sch.parse_days("fri-mon"), frozenset({4, 5, 6, 0}))
        with self.assertRaises(ValueError):
            sch.parse_days("funday")

    def test_end_date_is_end_of_day(self):
        self.assertEqual(sch.parse_end("2026-11-30", NOW), datetime(2026, 11, 30, 23, 59, 59, 999999))


class ScheduleMath(unittest.TestCase):
    def test_once(self):
        s = sch.build("14:00", 0, "hours", 1, "", "", NOW)
        self.assertEqual(s.next_at_or_after(NOW), (0, datetime(2026, 11, 23, 14)))
        self.assertIsNone(s.next_at_or_after(NOW, 1))

    def test_every_and_count(self):
        s = sch.build("2026-11-23 09:00", 1, "hours", 5, "", "", NOW)
        self.assertEqual(s.next_at_or_after(NOW), (1, datetime(2026, 11, 23, 10)))
        self.assertEqual(s.next_at_or_after(NOW + timedelta(minutes=1)), (2, datetime(2026, 11, 23, 11)))

    def test_days_filter_skips_weekend(self):
        s = sch.build("2026-11-27 08:00", 1, "days", 0, "weekdays", "", NOW)  # Friday
        k, at = s.next_at_or_after(datetime(2026, 11, 27, 9), 1)
        self.assertEqual(at, datetime(2026, 11, 30, 8))  # Monday

    def test_end(self):
        s = sch.build("now", 1, "hours", 0, "", "2026-11-23 12:30", NOW)
        self.assertEqual(s.next_at_or_after(NOW + timedelta(hours=1, minutes=1))[1], NOW + timedelta(hours=2))
        self.assertIsNone(s.next_at_or_after(NOW + timedelta(hours=2, minutes=31)))

    def test_bad(self):
        with self.assertRaises(ValueError):
            sch.build("now", 0, "hours", 3, "", "", NOW)  # repeats need an interval
        with self.assertRaises(ValueError):
            sch.build("now", 2, "seconds", 0, "", "", NOW)  # too fast
        self.assertIn("every 2 hours", sch.describe(sch.build("now", 2, "hours", 4, "", "", NOW)))


class Clock:
    def __init__(self):
        self.t = NOW
        self.m = 1000.0

    def now(self):
        return self.t

    def mono(self):
        return self.m

    def advance(self, secs):
        self.t += timedelta(seconds=secs)
        self.m += secs


def wf(class_type, **inputs):
    return {"1": {"class_type": class_type, "inputs": inputs},
            "2": {"class_type": "KSampler", "inputs": {"seed": 5, "model": ["3", 0]}},
            "3": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "x"}}}


class ManagerSchedule(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.queued = []
        self.m = triggers.Manager(None, lambda p, e: self.queued.append(p) or f"pid{len(self.queued)}",
                                  now_fn=self.clock.now, mono_fn=self.clock.mono)

    def sched(self, **kw):
        base = dict(start="now", every=1.0, unit="minutes", count=3, days="", end="",
                    missed=sch.MISSED[0], new_seeds=True)
        base.update(kw)
        return wf("CUScheduler", **base)

    def test_fires_count_times_then_done(self):
        st = self.m.arm("k", "1", self.sched())
        self.assertEqual(st["fired"], 0)
        for _ in range(10):
            self.m.tick()
            self.clock.advance(30)
        self.assertEqual(len(self.queued), 3)
        runs = [triggers.read_run(p, "1") for p in self.queued]
        self.assertEqual([r["index"] for r in runs], [1, 2, 3])
        self.assertTrue(self.m.list()[0]["done"])
        seeds = {p["2"]["inputs"]["seed"] for p in self.queued}
        self.assertEqual(len(seeds), 3)  # new_seeds rerolled every run
        self.assertNotIn(triggers.RUN_KEY, self.m.triggers["k"].prompt["1"]["inputs"])  # stored copy is clean

    def test_missed_runs_skipped(self):
        self.m.arm("k", "1", self.sched(count=0))
        self.m.tick()  # run 1 now
        self.clock.advance(600)  # asleep for 10 minutes
        self.m.tick()
        self.assertEqual(len(self.queued), 1)
        self.assertEqual(self.m.list()[0]["missed"], 1)
        self.clock.advance(60)
        self.m.tick()
        self.assertEqual(len(self.queued), 2)

    def test_missed_run_once(self):
        self.m.arm("k", "1", self.sched(count=0, missed=sch.MISSED[1]))
        self.m.tick()
        self.clock.advance(600)
        self.m.tick()
        self.m.tick()
        self.assertEqual(len(self.queued), 2)

    def test_linked_setting_refused(self):
        p = self.sched()
        p["1"]["inputs"]["count"] = ["9", 0]
        with self.assertRaises(ValueError):
            self.m.arm("k", "1", p)

    def test_queue_failure_recorded(self):
        def boom(p, e):
            raise RuntimeError("refused")

        m = triggers.Manager(None, boom, now_fn=self.clock.now, mono_fn=self.clock.mono)
        m.arm("k", "1", self.sched(count=1))
        m.tick()
        self.assertIn("refused", m.list()[0]["last_error"])

    def test_skip_if_busy(self):
        m = triggers.Manager(None, lambda p, e: self.queued.append(p) or "pid", busy_fn=lambda pid: True,
                             now_fn=self.clock.now, mono_fn=self.clock.mono)
        m.arm("k", "1", self.sched(count=0, skip_if_busy=True))
        m.tick()
        self.clock.advance(60)
        m.tick()
        self.assertEqual(len(self.queued), 1)

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as d:
            store = Path(d) / "t.json"
            m = triggers.Manager(store, lambda p, e: "pid", now_fn=self.clock.now, mono_fn=self.clock.mono)
            m.arm("k", "1", self.sched(count=0), {"workflow": {"id": "w"}})
            m.tick()
            m2 = triggers.Manager(store, lambda p, e: "pid", now_fn=self.clock.now, mono_fn=self.clock.mono)
            self.assertEqual(m2.load(), 1)
            self.assertEqual(m2.list()[0]["fired"], 1)
            self.assertEqual(m2.triggers["k"].extra_pnginfo, {"workflow": {"id": "w"}})


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.root = Path(self.d.name)
        (self.root / "old.png").write_bytes(b"x")

    def tearDown(self):
        self.d.cleanup()

    def test_added_waits_for_settle_and_ignores_baseline(self):
        w = watch.Watcher(str(self.root), watch.pattern_matcher("*.png"), events=("added", "modified"), settle=2)
        (self.root / "new.png").write_bytes(b"abc")
        (self.root / "skip.txt").write_bytes(b"abc")
        (self.root / "dl.png.crdownload").write_bytes(b"abc")
        self.assertEqual(w.poll(0.0), [])  # not settled yet
        self.assertEqual(w.poll(1.0), [])
        ev = w.poll(2.5)
        self.assertEqual(ev, [("added", str(self.root / "new.png"))])
        self.assertEqual(w.poll(5.0), [])

    def test_growing_file_restarts_settle(self):
        w = watch.Watcher(str(self.root), watch.pattern_matcher(""), settle=2)
        f = self.root / "big.png"
        f.write_bytes(b"a")
        w.poll(0.0)
        f.write_bytes(b"ab")
        os.utime(f, ns=(time.time_ns(), time.time_ns() + 5_000_000))
        self.assertEqual(w.poll(1.5), [])
        self.assertEqual(w.poll(3.0), [])  # changed at 1.5, needs until 3.5
        self.assertEqual(w.poll(3.6), [("added", str(f))])

    def test_modified_and_removed(self):
        w = watch.Watcher(str(self.root), watch.pattern_matcher(""), events=("modified", "removed"), settle=0)
        f = self.root / "old.png"
        f.write_bytes(b"changed!")
        self.assertEqual(w.poll(1.0), [("modified", str(f))])
        f.unlink()
        self.assertEqual(w.poll(2.0), [("removed", str(f))])

    def test_recursive(self):
        (self.root / "sub").mkdir()
        w = watch.Watcher(str(self.root), watch.pattern_matcher("*.png"), recursive=True, settle=0)
        (self.root / "sub" / "a.png").write_bytes(b"1")
        self.assertEqual(w.poll(1.0), [("added", str(self.root / "sub" / "a.png"))])

    def test_manager_folder_batch_and_max_runs(self):
        clock = Clock()
        queued = []
        m = triggers.Manager(None, lambda p, e: queued.append(p) or "pid", now_fn=clock.now, mono_fn=clock.mono)
        p = wf("CUFolderWatcher", folder=str(self.root), patterns="*.png", recursive=False, events="added",
               settle=0.0, poll=1.0, max_runs=2, run_mode=triggers.RUN_MODES[0], new_seeds=False)
        m.arm("f", "1", p)
        for n in ("a.png", "b.png", "c.png"):
            (self.root / n).write_bytes(b"1")
        clock.advance(1.5)
        m.tick()
        self.assertEqual(len(queued), 2)  # max_runs
        runs = [triggers.read_run(q, "1") for q in queued]
        self.assertEqual([os.path.basename(r["paths"][0]) for r in runs], ["a.png", "b.png"])
        self.assertTrue(m.list()[0]["done"])
        self.assertEqual(queued[0]["2"]["inputs"]["seed"], 5)  # new_seeds off

    def test_file_watcher(self):
        clock = Clock()
        queued = []
        m = triggers.Manager(None, lambda p, e: queued.append(p) or "pid", now_fn=clock.now, mono_fn=clock.mono)
        target = self.root / "prompt.txt"
        p = wf("CUFileWatcher", file=str(target), events="changed (created or modified)", settle=0.0, poll=1.0,
               max_runs=0)
        m.arm("f", "1", p)
        (self.root / "other.txt").write_text("x")
        target.write_text("hello")
        clock.advance(1.5)
        m.tick()
        self.assertEqual(len(queued), 1)
        r = triggers.read_run(queued[0], "1")
        self.assertEqual((r["event"], r["paths"]), ("added", [str(target)]))


class PathPolicy(unittest.TestCase):
    def test_inside_and_outside(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "input"
            root.mkdir()
            self.assertEqual(paths.check("a/b.png", [root], root), Path(os.path.realpath(root / "a" / "b.png")))
            with self.assertRaises(ValueError):
                paths.check("../escape.png", [root], root)
            with self.assertRaises(ValueError):
                paths.check(str(Path(d) / "elsewhere"), [root], root)

    def test_config_roots(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            cfg.write_text(json.dumps({"allowed_folders": [d]}))
            self.assertIn(Path(os.path.realpath(d)), paths.config_roots(cfg))


if __name__ == "__main__":
    unittest.main()
