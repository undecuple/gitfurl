"""The live activity view (gitfurl.activity): the decoders' counters through a multi-stage
chain, srep's kept bytes, cm job counters, the "no new output" wording, throughput and the
estimate with a fake clock, the command line's two forms, the window panel's text, the
Wine route's /proc sampling, and the counters' cost on a 64 MiB lzma block. Every archive
is synthetic (tests/farc_writer.py, tests/cm_writer.py)."""

from __future__ import annotations

import lzma
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    import cm_writer
    from farc_writer import F, make_archive, srep_encode
except ImportError:  # python -m unittest tests.test_activity
    from tests import cm_writer
    from tests.farc_writer import F, make_archive, srep_encode

from gitfurl import activity, extract, farc, procgroup, progress
from gitfurl import farc_codecs as fc

MiB = 1 << 20
CM = next(iter(farc.CM_NAMES))


class Clock:
    def __init__(self, t: float = 100.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def pattern(n: int, seed: int = 1) -> bytes:
    r = random.Random(seed)
    part = bytes(r.getrandbits(8) for _ in range(min(n, 50_000)))
    return (part * (n // len(part) + 1))[:n]


class Mem:
    def __init__(self, data: bytes, step: int = 1 << 20):
        self.data, self.at, self.step = data, 0, step

    def read(self, n: int) -> bytes:
        out = self.data[self.at:self.at + min(n, self.step)]
        self.at += len(out)
        return out


class Prefix(unittest.TestCase):
    """A setup.exe with an archive beside it and an empty prefix."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.src = self.base / "src"
        self.src.mkdir()
        self.setup = self.src / "setup.exe"
        self.setup.write_bytes(b"MZ" + b"\0" * 100)
        self.prefix = self.base / "data" / "gitfurl" / "prefixes" / "p1"
        (self.prefix / "drive_c").mkdir(parents=True)
        env = mock.patch.dict("os.environ", {"XDG_DATA_HOME": str(self.base / "data"),
                                             "HOME": str(self.base)})
        env.start()
        self.addCleanup(env.stop)

    def unpack(self, blocks, record, **kw):
        (self.src / "a.arc").write_bytes(make_archive(blocks))
        plan = extract.plan_extract(self.setup, "p1")
        return extract.extract(plan, activity=record, **kw)


# --- the native route ------------------------------------------------------------------------

class Chain(Prefix):
    def test_the_record_advances_through_a_multi_stage_chain(self):
        data = pattern(900_000, 2)
        method = "rep:1mb+exe+delta+lzma:1mb"
        record = activity.Activity()
        seen, events = [], []
        res = self.unpack([(method, [F("a.bin", data[:300_000]), F("b.bin", data[300_000:600_000]),
                                     F("c.bin", data[600_000:])]),
                           ("lzma:1mb", [F("d.txt", b"hello\n" * 1000)])],
                          record, on_progress=lambda d, t: seen.append(record.snapshot()),
                          on_event=lambda *a: events.append(a))
        self.assertTrue(res.ok, res.lines())
        first = [s for s in seen if s.block == 1 and s.method == "rep+exe+delta+lzma"]
        self.assertEqual(len(first), 3)  # one snapshot after each file of the block
        self.assertEqual([v.name for v in first[0].stages], ["rep", "exe", "delta", "lzma"])
        self.assertEqual([s.files_done for s in first], [1, 2, 3])
        for a, b in zip(first, first[1:]):
            self.assertLessEqual(a.block_out, b.block_out)
            for va, vb in zip(a.stages, b.stages):
                self.assertLessEqual(va.bytes_out, vb.bytes_out)
        last = first[-1]
        self.assertEqual(last.block_read, last.block_comp)  # the whole block was read
        self.assertGreaterEqual(last.block_out, len(data))
        lz = last.stages[-1]
        self.assertEqual(lz.bytes_in, last.block_comp)  # lzma reads the stored bytes
        for upper, lower in zip(last.stages, last.stages[1:]):
            self.assertEqual(upper.bytes_in, lower.bytes_out)  # each reads the one below
        self.assertEqual(last.run_files, 4)
        self.assertEqual(seen[-1].run_files_done, 4)
        # a row per stage when the block ends, in the log, the timeline and the record
        ends = [e for e in events if e[0] == "block_end"]
        self.assertEqual(len(ends), 2)
        rows = ends[0][4]
        self.assertEqual([r.split(":")[0] for r in rows], ["rep", "exe", "delta", "lzma"])
        self.assertRegex(rows[-1], r"^lzma: [\d.]+ KB in, [\d.]+ KB out, ")
        self.assertEqual(record.summaries[0], rows)
        self.assertTrue(record.snapshot().finished)
        self.assertIn("finished", activity.block_lines(record.snapshot())[0])

    def test_storing_only_counts_the_stored_bytes(self):
        record = activity.Activity()
        seen = []
        self.unpack([("storing", [F("a", b"x" * 5000)])], record,
                    on_progress=lambda d, t: seen.append(record.snapshot()))
        self.assertEqual((seen[0].block_read, seen[0].block_out, seen[0].stages), (5000, 5000, ()))

    def test_the_current_file_is_shown_while_it_is_written(self):
        record = activity.Activity()
        seen = []
        real = record.file_start

        def start(name):
            real(name)
            seen.append(record.snapshot())

        record.file_start = start
        self.unpack([("lzma:1mb", [F("one.bin", b"1" * 100), F("two.bin", b"2" * 100)])], record)
        self.assertEqual([s.current_file for s in seen], ["one.bin", "two.bin"])
        self.assertIn("files: 1 of 2 in this block, now two.bin", activity.panel_text(seen[1]))


class Srep(unittest.TestCase):
    def test_kept_bytes_of_pending_matches_and_history(self):
        block = 4096
        r = random.Random(4)
        part = bytes(r.getrandbits(8) for _ in range(block))
        data = part + bytes(r.getrandbits(8) for _ in range(block)) + part  # 2 blocks back
        for version in (3, 2):
            with self.subTest(version=version):
                st = fc.SrepStage(Mem(srep_encode(data, version, block=block)), block,
                                  16 * MiB, 1 << 20)
                kept = []
                out = bytearray()
                while piece := st.read(block):
                    out += piece
                    kept.append(st.kept)
                self.assertEqual(bytes(out), data)
                self.assertLessEqual(max(kept), st.budget)
                if version == 3:  # the match for block 3 is held from block 1 until used
                    self.assertGreaterEqual(kept[0], block)
                    self.assertEqual(kept[-1], 0)
                else:  # earlier output kept for matches
                    self.assertEqual(kept[-1], len(data))
                text = activity._detail("srep", st)
                self.assertEqual(text, f"keeps {activity.size_text(st.kept)} of its 16.8 MB cap")

    def test_the_cap_is_srep_memory_cap_without_a_known_size(self):
        st = fc.SrepStage(Mem(b""), 4096, fc.srep_budget(None), 1 << 20)
        self.assertEqual(activity._detail("srep", st), "keeps 0 bytes of its 1.1 GB cap")


class FakeOut:
    def __init__(self, data: bytes):
        self.data = data

    def take(self, n: int) -> bytes:
        piece, self.data = self.data[:n], self.data[n:]
        return piece

    def __len__(self):
        return len(self.data)


class Cm(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "blk"

    def stage(self, enc: bytes):
        self.path.write_bytes(b"pad" + enc)
        fd = os.open(self.path, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        return fc.CmStage(fd, 3, len(enc), 1, MiB)

    def test_job_counters_with_a_mocked_module(self):
        data = pattern(5 * MiB, 3)  # jobs of 2 MiB: 3 jobs
        enc = cm_writer.encode(data, piece=500_000)
        during = []

        def decode(head, jobs, read, threads, stop):
            during.append(activity._detail("cm", st))
            ((a_off, a_len, b_off, b_len),) = jobs
            read(b_off, b_len)  # what the module reads while the job runs
            return [FakeOut(enc[b_off:b_off + b_len])]

        fake = SimpleNamespace(cm_header=lambda head: (2 * MiB, 0), cm_decode_jobs=decode,
                               CmError=ValueError)
        with mock.patch.object(fc, "_codecs", fake):
            st = self.stage(enc)
            self.assertEqual(st.jobs_total, 3)
            self.assertEqual(activity._detail("cm", st), "no job started yet (3 jobs)")
            out = bytearray()
            while piece := st.read(MiB):
                out += piece
        self.assertEqual(bytes(out), data)
        self.assertEqual(len(during), 3)
        for k, text in enumerate(during, 1):
            self.assertRegex(text, rf"^job {k} of 3 \([\d.]+ [MK]B stored\), running for 0 s$")
        self.assertEqual((st.jobs_started, st.jobs_done), (3, 3))
        self.assertEqual(activity._detail("cm", st), "3 of 3 jobs done")
        self.assertGreaterEqual(st.taken, len(data))
        view = activity.StageView("cm", st.taken, st.made, 192.0, activity._detail("cm", st))
        self.assertEqual(activity.summary_row(view), "cm: 3 jobs, 5.2 MB, 3 min 12 s")

    def test_a_damaged_job_list_gives_no_count(self):
        enc = cm_writer.encode(pattern(3 * MiB, 3))
        fake = SimpleNamespace(cm_header=lambda head: (2 * MiB, 0), CmError=ValueError)
        with mock.patch.object(fc, "_codecs", fake):
            st = self.stage(enc[:-50])
        self.assertIsNone(st.jobs_total)
        self.assertEqual(activity._detail("cm", st), "no job started yet")

    @unittest.skipUnless(fc.cm_available(), "gitfurl's native decoder module is not built")
    def test_job_counters_with_the_module(self):
        data = pattern(5 * MiB, 5)
        st = self.stage(cm_writer.encode(data, piece=300_000))
        self.assertEqual(st.jobs_total, 3)
        first = st.read(MiB)
        self.assertTrue(first)
        self.assertEqual((st.jobs_started, st.jobs_done), (1, 1))
        self.assertEqual(activity._detail("cm", st), "1 of 3 jobs done")
        out = bytearray(first)
        while piece := st.read(MiB):
            out += piece
        self.assertEqual(bytes(out), data)
        self.assertEqual((st.jobs_started, st.jobs_done), (3, 3))


class TimelineRows(unittest.TestCase):
    def test_a_row_per_stage_for_blocks_that_took_a_while(self):
        from gitfurl import route

        rows = []
        tl = SimpleNamespace(add=lambda kind, what, why="", fold=None: rows.append((kind, what)),
                             attach=lambda path: None, log_path=None)
        xplan = SimpleNamespace(archives=[1], target="C:\\Games\\p1", dest=Path("/x"),
                                dest_chosen=False, file_count=2, total_size=10)
        on_event = route._tl_unpack_start(tl, xplan, Path("/x/stem"), Path("/x/log"))
        ap = SimpleNamespace(path=Path("a.arc"), archive=SimpleNamespace(blocks=[1, 2]), files=[])
        del rows[:]
        stages = ["srep: 8.9 GB in, 10.0 GB out, 4 min 01 s", "cm: 17 jobs, 8.9 GB, 3 min 12 s"]
        on_event("block_end", ap, 0, None, stages, 2.0)  # quick: in the log only
        self.assertEqual(rows, [])
        on_event("block_end", ap, 1, None, stages, 430.0)
        self.assertEqual(rows, [("archive", f"Data block 2 of a.arc: {r}") for r in stages])


# --- wording, throughput and the estimate (a fake clock) ------------------------------------

def fake_chain(comp: int):
    """A Source and an lzma-like stage whose counters a test moves by hand."""
    src = SimpleNamespace(taken=0)
    st = SimpleNamespace(made=0, busy=0.0, taken=0)
    return src, st, [("source", src), ("lzma", st)]


class Figures(unittest.TestCase):
    def record(self, comp=100 * MiB):
        clock = Clock()
        cpu = [0.0]
        rec = activity.Activity(clock=clock, cpu=lambda: cpu[0])
        rec.begin_run(comp, 2)
        src, st, parts = fake_chain(comp)
        rec.begin_block("a.arc", 0, 1, "lzma", comp, 4 * comp, 2, parts)
        return rec, clock, cpu, src, st

    def test_no_new_output_while_the_archive_is_read(self):
        rec, clock, cpu, src, st = self.record(1000)
        rec.snapshot()
        for _ in range(13):  # 130 s: the archive position moves, no output
            clock.t += 10
            src.taken += 26
            rec.snapshot()
        snap = rec.snapshot()
        self.assertEqual(activity.quiet_text(snap),
                         "no new output for 2 min 10 s; the decoder is still reading the "
                         "archive (34 % of this block)")
        self.assertEqual(activity.block_lines(snap)[-1], "  " + activity.quiet_text(snap))

    def test_nothing_moving_is_described_never_called_stuck(self):
        rec, clock, cpu, src, st = self.record(1000)
        st.made = 10
        rec.snapshot()
        clock.t += 95
        cpu[0] += 95.0
        snap = rec.snapshot()
        text = activity.quiet_text(snap)
        self.assertEqual(text, "no new output for 1 min 35 s, and no counter has changed for "
                               "1 min 35 s; gitfurl used 100 % CPU over the last 1 min 35 s; one "
                               "large step (a cm job, an x86 code block) gives no figure until "
                               "it ends. Stop still works")
        for line in (*activity.block_lines(snap), activity.one_line(snap)):
            self.assertNotIn("stuck", line.lower())

    def test_working_decoders_without_output_or_reading(self):
        rec, clock, cpu, src, st = self.record(1000)
        holder = SimpleNamespace(made=0, busy=0.0, kept=0, budget=10 ** 6)
        rec._block.parts.append(("srep", holder))
        rec.snapshot()
        for _ in range(4):
            clock.t += 10
            holder.kept += 1000  # srep takes in data and keeps it: nothing out yet
            rec.snapshot()
        snap = rec.snapshot()
        self.assertEqual(snap.out_quiet_s, 40)
        self.assertEqual(activity.quiet_text(snap), "no new output for 40 s; the decoders are "
                         "still working (their counters still change)")

    def test_short_quiet_periods_say_nothing(self):
        rec, clock, cpu, src, st = self.record(1000)
        rec.snapshot()
        clock.t += 29
        snap = rec.snapshot()
        self.assertIsNone(activity.quiet_text(snap))
        self.assertIn("  last change 29 s ago; elapsed 29 s", activity.block_lines(snap))

    def test_throughput_over_the_last_ten_seconds_and_the_estimate(self):
        rec, clock, cpu, src, st = self.record(1000 * 10 ** 6)
        rec.snapshot()
        steps = [(5, 50), (5, 50), (10, 100)]  # seconds, MB read
        for dt, mb in steps:
            clock.t += dt
            src.taken += mb * 10 ** 6
            st.made += 3 * mb * 10 ** 6
            snap = rec.snapshot()
        # the window starts at the sample 10 s back: 100 MB in 10 s
        self.assertAlmostEqual(snap.read_bps, 10e6)
        self.assertAlmostEqual(snap.out_bps, 30e6)
        self.assertAlmostEqual(snap.eta_s, (1000 - 200) / 10)  # 80 s
        lines = activity.block_lines(snap)
        self.assertIn("  last 10 s: archive 10.0 MB/s, decoded 30.0 MB/s; about 1 min 20 s "
                      "left (estimate)", lines)
        self.assertIn("about 1 min 20 s left (estimate)", activity.one_line(snap))

    def test_no_estimate_too_early_or_without_reading(self):
        rec, clock, cpu, src, st = self.record(10 ** 9)
        rec.snapshot()
        clock.t += 10
        src.taken += 10 ** 6  # 0.1 %: too early
        self.assertIsNone(rec.snapshot().eta_s)
        clock.t += 10
        snap = rec.snapshot()  # nothing read in the last 10 s
        self.assertEqual(snap.read_bps, 0)
        self.assertIsNone(snap.eta_s)
        self.assertIn("no estimate yet", activity.block_lines(snap)[-2])

    def test_finished_blocks_count_toward_the_run(self):
        rec, clock, cpu, src, st = self.record(1000)
        src.taken, st.made = 1000, 4000
        rec.end_block()
        snap = rec.snapshot()
        self.assertFalse(snap.active)
        self.assertEqual(snap.run_read, 1000)
        self.assertIn("between data blocks", activity.one_line(snap))
        self.assertEqual(rec.summaries, [["lzma: 1.0 KB in, 4.0 KB out, under 1 s"]])


# --- the command line and the window ----------------------------------------------------------

class Interfaces(Prefix):
    def snap(self):
        record = activity.Activity()
        seen = []
        self.unpack([("srep+lzma:1mb", [F("data/one.bin", pattern(400_000)),
                                        F("data/two.bin", pattern(300_000, 2))])],
                    record, on_progress=lambda d, t: seen.append(record.snapshot()))
        return seen[0]

    def test_terminal_block_and_plain_line(self):
        record = activity.Activity()
        out_t, out_p = [], []
        with activity.CliProgress(True, 0.01, out_t.append, terminal=True) as live, \
                activity.CliProgress(True, 0.01, out_p.append, terminal=False):
            self.unpack([("lzma:1mb", [F("a.bin", pattern(200_000))])], live.record)
        self.assertTrue(out_t and out_p)
        self.assertEqual(out_t[-1].splitlines()[0][:34], "[progress] native unpack: finished")
        block = activity.Printer(record, 10, None, True)
        record.begin_run(1000, 1)
        src, st, parts = fake_chain(1000)
        record.begin_block("a.arc", 0, 3, "lzma", 1000, 4000, 1, parts)
        lines = block.text().splitlines()
        self.assertEqual(lines[0], "[progress] native unpack: data block 1 of 3 in a.arc (lzma)")
        self.assertTrue(all(ln.startswith(" " * 11) and ln[11] != " " for ln in lines[1:]))
        self.assertEqual(len(lines), 6)
        plain = activity.Printer(record, 10, None, False).text()
        self.assertNotIn("\n", plain)
        self.assertTrue(plain.startswith("[progress] native unpack: block 1/3 (lzma), archive "
                                         "0 % of this block, decoded 0 bytes, files 0/1"))

    def test_no_final_print_after_an_interrupt(self):
        out = []
        with self.assertRaises(KeyboardInterrupt), \
                activity.CliProgress(True, 60, out.append, terminal=False):
            raise KeyboardInterrupt
        self.assertEqual(out, [])
        self.assertEqual(activity.CliProgress(False, 10).kwargs(), {})

    def test_window_panel_text(self):
        self.assertEqual(activity.panel_text(None),
                         "Shown while a run works: what each step is doing right now.")
        text = activity.panel_text(self.snap())
        lines = text.splitlines()
        self.assertEqual(lines[0], "native unpack: data block 1 of 1 in a.arc (srep+lzma)")
        self.assertRegex(lines[1], r"^archive read: [\d.]+ KB of [\d.]+ KB \(100 % of this "
                                   r"block\); decoded [\d.]+ KB of 700.0 KB$")
        self.assertRegex(lines[2], r"^srep: .* in, .* out; keeps .* of its .* cap$")
        self.assertRegex(lines[3], r"^lzma: .* in, .* out$")
        self.assertEqual(lines[4], "files: 1 of 2 in this block")
        self.assertRegex(lines[5], r"^(last \d+ s: archive |rates: not measured yet)")
        self.assertFalse(any(ln.startswith(" ") for ln in lines))

    def test_wine_route_text(self):
        smp = progress.Sample(60, 0, 0, 10 ** 9, 0.0, procs=3, cpu_pct=101.0,
                              helpers=("unpack.exe", "setup.exe"),
                              processes=(("unpack.exe", 99.0, 800 * 10 ** 6),
                                         ("setup.exe", 0.0, 40 * 10 ** 6),
                                         ("wineserver", None, 5 * 10 ** 6)),
                              newest="drive_c/Games/x/data.pak", newest_age_s=4.0,
                              write_bps=45e6)
        self.assertEqual(activity.wine_lines(smp), [
            "unpack.exe: CPU 99 %, busy, 800.0 MB memory",
            "setup.exe: CPU 0 %, CPU time not growing, 40.0 MB memory",
            "wineserver: CPU not measured yet, 5.0 MB memory",
            "newest file in the prefix: drive_c/Games/x/data.pak, 4 s ago",
            "writing to the prefix: 45.0 MB/s"])
        term = activity.wine_progress_text(smp, True).splitlines()
        self.assertEqual(term[0], f"[progress] {smp.line()}")
        self.assertEqual(term[1], "           unpack.exe: CPU 99 %, busy, 800.0 MB memory")
        plain = activity.wine_progress_text(smp, False)
        self.assertEqual(plain, f"[progress] {smp.line()}; busiest: unpack.exe CPU 99 %; "
                                "writing 45.0 MB/s")
        self.assertEqual(activity.wine_panel_text(smp).splitlines()[1:],
                         activity.wine_lines(smp))
        bare = progress.Sample(1, 0, 0, 0, 0.0)  # an older sample has none of these
        self.assertEqual(activity.wine_lines(bare), [])
        self.assertEqual(activity.wine_progress_text(bare, False), f"[progress] {bare.line()}")


# --- the Wine route: /proc --------------------------------------------------------------------

def stat_line(pid, name, pgid, ticks, rss_pages, state="S"):
    fields = [state, "1", str(pgid), str(pgid)] + ["0"] * 7 + [str(ticks), "0"] + ["0"] * 8 \
        + [str(rss_pages)] + ["0"] * 20
    return f"{pid} ({name}) " + " ".join(fields)


class Proc(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.proc = self.root / "proc"
        self.prefix = self.root / "prefix"
        (self.prefix / "drive_c" / "Games").mkdir(parents=True)

    def put(self, pid, name, pgid, ticks, rss_pages, prefix=None, state="S"):
        d = self.proc / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "stat").write_text(stat_line(pid, name, pgid, ticks, rss_pages, state))
        env = b"HOME=/x\0" + (f"WINEPREFIX={prefix}".encode() + b"\0" if prefix else b"")
        (d / "environ").write_bytes(env)

    def test_fake_proc_tree(self):
        page = os.sysconf("SC_PAGE_SIZE")
        self.put(100, "umu-run", 100, 10, 100)  # gitfurl's group leader, not in the prefix
        self.put(101, "setup.exe", 100, 50, 1000, self.prefix)
        self.put(102, "unpack (x).exe", 300, 0, 2000, self.prefix)  # in the prefix only
        self.put(103, "other", 400, 999, 10)  # neither: never shown
        self.put(104, "gone", 100, 5, 0, state="Z")  # a zombie of the group
        self.assertEqual(procgroup.group_members(100, str(self.proc)), [100, 101])
        self.assertEqual(procgroup.group_members(None, str(self.proc)), [])
        info = progress.process_info([101, 102, 999], str(self.proc))
        self.assertEqual([(p.name, p.ticks, p.rss) for p in info],
                         [("setup.exe", 50, 1000 * page), ("unpack (x).exe", 0, 2000 * page)])
        mon = progress.ProgressMonitor(self.root / "setup.exe", self.prefix, lambda s: None,
                                       1.0, str(self.proc), group=lambda: 100)
        mon._t0 = time.monotonic()
        first = mon.sample()
        self.assertEqual(sorted(n for n, _c, _r in first.processes),
                         ["setup.exe", "umu-run", "unpack (x).exe"])
        self.assertTrue(all(c is None for _n, c, _r in first.processes))
        self.assertIsNone(first.write_bps)
        hz = os.sysconf("SC_CLK_TCK")
        mon._ticks_t -= 1.0  # one second ago
        self.put(102, "unpack (x).exe", 300, hz, 2500, self.prefix)  # a full core for 1 s
        (self.prefix / "drive_c" / "Games" / "new.bin").write_bytes(b"x" * 4096)
        mon._size_last = (mon._size_last[0] - 1.0, mon._size_last[1])
        second = mon.sample()
        name, cpu, rss = second.processes[0]  # busiest first
        self.assertEqual((name, rss), ("unpack (x).exe", 2500 * page))
        self.assertAlmostEqual(cpu, 100.0, delta=5.0)
        self.assertEqual(second.newest, "drive_c/Games/new.bin")
        self.assertLess(second.newest_age_s, 60)
        self.assertGreater(second.write_bps, 3000)
        self.assertIn("unpack (x).exe: CPU", activity.wine_lines(second)[0])

    def test_a_real_child_in_its_own_group(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 start_new_session=True)
        try:
            self.assertIn(child.pid, procgroup.group_members(child.pid))
            (info,) = progress.process_info([child.pid])
            self.assertGreater(info.rss, 0)
            self.assertTrue(info.name)
        finally:
            child.kill()  # the test's own child, by its handle
            child.wait()
        self.assertEqual(procgroup.group_members(child.pid), [])

    def test_tree_scan_names_the_newest_file_cleaned(self):
        a = self.prefix / "drive_c" / "Games" / "a.bin"
        a.write_bytes(b"1" * 10)
        b = self.prefix / "drive_c" / "Games" / "b\x1b[2J.bin"
        b.write_bytes(b"2" * 5)
        os.utime(a, (1, 1))
        size, newest, _t = progress.tree_scan(self.prefix)
        self.assertEqual(size, 15)
        self.assertNotIn("\x1b", newest)
        self.assertTrue(newest.startswith("drive_c/Games/b"))
        self.assertEqual(progress.tree_size(self.prefix), 15)


# --- the counters' cost ----------------------------------------------------------------------

def _uncounted_read(self, n: int) -> bytes:
    """_Stage.read as it was before the counters (the baseline)."""
    while self.out_at >= len(self.out):
        if self.done:
            return b""
        self.stop()
        self.out = b""
        self.out, self.out_at = self._next(), 0
    piece = self.out[self.out_at:self.out_at + n]
    self.out_at += len(piece)
    return piece


class Overhead(unittest.TestCase):
    SIZE = 64 * MiB
    RUNS = 5

    def test_counters_cost_under_five_percent_on_a_64_mib_lzma_block(self):
        r = random.Random(1)
        words = [bytes(r.getrandbits(8) for _ in range(r.randint(3, 12))) for _ in range(4000)]
        buf = bytearray()
        while len(buf) < self.SIZE:
            buf += b" ".join(r.choice(words) for _ in range(5000))
        enc = lzma.compress(bytes(buf[:self.SIZE]), format=lzma.FORMAT_RAW, filters=[
            {"id": lzma.FILTER_LZMA1, "dict_size": MiB, "lc": 3, "lp": 0, "pb": 2,
             "preset": 1}])
        del buf
        method = farc.parse_method("lzma:1mb")
        with tempfile.TemporaryFile() as fh:
            fh.write(enc)
            fh.flush()
            fd = fh.fileno()

            def run(counted: bool) -> float:
                parts: list = []
                stop = threading.Event()
                t0 = time.perf_counter()
                top = fc.open_chain(method, fd, 0, len(enc), self.SIZE, MiB, lambda: None,
                                    parts)
                if counted:
                    rec = activity.Activity()
                    rec.begin_run(len(enc), 1)
                    rec.begin_block("a.arc", 0, 1, "lzma", len(enc), self.SIZE, 1, parts)
                    th = threading.Thread(target=lambda: [rec.snapshot() for _ in iter(
                        lambda: stop.wait(0.1), True)])
                    th.start()
                left = self.SIZE
                while left:
                    left -= len(top.read(min(MiB, left)))
                dt = time.perf_counter() - t0
                if counted:
                    stop.set()
                    th.join()
                return dt

            before, after = [], []
            for _ in range(self.RUNS):
                with mock.patch.object(fc._Stage, "read", _uncounted_read):
                    before.append(run(False))
                after.append(run(True))
        ratio = min(after) / min(before)
        sys.stderr.write(f"\n64 MiB lzma block: before {min(before):.3f} s, after "
                         f"{min(after):.3f} s (best of {self.RUNS}), {100 * (ratio - 1):+.2f} %\n")
        self.assertLess(ratio, 1.05)


if __name__ == "__main__":
    unittest.main()
