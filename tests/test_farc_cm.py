"""The cm stage: its method string (read from farc's method table, never spelled out here), the
plan's memory, the refusal without gitfurl's native decoder module, and, when that module is
built, the stage's wiring (header, jobs, threads, limits, Stop, damage) on stored-segment
streams from tests/cm_writer.py and an extraction.

With GITFURL_CM_VECTORS naming a folder (see docs/testing.md), the vector test decodes the
real jobs listed there and compares each segment's CRC-32 and each job's SHA-256 with the
lists in that folder; decoded bytes are only checksummed, never written. Otherwise it is
skipped.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

try:
    import cm_writer
    import farc_writer
    from farc_writer import F, make_archive
except ImportError:  # run from the repository root
    from tests import cm_writer, farc_writer
    from tests.farc_writer import F, make_archive

from gitfurl import extract, farc
from gitfurl import farc_codecs as fc
from gitfurl.farc_codecs import CodecError, StartError

CM = next(iter(farc.CM_NAMES))  # the archive's text for the cm stage, from farc's table
NATIVE = fc.cm_available()
VECTORS = os.environ.get("GITFURL_CM_VECTORS")
MiB = 1 << 20


def pattern(n: int, seed: int = 1) -> bytes:
    out = bytearray()
    x = seed
    while len(out) < n:
        x = (x * 6364136223846793005 + 1442695040888963407) & (2**64 - 1)
        out += x.to_bytes(8, "little")
    return bytes(out[:n])


class MethodStrings(unittest.TestCase):
    def test_the_table_holds_one_name(self):
        self.assertEqual(len(farc.CM_NAMES), 1)

    def test_accepted_alone_and_as_the_last_method(self):
        m = farc.parse_method(CM)
        self.assertTrue(m.supported)
        self.assertEqual(m.kind, "cm")
        m = farc.parse_method(f"srep:m3f+{CM}")
        self.assertTrue(m.supported)
        self.assertEqual(m.kind, "srep+cm")

    def test_refused_forms_never_show_the_text(self):
        for text, reason in ((f"{CM}:x", "cm stage settings"), (f"{CM}+lzma", "last method"),
                             (f"{CM}+{CM}", "last method")):
            m = farc.parse_method(text)
            self.assertFalse(m.supported, text)
            self.assertIn(reason, m.reason)
            self.assertNotIn(CM, m.reason)
            self.assertNotIn(CM, m.kind)


class Plan(unittest.TestCase):
    def test_memory_is_counted_per_job_and_thread(self):
        m = farc.parse_method(f"srep+{CM}")
        plans = fc.stage_plan(m, 100 * MiB)
        cm = [p for p in plans if p.stage.name == "cm"][0]
        self.assertEqual(cm.budget, fc.CM_THREADS)
        self.assertEqual(cm.memory, fc.CM_THREADS * fc.CM_JOB_MEMORY)
        self.assertEqual(fc.CM_JOB_MEMORY, 520 * MiB)
        self.assertIsNone(cm.limit)  # srep to its left changes sizes
        with mock.patch.object(fc, "CM_THREADS", 3):
            mem = fc.chain_memory(m, 100 * MiB, MiB)
            self.assertGreaterEqual(mem, 3 * 520 * MiB)
        alone = fc.stage_plan(farc.parse_method(CM), 5000)[0]
        self.assertEqual(alone.limit, 5000)


class WithoutTheModule(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch.object(fc, "_codecs", None)
        p.start()
        self.addCleanup(p.stop)

    def test_the_stage_refuses_with_a_plain_reason(self):
        self.assertFalse(fc.cm_available())
        path = Path(self._tmp.name) / "blk"
        path.write_bytes(b"\0" * 64)
        fd = os.open(path, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        with self.assertRaises(StartError) as cm:
            fc.CmStage(fd, 0, 64, 1, MiB)
        self.assertEqual(str(cm.exception), fc.CM_MISSING)
        self.assertEqual(fc.CM_MISSING, "the cm stage needs gitfurl's native decoder module, "
                                        "which is not installed")
        with self.assertRaises(StartError):
            fc.open_chain(farc.parse_method(CM), fd, 0, 64, 64, MiB, lambda: None)

    def test_the_plan_is_refused_before_anything_is_written(self):
        base = Path(self._tmp.name)
        src = base / "src"
        src.mkdir()
        setup = src / "setup.exe"
        setup.write_bytes(b"MZ" + b"\0" * 100)
        prefix = base / "data" / "gitfurl" / "prefixes" / "p1"
        (prefix / "drive_c").mkdir(parents=True)
        (src / "a.arc").write_bytes(make_archive([(CM, [F("x.bin", b"abc")])]))
        with mock.patch.dict("os.environ", {"XDG_DATA_HOME": str(base / "data"),
                                            "HOME": str(base)}):
            with self.assertRaises(extract.ExtractRefused) as cm:
                extract.plan_extract(setup, "p1")
        self.assertIn(fc.CM_MISSING, str(cm.exception))
        self.assertNotIn(CM, str(cm.exception))
        self.assertFalse((prefix / "drive_c" / "Games").exists())


@unittest.skipUnless(NATIVE, "gitfurl's native decoder module is not built")
class Stage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def stage(self, enc: bytes, threads: int = 1, chunk: int = MiB, limit=None, stop=None,
              size=None):
        path = Path(self._tmp.name) / "blk"
        path.write_bytes(b"pad" + enc)
        fd = os.open(path, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        return fc.CmStage(fd, 3, len(enc) if size is None else size, threads, chunk, limit, stop)

    def drain(self, st, n: int = MiB) -> bytes:
        out = bytearray()
        while piece := st.read(n):
            out += piece
        return bytes(out)

    def test_jobs_in_order_on_one_or_more_threads(self):
        data = pattern(5 * MiB + 12345)
        enc = cm_writer.encode(data, piece=300_000)
        for threads in (1, 2, 4):
            for chunk in (MiB, 4096):
                with self.subTest(threads=threads, chunk=chunk):
                    self.assertEqual(self.drain(self.stage(enc, threads, chunk), 7777), data)

    def test_whole_stream_function(self):
        data = pattern(3 * MiB)
        enc = cm_writer.encode(data)
        self.assertEqual(fc._codecs.cm_decode(enc), data)
        self.assertEqual(fc._codecs.cm_decode(enc, 3), data)
        self.assertEqual(fc._codecs.cm_job_count(enc), 2)

    def test_an_empty_stream_gives_nothing(self):
        self.assertEqual(self.drain(self.stage(cm_writer.header() + b"\0" * 4)), b"")

    def test_output_over_the_limit_is_refused(self):
        data = pattern(3 * MiB)
        st = self.stage(cm_writer.encode(data), limit=3 * MiB - 1)
        with self.assertRaises(CodecError) as cm:
            self.drain(st)
        self.assertIn("more output than the block's files hold", str(cm.exception))
        self.assertEqual(self.drain(self.stage(cm_writer.encode(data), limit=3 * MiB)), data)

    def test_damage_is_refused_with_gitfurls_wording(self):
        data = pattern(2 * MiB + 100)
        enc = cm_writer.encode(data)
        head = cm_writer.header()
        short_first = head + cm_writer.job(b"x" * 10) + cm_writer.job(pattern(2 * MiB)) + b"\0" * 4
        cases = {
            "data after the end": enc + b"\0",
            "a read past the end": enc[:-4],
            "a job longer than the block": head + (2**31).to_bytes(4, "little") + b"\0" * 8,
            "a job shorter than the others": short_first,
            "variant gitfurl does not decode": cm_writer.header(single=True) + enc[11:],
        }
        for what, bad in cases.items():
            with self.subTest(what):
                with self.assertRaises(CodecError) as cm:
                    self.drain(self.stage(bad))
                self.assertIn(what, str(cm.exception))
                self.assertTrue(str(cm.exception).startswith(("cm stage:", "the archive")))
        # a flipped byte in a segment-header stream fails its end check, or gives another
        # segment layout that the job does not fit
        flipped = bytearray(enc)
        flipped[11 + 4 + 6] ^= 0x10
        with self.assertRaises(CodecError):
            self.drain(self.stage(bytes(flipped)))

    def test_a_changed_archive_is_an_error(self):
        enc = cm_writer.encode(pattern(MiB))
        with self.assertRaises(CodecError) as cm:
            self.drain(self.stage(enc, size=len(enc) + 10))
        self.assertIn("damaged data", str(cm.exception))

    def test_stop_ends_the_decode_with_the_callers_exception(self):
        class Stopped(Exception):
            pass

        calls = []

        def stop():
            calls.append(1)
            if len(calls) > 3:
                raise Stopped()

        st = self.stage(cm_writer.encode(pattern(6 * MiB), piece=64 * 1024), stop=stop)
        with self.assertRaises(Stopped):
            self.drain(st)

    def test_a_read_error_inside_a_job_comes_back_as_it_is(self):
        enc = cm_writer.encode(pattern(3 * MiB))
        st = self.stage(enc)
        real = st._read

        def flaky(off, n):
            if off > 1000:
                raise CodecError("the archive could not be read (test)")
            return real(off, n)

        st._read = flaky
        with self.assertRaises(CodecError) as cm:
            self.drain(st)
        self.assertIn("(test)", str(cm.exception))

    def test_too_many_jobs_in_one_call_are_refused(self):
        with self.assertRaises(ValueError):
            fc._codecs.cm_decode_jobs(cm_writer.header(), [(0, 0, 0, 0)] * 65, lambda o, n: b"")

    def test_open_chain_and_an_extraction(self):
        base = Path(self._tmp.name)
        src = base / "src"
        src.mkdir()
        setup = src / "setup.exe"
        setup.write_bytes(b"MZ" + b"\0" * 100)
        prefix = base / "data" / "gitfurl" / "prefixes" / "p1"
        (prefix / "drive_c").mkdir(parents=True)
        files = [F("big.bin", pattern(2 * MiB + 5, 3)), F("small.txt", b"hello\n" * 100),
                 F("empty", b"")]
        with mock.patch.dict(farc_writer.ENCODERS,
                             {CM: lambda data, st: cm_writer.encode(data, piece=500_000)}):
            (src / "a.arc").write_bytes(make_archive([(CM, files)]))
        with mock.patch.dict("os.environ", {"XDG_DATA_HOME": str(base / "data"),
                                            "HOME": str(base)}):
            plan = extract.plan_extract(setup, "p1")
            text = "\n".join(plan.describe())
            self.assertNotIn(CM, text)
            self.assertIn("the cm stage decodes 1 job at a time", text)
            res = extract.extract(plan)
        self.assertTrue(res.ok, res.lines())
        dest = prefix / "drive_c" / "Games" / "p1"
        for f in files:
            self.assertEqual((dest / f[1]).read_bytes(), f[2])


# --- the real vectors, only when they are given ----------------------------------------------

def _oracle(path: Path) -> tuple[list[tuple[int, int, int, int]], int, str]:
    segs, size, sha = [], -1, ""
    for line in path.read_text().splitlines():
        f = line.split()
        if f and f[0] == "size":
            size = int(f[1])
        elif f and f[0] == "sha256":
            sha = f[1]
        elif len(f) == 5:
            segs.append((int(f[1]), int(f[2]), int(f[3]), int(f[4], 16)))
    return segs, size, sha


@unittest.skipUnless(VECTORS, "GITFURL_CM_VECTORS is not set")
@unittest.skipUnless(NATIVE, "gitfurl's native decoder module is not built")
class Vectors(unittest.TestCase):
    """GITFURL_CM_VECTORS names a folder holding `block.txt` (three lines: the archive's path,
    the cm block's offset in it and the block's size) and `crc<J>-oracle.txt` per job (one line per segment:
    index, type, offset, length, CRC-32; then `size N` and `sha256 HEX`). GITFURL_CM_JOBS
    (space-separated job numbers, default: every job with a list) and GITFURL_CM_THREADS
    (default 1) choose what is decoded."""

    def test_jobs_match_their_lists(self):
        root = Path(VECTORS)
        archive, offset, size = root.joinpath("block.txt").read_text().splitlines()[:3]
        fd = os.open(archive, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        st = fc.CmStage(fd, int(offset), int(size), 1, MiB)
        spans = []
        while not st._at_end():
            a_len = st._u32(st.at)
            b_len = st._u32(st.at + 4 + a_len)
            spans.append((st.at + 4, a_len, st.at + 8 + a_len, b_len))
            st.at += 8 + a_len + b_len
        wanted = os.environ.get("GITFURL_CM_JOBS")
        jobs = ([int(j) for j in wanted.split()] if wanted else
                [j for j in range(len(spans)) if (root / f"crc{j}-oracle.txt").exists()])
        threads = int(os.environ.get("GITFURL_CM_THREADS", "1"))
        self.assertTrue(jobs)
        for k in range(0, len(jobs), threads):
            group = jobs[k:k + threads]
            outs = fc._codecs.cm_decode_jobs(st.head, [spans[j] for j in group], st._read,
                                             threads)
            for j, out in zip(group, outs):
                segs, total, sha = _oracle(root / f"crc{j}-oracle.txt")
                with self.subTest(job=j):
                    self.assertEqual(len(out), total)
                    h = hashlib.sha256()
                    for i, (_ty, at, n, crc) in enumerate(segs):
                        piece = b""
                        while len(piece) < n:
                            piece += out.take(n - len(piece))
                        h.update(piece)
                        self.assertEqual(zlib.crc32(piece), crc, f"job {j} segment {i} at {at}")
                    self.assertEqual(out.left, 0)
                    self.assertEqual(h.hexdigest(), sha)


if __name__ == "__main__":
    unittest.main()
