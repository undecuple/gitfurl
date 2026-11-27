"""The rolz stage's native decoder (gitfurl._codecs, native/gitfurl-codecs/src/rolz) against
the pure-Python one (farc_rolz.py, the reference): identical bytes, or identical refusal texts.

The streams are every rolz stream the tests build with tests/rolz_writer.py: test_farc_rolz's
own tests are run once with the Python decoder while every block they decode, every record
and every container they build is collected; the token streams they write are also put
into whole records (as the main LZ's stream), and so are the refused tokens and the x86
call cases, whose expected bytes the Python tests check by hand. Then each is decoded by
both. Without the native module these tests are skipped; the fallback test runs always.

With GITFURL_ROLZ_VECTORS naming a folder (see docs/testing.md), the vector test decodes
the real block listed there and compares its size, CRC-32 and SHA-256; decoded bytes are
only checksummed, never written. Otherwise it is skipped.
"""

from __future__ import annotations

import hashlib
import os
import random
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

try:
    import rolz_writer as w
    import test_farc_rolz as base
except ImportError:  # python -m unittest tests.test_farc_rolz_native
    from tests import rolz_writer as w
    from tests import test_farc_rolz as base

from gitfurl import farc
from gitfurl import farc_codecs as fc
from gitfurl import farc_rolz as r

NATIVE = fc.rolz_native()
VECTORS = os.environ.get("GITFURL_ROLZ_VECTORS")
NAME = next(iter(farc.ROLZ_NAMES))
KIND = base.KIND


def py_decode(data: bytes, budget=r.WINDOW_CAP, chunk=1 << 20, limit=None):
    try:
        return b"".join(r.decode_block(base.Bytes(data), len(data), budget, None, chunk, limit))
    except r.RolzError as exc:
        return ("refused", str(exc))


def native_decode(data: bytes, budget=r.WINDOW_CAP, chunk=1 << 20, limit=None):
    try:
        return fc._codecs.rolz_decode(data, budget, chunk, limit)
    except fc._codecs.RolzError as exc:
        return ("refused", str(exc))


def native_pieces(data: bytes, budget=r.WINDOW_CAP, chunk=1 << 20, limit=None):
    """Through rolz_block with a read callable, as RolzStage uses it; the pieces as a list."""
    out = []
    try:
        for piece in fc._codecs.rolz_block(base.Bytes(data), len(data), budget, chunk, limit):
            out.append(piece)
    except fc._codecs.RolzError as exc:
        return out, ("refused", str(exc))
    return out, None


def py_pieces(data: bytes, budget=r.WINDOW_CAP, chunk=1 << 20, limit=None):
    out = []
    try:
        for piece in r.decode_block(base.Bytes(data), len(data), budget, None, chunk, limit):
            out.append(piece)
    except r.RolzError as exc:
        return out, ("refused", str(exc))
    return out, None


class Corpus:
    """Everything test_farc_rolz builds and decodes, collected once."""

    blocks: list = []  # (data, budget, chunk, limit)
    records: list = []
    containers: list = []
    lz_runs: list = []  # (values, n, window, rolz, expected bytes)
    done = False

    @classmethod
    def collect(cls):
        if cls.done:
            return
        cls.done = True
        real_decode, real_record = r.decode_block, w.record
        real_container, real_lz_ops = w.container, w.lz_ops

        def decode_block(read, size, budget, stop=None, chunk=1 << 20, limit=None):
            cls.blocks.append((read(0, size) if size else b"", budget, chunk, limit))
            return real_decode(read, size, budget, stop, chunk, limit)

        def record(archive):
            out = real_record(archive)
            cls.records.append(out)
            return out

        def container(chunks, index_packet=4096):
            out = real_container(chunks, index_packet)
            cls.containers.append(out)
            return out

        def lz_ops(values, n, window=r.SMALL_WINDOW, rolz=False):
            ops, data = real_lz_ops(values, n, window, rolz)
            cls.lz_runs.append((list(values), n, window, rolz, data))
            return ops, data

        suite = unittest.defaultTestLoader.loadTestsFromModule(base)
        with mock.patch.object(r, "decode_block", decode_block), \
                mock.patch.object(w, "record", record), \
                mock.patch.object(w, "container", container), \
                mock.patch.object(w, "lz_ops", lz_ops), \
                mock.patch.object(fc, "rolz_native", lambda: False), \
                open(os.devnull, "w") as sink:
            result = unittest.TextTestRunner(stream=sink, verbosity=0).run(suite)
        assert result.wasSuccessful(), (result.failures, result.errors)


def main_record(ops, size: int, window: int, crc: int = 0, dir_ops=None) -> bytes:
    """A record whose main LZ stream is `ops` (a file of `size` bytes, no copies, every x86
    request passed on); `dir_ops` replaces the directory's stream."""
    if dir_ops is None:
        d = w.directory(window, size, crc)
        dir_ops, _ = w.lz_ops(w.literals(d), len(d))
    requests = -(-size // r.REQUEST)
    fl = w.Script([0] * requests)
    flag = [128]
    for _ in range(requests):
        fl.model_bit(flag, 0)
    s1 = w.Script(w.gamma(0) + w.gamma(size))
    g0 = [128] * 64
    s1.gamma(g0)
    s1.gamma(g0)
    chunks = [(4, w.packet(w.encode(ops)))] if ops else []
    chunks += [(3, w.packet(w.encode(fl.ops)))] if fl.ops else []
    chunks += [(1, w.packet(w.encode(s1.ops))), (0, w.packet(w.encode(dir_ops)))]
    return w.record(w.container(chunks))


def calls_record(flags, main: bytes, tab: bytes, n: int) -> bytes:
    """A record of one x86 request of n bytes: `flags` are its script values (as in
    test_farc_rolz.Calls), `main` and `tab` the main and table LZs' output (as literals)."""
    sc = w.Script(flags)
    calls = r.X86Calls(sc, base.FakeLZ(tab), base.FakeLZ(main))
    try:
        out = calls.request(n)
    except r.RolzError:
        out = b""
    d = w.directory(1024, n, zlib.crc32(out))
    dops, _ = w.lz_ops(w.literals(d), len(d))
    s1 = w.Script(w.gamma(0) + w.gamma(n))
    g0 = [128] * 64
    s1.gamma(g0)
    s1.gamma(g0)
    mops, _ = w.lz_ops(w.literals(main), len(main), 1024, True)
    tops, _ = w.lz_ops(w.literals(tab), len(tab))
    chunks = [(4, w.packet(w.encode(mops)))] if mops else []
    chunks += [(3, w.packet(w.encode(sc.ops)))] if sc.ops else []
    chunks += [(2, w.packet(w.encode(tops)))] if tops else []
    chunks += [(1, w.packet(w.encode(s1.ops))), (0, w.packet(w.encode(dops)))]
    return w.record(w.container(chunks))


def refused_ops(values, rolz: bool, window: int = 1 << 16, n: int = 64):
    """The operations a real LZ reads from `values` before it refuses them."""
    sc = w.Script(values)
    lz = r.LZ(sc, window, rolz, trim=not rolz)
    try:
        lz.take(n)
    except r.RolzError:
        pass
    return sc.ops


@unittest.skipUnless(NATIVE, "gitfurl's native decoder module is not built")
class SameAsPython(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Corpus.collect()

    def same(self, data: bytes, **kw):
        want = py_decode(data, **kw)
        got = native_decode(data, **kw)
        self.assertEqual(got, want)
        return want

    def test_corpus_is_large(self):
        # counted on 2026-11-27: 99 blocks, 27 records, 111 containers, 72 LZ runs
        self.assertGreaterEqual(len(Corpus.blocks), 90)
        self.assertGreaterEqual(len(Corpus.records), 25)
        self.assertGreaterEqual(len(Corpus.containers), 100)
        self.assertGreaterEqual(len(Corpus.lz_runs), 50)

    def test_every_decoded_block(self):
        seen = set()
        good = 0
        for i, (data, budget, chunk, limit) in enumerate(Corpus.blocks):
            key = (zlib.crc32(data), len(data), budget, chunk, limit)
            if key in seen:
                continue
            seen.add(key)
            with self.subTest(block=i):
                want = self.same(data, budget=budget, chunk=chunk, limit=limit)
                good += isinstance(want, bytes)
        self.assertGreater(good, 10)

    def test_every_record_and_container(self):
        refused = 0
        for i, rec in enumerate(Corpus.records):
            with self.subTest(record=i):
                self.same(rec)
                self.same(rec, chunk=777)
                if len(rec) < 20_000:
                    self.same(rec + rec)
        for i, box in enumerate(Corpus.containers):
            with self.subTest(container=i):
                refused += isinstance(self.same(w.record(box)), tuple)
        self.assertGreater(refused, 50)  # most are single streams, not whole records

    def test_pieces_and_limits(self):
        # the same pieces in the same order, through a read callable, and the same point
        # of refusal under a limit
        for rec in Corpus.records[:40]:
            want = py_decode(rec)
            if not isinstance(want, bytes) or not want or len(want) > 75_000:
                continue  # the larger ones are compared whole in the test above
            for chunk in (1, 777, 1 << 16) if len(want) < 20_000 else (777, 1 << 16):
                self.assertEqual(native_pieces(rec, chunk=chunk), py_pieces(rec, chunk=chunk))
            for limit in (0, len(want) - 1, len(want)):
                self.assertEqual(native_pieces(rec, chunk=500, limit=limit),
                                 py_pieces(rec, chunk=500, limit=limit))
            self.assertEqual(native_decode(rec, budget=1023), py_decode(rec, budget=1023))

    def test_token_streams_as_records(self):
        done, seen = 0, set()
        for i, (values, n, window, _rolz, data) in enumerate(Corpus.lz_runs):
            key = (n, window, zlib.crc32(data), len(values))
            if n == 0 or n > window or key in seen:
                continue
            seen.add(key)
            with self.subTest(run=i):
                ops, got = w.lz_ops(values, n, window, True)
                self.assertEqual(got, data)  # tables or not, the same tokens
                rec = main_record(ops, n, window, zlib.crc32(data))
                self.assertEqual(self.same(rec), data)
                done += 1
        self.assertGreater(done, 15)

    def test_refused_tokens(self):
        cases = [
            # (values, in the main stream (with tables) or the directory's, message)
            (KIND["rolz"] + [0, 10], False, "has none"),
            (w.literals(b"a") + KIND["rolz"] + [0], True, "past its list"),
            (KIND["match"] + [4, 15, 15, 15], True, "before the history"),
            (KIND["rep"] + [4], True, "before the history"),
            (KIND["special"] + [9, 0], True, "not yet written"),
            (KIND["special"] + [9, 1, 5], True, "not yet written"),
        ]
        for values, main, msg in cases:
            with self.subTest(values=values[:8]):
                ops = refused_ops(values, main)
                rec = main_record(ops, 64, 1 << 16) if main else \
                    main_record([], 0, 1024, dir_ops=ops)
                want = self.same(rec)
                self.assertIsInstance(want, tuple)
                self.assertIn(msg, want[1])

    def test_distance_over_the_window(self):
        data = bytes(5000)
        ops = refused_ops(w.literals(data) + KIND["match"] + [4, 15, 15, 15], True, 5120, 5001)
        want = self.same(main_record(ops, 5001, 5120))
        self.assertIn("before the history", want[1])

    def test_x86_calls(self):
        main = b"\x90\x90\xe8" + b"\x90" * 9
        cases = [
            ([0], b"\xe8" * 16, b"", 16),
            ([1, 1, 0, 0, 0, 1], main, base.tables(0x1000), 16),
            ([1, 1, 0, 0, 0, 1], main, base.tables(0), 16),
            ([1, 1, 0, 1, 1, 1, 0, 1, 1, 1, 1], b"\x90\x90\xe8\xe9\x0f\x85\x48\x80\x05\x90\x91\x92",
             base.tables(21, 8, 14, 3), 28),
            ([1, 1, 0, 0, 0, 1], b"MZ" + bytes(8) + b"\xe8" + bytes(9), base.tables(0x100), 24),
            ([1, 1, 1, 0, 0, 1], b"\x90\x90\xe8" + b"\x90" * 5, base.tables(1, 2), 16),
            ([1, 0, 0, 0, 1], main + b"\x90" * 4, b"", 16),
            ([1, 5, 0, 0, 0], b"", b"", 16),
        ]
        results = []
        for flags, m, tab, n in cases:
            with self.subTest(flags=flags):
                results.append(self.same(calls_record(flags, m, tab, n)))
        self.assertEqual(sum(isinstance(x, bytes) for x in results), 5)
        self.assertIn("used up exactly", results[5][1])
        self.assertIn("past its end", results[6][1])
        self.assertIn("larger than", results[7][1])

    def test_random_token_streams(self):
        # a real LZ answered at random (small symbols more often; in three streams of four
        # also guided so that most tokens are valid); its requests up to the last whole
        # token become the main stream of a record, and the stream with the refused token
        # another

        class Random(w.Script):
            def __init__(self, seed):
                super().__init__([])
                self.rng = random.Random(seed)

            def bit(self, p):
                # a context with no reduced-offset entries yet gets a literal (a 1 as the
                # first decision; any other bit read then is harmless as a 1)
                empty = self.guided and not self.lz.rolz.count[self.lz.buf[-1]]
                b = 1 if empty else int(self.rng.random() < 0.7)
                self.ops.append(("bit", p, b))
                return b

            def raw(self, n):
                if n > 16:
                    hi = self.raw(n - 16)
                    return hi << 16 | self.raw(16)
                if not n:
                    return 0
                v = self.rng.randrange(1 << min(n, 1 + self.rng.randrange(n)))
                self.ops.append(("raw", n, v))
                return v

            def guide(self, lz, guided):
                """Symbols that keep most tokens valid: near distances, reps without a
                delta, the newest reduced-offset entry and lengths near their base."""
                self.lz, self.guided = lz, guided
                self.near = {id(lz.dcdf)} | {id(c) for c in lz.repsym}
                self.newest = {id(c) for c in lz.ridx}
                self.based = {id(c) for c in lz.rlen}

            def sym(self, cdf):
                n = len(cdf) - 1
                s = min(self.rng.randrange(n) for _ in range(3))
                if not self.guided:
                    pass
                elif id(cdf) in self.near:
                    s = self.rng.randrange(4)
                elif id(cdf) in self.newest:
                    s = 0
                elif id(cdf) in self.based:
                    s = self.rng.randrange(9, 13)
                self.ops.append(("sym", cdf[s], cdf[s + 1] - cdf[s], s))
                r.update(cdf, s)
                return s

        lengths = []
        refused = 0
        for seed in range(120):
            sc = Random(seed)
            lz = r.LZ(sc, 1 << 16, True, trim=False)
            sc.guide(lz, seed % 4 != 0)  # every fourth stream is left to chance
            k = size = 0
            failed = False
            while len(lz.buf) - r.HIST < 2000:
                k, size = len(sc.ops), len(lz.buf) - r.HIST
                try:
                    lz.take(1)
                except r.RolzError:
                    failed = True
                    break
            if not failed:
                k, size = len(sc.ops), len(lz.buf) - r.HIST
            data = bytes(lz.buf[r.HIST:r.HIST + size])
            with self.subTest(seed=seed):
                # up to the last whole token: both give these bytes
                if size:
                    rec = main_record(sc.ops[:k], size, 1 << 16, zlib.crc32(data))
                    self.assertEqual(self.same(rec), data)
                    lengths.append(size)
                # the whole stream, with the token the Python decoder refused
                if failed:
                    want = self.same(main_record(sc.ops, size + 64, 1 << 16))
                    self.assertIsInstance(want, tuple)
                    refused += 1
        self.assertGreater(sum(lengths), 150_000)
        self.assertGreater(refused, 20)

    def test_memory_and_window_cap(self):
        self.assertEqual(fc._codecs.ROLZ_WINDOW_CAP, r.WINDOW_CAP)
        for window in (0, 1024, 153_754_624, r.WINDOW_CAP):
            self.assertEqual(fc._codecs.rolz_memory_for(window), r.memory_for(window))


class Choice(unittest.TestCase):
    def _run(self, rec: bytes, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.bin"
            path.write_bytes(rec)
            fd = os.open(path, os.O_RDONLY)
            try:
                st = fc.RolzStage(fd, 0, len(rec), r.WINDOW_CAP, 1 << 16, **kw)
                out = bytearray()
                while piece := st.read(1 << 16):
                    out += piece
                return st, bytes(out)
            finally:
                os.close(fd)

    def test_python_fallback(self):
        data = os.urandom(3000)
        rec = w.stored_file(data)
        st, out = self._run(rec, native=False)
        self.assertFalse(st.native)
        self.assertEqual(out, data)
        with mock.patch.object(fc, "_codecs", None):
            self.assertFalse(fc.rolz_native())
            st, out = self._run(rec)
            self.assertFalse(st.native)
            self.assertEqual(out, data)
        with mock.patch.object(fc, "_codecs", object()):  # a module built before rolz
            self.assertFalse(fc.rolz_native())

    @unittest.skipUnless(NATIVE, "gitfurl's native decoder module is not built")
    def test_native_is_used_when_built(self):
        data = os.urandom(3000)
        rec = w.stored_file(data)
        st, out = self._run(rec)
        self.assertTrue(st.native)
        self.assertEqual(out, data)
        bad = bytearray(rec)
        bad[40] ^= 4
        texts = []
        for native in (True, False):
            with self.assertRaisesRegex(fc.CodecError, "rolz stage: damaged data") as cm:
                self._run(bytes(bad), native=native)
            texts.append(str(cm.exception))
        self.assertEqual(texts[0], texts[1])

    @unittest.skipUnless(NATIVE, "gitfurl's native decoder module is not built")
    def test_read_and_stop_errors_come_back_as_they_are(self):
        rec = w.stored_file(bytes(300_000))

        class Stopped(Exception):
            pass

        calls = []

        def stop():
            calls.append(1)
            if len(calls) > 3:
                raise Stopped

        with self.assertRaises(Stopped):
            list(fc._codecs.rolz_block(base.Bytes(rec), len(rec), r.WINDOW_CAP, 1 << 16,
                                       None, stop))

        def read(off, n):
            raise OSError("gone")

        with self.assertRaisesRegex(OSError, "gone"):
            list(fc._codecs.rolz_block(read, len(rec), r.WINDOW_CAP))
        with self.assertRaisesRegex(fc._codecs.RolzError, "short read"):
            list(fc._codecs.rolz_block(lambda off, n: b"", len(rec), r.WINDOW_CAP))
        # after a refusal the iterator ends
        it = fc._codecs.rolz_block(base.Bytes(rec + b"\0"), len(rec) + 1, r.WINDOW_CAP)
        with self.assertRaises(fc._codecs.RolzError):
            list(it)
        self.assertEqual(list(it), [])


def _vector_block(root: Path):
    lines = root.joinpath("block.txt").read_text().splitlines()
    archive, offset, size = lines[0], int(lines[1]), int(lines[2])
    want_size, want_crc, want_sha = int(lines[3]), int(lines[4], 16), lines[5].strip()
    return archive, offset, size, want_size, want_crc, want_sha


@unittest.skipUnless(VECTORS, "GITFURL_ROLZ_VECTORS is not set")
class Vectors(unittest.TestCase):
    """GITFURL_ROLZ_VECTORS names a folder holding `block.txt` (six lines: the archive's path,
    the rolz block's offset in it, the block's size, then the expected output size, CRC-32 in
    hex and SHA-256). The native decoder is checked when it is built; with
    GITFURL_ROLZ_PYTHON=1 the pure-Python decoder too (it takes about four minutes)."""

    def _check(self, native: bool):
        archive, offset, size, want_size, want_crc, want_sha = _vector_block(Path(VECTORS))
        fd = os.open(archive, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        st = fc.RolzStage(fd, offset, size, r.WINDOW_CAP, 1 << 20, None, None, native=native)
        h, crc, n = hashlib.sha256(), 0, 0
        while piece := st.read(1 << 20):
            h.update(piece)
            crc = zlib.crc32(piece, crc)
            n += len(piece)
        self.assertEqual((n, crc, h.hexdigest()), (want_size, want_crc, want_sha))

    @unittest.skipUnless(NATIVE, "gitfurl's native decoder module is not built")
    def test_native(self):
        self._check(True)

    @unittest.skipUnless(os.environ.get("GITFURL_ROLZ_PYTHON") == "1",
                         "GITFURL_ROLZ_PYTHON is not 1")
    def test_python(self):
        self._check(False)


if __name__ == "__main__":
    unittest.main()
