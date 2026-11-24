"""Tests for 0.2 (no ComfyUI needed): pause, webhook inbox, folder batch, run tracking + CSV log,
failure notifications, notification routing, copy/move, audio export.

    python -m unittest discover -s tests -v
"""

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from crateutils import exporter as ex  # noqa: E402
from crateutils import fileops, notify, triggers  # noqa: E402
from test_core import Clock, wf  # noqa: E402


def sched(**kw):
    base = dict(start="now", every=1.0, unit="minutes", count=0, days="", end="", missed="skip missed runs",
                new_seeds=True)
    base.update(kw)
    return wf("CUScheduler", **base)


def hist(status="success", error=None, start=1000, end=3500, files=("a.png",)):
    msgs = [["execution_start", {"timestamp": start}]]
    if error:
        msgs.append(["execution_error", {"timestamp": end, "node_type": "KSampler", "node_id": "2",
                                         "exception_message": error}])
    else:
        msgs.append(["execution_success", {"timestamp": end}])
    return {"status": {"status_str": status, "completed": status == "success", "messages": msgs},
            "outputs": {"9": {"images": [{"filename": f, "subfolder": "out", "type": "output"} for f in files]}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.queued = []
        self.histories = {}
        self.sent = []
        self.d = tempfile.TemporaryDirectory()
        self.dir = Path(self.d.name)
        self.m = triggers.Manager(None, self.q, now_fn=self.clock.now, mono_fn=self.clock.mono,
                                  history_fn=self.histories.get, notify_fn=lambda *a: self.sent.append(a),
                                  log_path=self.dir / "runs.csv")

    def tearDown(self):
        self.d.cleanup()

    def q(self, prompt, extra):
        self.queued.append(prompt)
        return f"pid{len(self.queued)}"


class Pause(Base):
    def test_paused_schedule_skips_then_resumes(self):
        self.m.arm("k", "1", sched())
        self.m.tick()
        self.assertEqual(len(self.queued), 1)
        self.m.pause("k", True)
        self.clock.advance(60)
        self.m.tick()
        self.clock.advance(60)
        self.m.tick()
        self.assertEqual(len(self.queued), 1)
        self.m.pause("k", False)
        self.clock.advance(60)
        self.m.tick()
        self.assertEqual(len(self.queued), 2)

    def test_paused_watcher_drops_changes(self):
        p = wf("CUFolderWatcher", folder=str(self.dir), patterns="*.png", recursive=False, events="added",
               settle=0.0, poll=1.0, max_runs=0, run_mode=triggers.RUN_MODES[0])
        self.m.arm("w", "1", p)
        self.m.pause("w", True)
        (self.dir / "a.png").write_bytes(b"1")
        self.clock.advance(1.5)
        self.m.tick()
        self.m.pause("w", False)
        self.clock.advance(1.5)
        self.m.tick()
        self.assertEqual(self.queued, [])  # a.png arrived while paused
        (self.dir / "b.png").write_bytes(b"1")
        self.clock.advance(1.5)
        self.m.tick()
        self.assertEqual(len(self.queued), 1)


class Webhook(Base):
    def test_calls_queue_with_payload_and_max_runs(self):
        st = self.m.arm("h", "1", wf("CUWebhook", max_runs=2))
        self.assertEqual(len(st["token"]), 32)
        hook_id = st["hook_path"].rsplit("/", 1)[1]
        self.assertIs(self.m.find_hook(hook_id).key, "h")
        for i in range(3):
            self.m.request_fire("h", {"event": "webhook", "paths": [], "payload": {"text": f"hi {i}"}})
        self.m.request_fire("h")  # a test run never counts
        self.m.tick()
        runs = [triggers.read_run(p, "1") for p in self.queued]
        self.assertEqual([r["payload"]["text"] for r in runs if r["event"] == "webhook"], ["hi 0", "hi 1"])
        self.assertTrue(self.m.list()[0]["done"])

    def test_rearm_keeps_url(self):
        a = self.m.arm("h", "1", wf("CUWebhook", max_runs=0))
        b = self.m.arm("h", "1", wf("CUWebhook", max_runs=5))
        self.assertEqual((a["hook_path"], a["token"]), (b["hook_path"], b["token"]))


class Batch(Base):
    def test_one_run_per_file_in_order(self):
        for n in ("b.png", "a.png", "c.png", "x.txt"):
            (self.dir / n).write_bytes(b"1")
        p = wf("CUFolderBatch", folder=str(self.dir), patterns="*.png", recursive=False, sort="name", max_files=0,
               new_seeds=False, skip_if_busy=False)
        st = self.m.arm("b", "1", p)
        self.assertEqual(st["limit"], 3)
        self.m.tick()
        runs = [triggers.read_run(q, "1") for q in self.queued]
        self.assertEqual([os.path.basename(r["paths"][0]) for r in runs], ["a.png", "b.png", "c.png"])
        self.assertEqual(runs[0]["total"], 3)
        self.assertTrue(self.m.list()[0]["done"])

    def test_one_at_a_time_when_busy(self):
        for n in ("a.png", "b.png"):
            (self.dir / n).write_bytes(b"1")
        busy = {"on": True}
        m = triggers.Manager(None, self.q, busy_fn=lambda pid: busy["on"], now_fn=self.clock.now,
                             mono_fn=self.clock.mono)
        m.arm("b", "1", wf("CUFolderBatch", folder=str(self.dir), patterns="", recursive=False, sort="name",
                           max_files=0, new_seeds=False, skip_if_busy=True))
        m.tick()
        m.tick()
        self.assertEqual(len(self.queued), 1)
        busy["on"] = False
        m.tick()
        self.assertEqual(len(self.queued), 2)

    def test_empty_folder_refused(self):
        with self.assertRaises(ValueError):
            self.m.arm("b", "1", wf("CUFolderBatch", folder=str(self.dir), patterns="*.png", recursive=False,
                                    sort="name", max_files=0))


class Tracking(Base):
    def test_success_logged_with_duration_and_outputs(self):
        self.m.arm("k", "1", sched(count=1), title="Hourly")
        self.m.tick()
        self.histories["pid1"] = hist()
        self.clock.advance(5)
        self.m.tick()
        run = self.m.list()[0]["runs"][0]
        self.assertEqual((run["status"], run["duration"], run["outputs"]), ("success", 2.5, ["out/a.png"]))
        rows = list(csv.DictReader(open(self.dir / "runs.csv", encoding="utf-8")))
        self.assertEqual(rows[0]["title"], "Hourly")
        self.assertEqual(rows[0]["status"], "success")
        self.assertEqual(rows[0]["duration_s"], "2.5")
        self.assertEqual(self.sent, [])

    def test_failure_notifies_external_tracker(self):
        self.m.track_external("ext1", {"destination": "toast"})
        self.histories["ext1"] = hist("error", "out of memory")
        self.clock.advance(5)
        self.m.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("out of memory", self.sent[0][2])
        self.assertEqual(self.m.tracked, {})

    def test_lost_run_marked_cancelled(self):
        self.m.arm("k", "1", sched(count=1))
        self.m.tick()
        self.clock.advance(5)
        self.m.tick()
        self.assertEqual(self.m.list()[0]["runs"][0]["status"], "queued")
        self.clock.advance(triggers.TRACK_LOST_AFTER + 5)
        self.m.tick()
        self.assertEqual(self.m.list()[0]["runs"][0]["status"], "cancelled")

    def test_queue_refusal_logged(self):
        def boom(p, e):
            raise RuntimeError("bad node")

        m = triggers.Manager(None, boom, now_fn=self.clock.now, mono_fn=self.clock.mono,
                             log_path=self.dir / "runs.csv")
        m.arm("k", "1", sched(count=1))
        m.tick()
        rows = list(csv.DictReader(open(self.dir / "runs.csv", encoding="utf-8")))
        self.assertEqual((rows[0]["status"], rows[0]["error"]), ("not queued", "bad node"))

    def test_summarize_interrupted(self):
        h = {"status": {"status_str": "error", "messages": [["execution_start", {"timestamp": 1}],
                                                             ["execution_interrupted", {"timestamp": 2}]]}}
        self.assertEqual(triggers.summarize_history(h)["status"], "interrupted")


class NotifyRouting(unittest.TestCase):
    def test_resolve(self):
        named = {"phone": "https://ntfy.sh/x", "team": "https://discord.com/api/webhooks/1/abc"}
        self.assertEqual(notify.resolve("toast, phone,team", named),
                         [("toast", ""), ("ntfy", "https://ntfy.sh/x"), ("discord", named["team"])])
        with self.assertRaises(ValueError):
            notify.resolve("nobody", named)
        with self.assertRaises(ValueError):
            notify.resolve("", named)

    def test_send_reports_failures_without_raising(self):
        with mock.patch.object(notify, "_post", side_effect=OSError("offline")):
            lines = notify.send([("ntfy", "https://ntfy.sh/x")], "t", "m")
        self.assertIn("FAILED", lines[0])

    def test_discord_multipart_with_image(self):
        seen = {}
        with mock.patch.object(notify, "_post", side_effect=lambda u, d, h, **k: seen.update(d=d, h=h)):
            notify.send_discord("https://discord.com/api/webhooks/1/a", "Title", "msg", b"PNGDATA")
        self.assertIn("multipart/form-data", seen["h"]["Content-Type"])
        self.assertIn(b"PNGDATA", seen["d"])
        self.assertIn(b'"parse": []', seen["d"])  # never pings @everyone

    def test_ntfy_image_uses_put(self):
        seen = {}
        with mock.patch.object(notify, "_post", side_effect=lambda u, d, h, method="POST": seen.update(m=method, h=h)):
            notify.send_ntfy("https://ntfy.sh/x", "T", "line\nbreak", b"img")
        self.assertEqual(seen["m"], "PUT")
        self.assertNotIn("\n", seen["h"]["Message"])


class CopyMove(unittest.TestCase):
    def test_copy_move_rename_skip(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "src").mkdir()
            a = d / "src" / "a.png"
            a.write_bytes(b"1")
            new, _ = fileops.copy_or_move([a], d / "dst", "copy", "add a number")
            new2, _ = fileops.copy_or_move([a], d / "dst", "copy", "add a number")
            self.assertEqual([Path(new[0]).name, Path(new2[0]).name], ["a.png", "a_1.png"])
            _, lines = fileops.copy_or_move([a], d / "dst", "copy", "skip")
            self.assertIn("skipped", lines[0])
            fileops.copy_or_move([a], d / "done", "move", "overwrite")
            self.assertFalse(a.exists())
            self.assertTrue((d / "done" / "a.png").exists())


@unittest.skipUnless(shutil.which("ffmpeg") or os.environ.get("CRATEUTILS_FFMPEG"), "ffmpeg not installed")
class Audio(unittest.TestCase):
    def test_every_audio_format(self):
        sr = 44100
        t = np.linspace(0, 1, sr, endpoint=False)
        wave = np.stack([np.sin(2 * np.pi * 440 * t), np.sin(2 * np.pi * 660 * t)]).astype(np.float32) * 0.5
        ff = Path(ex.ffmpeg_path())
        probe = ff.with_name(ff.name.replace("ffmpeg", "ffprobe"))
        with tempfile.TemporaryDirectory() as d:
            for fmt in [f for f, v in ex.FORMATS.items() if v.kind == "audio"]:
                for depth in ex.FORMATS[fmt].depths:
                    with self.subTest(fmt=fmt, depth=depth):
                        s = ex.Settings(fmt=fmt, bit_depth=depth, quality=80)
                        r = ex.save_audio([wave, wave[:1]], sr, s, lambda i, e: os.path.join(d, f"a{i}{e}"))
                        self.assertEqual(len(r), 2)
                        if not probe.is_file():
                            continue
                        p = subprocess.run([str(probe), "-v", "error", "-show_entries",
                                            "stream=codec_name,channels:format=duration", "-of", "json", r[0].path],
                                           capture_output=True, text=True)
                        j = json.loads(p.stdout)
                        self.assertEqual(j["streams"][0]["channels"], 2)
                        self.assertAlmostEqual(float(j["format"]["duration"]), 1.0, delta=0.08)

    def test_lossless_wav_is_exact_and_lossy_flag_maxes_bitrate(self):
        args, detail = ex.audio_args(ex.Settings(fmt="mp3 (audio)", lossless=True))
        self.assertIn("320k", args)
        with tempfile.TemporaryDirectory() as d:
            w = (np.arange(1000, dtype=np.float32)[None] % 200 - 100) / 128
            p = os.path.join(d, "x.wav")
            ex.write_audio(w, 8000, ex.Settings(fmt="wav (audio)", bit_depth="32 float"), p)
            data = open(p, "rb").read()
            pcm = np.frombuffer(data[data.index(b"data") + 8:], "<f4")
            self.assertTrue(np.array_equal(pcm, w[0]))


if __name__ == "__main__":
    unittest.main()
