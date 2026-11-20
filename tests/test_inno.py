"""The Inno Setup reader (gitfurl.inno): hand-made vectors from the format specification, and
synthetic installers built in memory by tests/inno_writer.py. Nothing here is a real
installer; the bytes are written to a temporary folder for the test only."""

import contextlib
import io
import os
import random
import re
import shutil
import struct
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from unittest import mock

try:
    import inno_writer as w
except ImportError:  # python -m unittest tests.test_inno
    from tests import inno_writer as w

from gitfurl import cli, detect, inno
from gitfurl.extract import ExtractRefused


def hx(dump: str) -> bytes:
    """Bytes from a hex dump whose lines start with a 4-digit offset."""
    out = bytearray()
    for line in dump.strip().splitlines():
        out += bytes.fromhex(re.sub(r"^\s*[0-9a-f]{4}\s+", "", line))
    return bytes(out)


RND = random.Random(20261119)
EXE = bytes(RND.getrandbits(8) for _ in range(70000)) + b"\x90\xe8\x10\x00\x00\x00" * 3000
TEXT = b"plain text line\n" * 3000


def full_files():
    return [
        w.WFile("", kind="uninstaller"),
        w.WFile("{app}\\readme.txt", TEXT),
        w.WFile("{app}\\docs\\README-copy.txt", location=1, flags=("custom destination name",)),
        w.WFile("{app}\\empty.txt", b""),
        w.WFile("{app}\\bin\\game.exe", EXE, call_filter=True),
        w.WFile("{app}\\data\\sub\\b.snd", bytes(range(256)) * 40),
        w.WFile("{tmp}\\vcredist_x64.exe", b"MZ redist", flags=("delete after install",)),
    ]


def full_spec(**kw):
    kw.setdefault("files", full_files())
    kw.setdefault("run", [
        w.WRun("{tmp}\\vcredist_x64.exe", "/install /quiet /norestart"),
        w.WRun("{app}\\bin\\game.exe", "--first-run", show=0),
        w.WRun("{app}\\bin\\game.exe", flags=("post-install", "skip if silent"), wait=1),
        w.WRun("{app}\\readme.txt", flags=("shell exec", "post-install", "skip if silent",
                                           "unchecked"), wait=1),
    ])
    kw.setdefault("uninstall_run", [w.WRun("{app}\\bin\\game.exe", "--uninstall")])
    kw.setdefault("dirs", ["{app}\\saves"])
    kw.setdefault("icons", 2)
    kw.setdefault("ini", 1)
    kw.setdefault("registry", 1)
    return w.Spec(**kw)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.src = self.base / "src"
        self.src.mkdir()
        self.setup = self.src / "setup.exe"
        self.env = {"XDG_DATA_HOME": str(self.base / "data"), "HOME": str(self.base)}
        self.prefix = self.base / "data" / "gitfurl" / "prefixes" / "p1"
        (self.prefix / "drive_c").mkdir(parents=True)
        patcher = mock.patch.dict("os.environ", self.env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, spec_or_bytes) -> Path:
        data = spec_or_bytes if isinstance(spec_or_bytes, bytes) else w.build(spec_or_bytes)[0]
        self.setup.write_bytes(data)
        return self.setup

    def dest(self) -> Path:
        return self.prefix / "drive_c" / "Games" / "p1"


# --- hand-made vectors (specs/test-vectors.md) ---------------------------------------------

class Vectors(unittest.TestCase):
    def tmpfile(self, data: bytes, size: int | None = None) -> int:
        tmp = self.enterContext(tempfile.TemporaryFile())
        tmp.write(data)
        if size:
            tmp.truncate(size)
        tmp.flush()
        return tmp.fileno()

    def test_v1_crc(self):
        self.assertEqual(zlib.crc32(b""), 0)
        self.assertEqual(zlib.crc32(b"a"), 0xE8B7BE43)
        self.assertEqual(zlib.crc32(b"123456789"), 0xCBF43926)
        self.assertEqual(zlib.crc32(bytes(5)), 0xC622F71D)

    V2 = """
0000  72 44 6c 50 74 53 cd e6 d7 7b 0b 2a 01 00 00 00
0010  00 20 01 00 00 10 01 00 00 00 03 00 44 33 22 11
0020  00 08 01 00 00 04 00 00 20 dc 04 d8"""
    V3 = """
0000  72 44 6c 50 74 53 cd e6 d7 7b 0b 2a 02 00 00 00
0010  00 20 01 00 00 00 00 00 00 10 01 00 00 00 00 00
0020  00 00 03 00 44 33 22 11 00 08 01 00 00 00 00 00
0030  00 04 00 00 00 00 00 00 00 00 00 00 c6 e0 8e 99"""

    def test_v2_v3_offset_tables(self):
        for dump, size in ((self.V2, 44), (self.V3, 64)):
            raw = hx(dump)
            fd = self.tmpfile(raw, 0x12000)
            t = inno._read_table(fd, 0x12000, 0, size)
            self.assertEqual((t.version, t.min_size, t.offset_exe, t.offset0, t.offset1),
                             (size // 32 if size == 64 else 1, 0x12000, 0x11000, 0x10800, 0x400))
            bad = bytearray(raw)
            bad[20] = 1
            fd = self.tmpfile(bytes(bad), 0x12000)
            with self.assertRaisesRegex(inno.InnoError, "CRC-32"):
                inno._read_table(fd, 0x12000, 0, size)

    def test_v4_v5_v7_blocks(self):
        v4 = hx("""
0000  9f 60 c2 a6 10 00 00 00 00 88 74 90 79 48 65 6c
0010  6c 6f 2c 20 49 6e 6e 6f 21""")
        v5 = hx("""
0000  df 56 c9 4a 10 00 00 00 00 00 00 00 00 88 74 90
0010  79 48 65 6c 6c 6f 2c 20 49 6e 6e 6f 21""")
        v7 = hx("""
0000  d8 c7 95 53 1f 00 00 00 01 50 0e 11 f2 5d 00 00
0010  01 00 00 24 19 49 98 6f 16 02 87 59 69 84 67 9b
0020  9d 07 ff ff e8 49 80 00""")
        for raw, fmt in ((v4, inno.F557), (v5, inno.F670), (v7, inno.F557)):
            fd = self.tmpfile(raw)
            content, end = inno._read_block(fd, 0, len(raw), fmt, 1 << 20, "block")
            self.assertEqual(content, b"Hello, Inno!")
            self.assertEqual(end, len(raw))

    def test_v6_two_chunks_and_short_chunk(self):
        payload = bytes((7 * i) % 256 for i in range(5000))
        raw = w.frame_block(payload, inno.F557, False)
        self.assertEqual(struct.unpack_from("<I", raw, 0)[0], 0x5588287C)
        self.assertEqual(raw[4:9], bytes.fromhex("9013000000"))
        self.assertEqual(struct.unpack_from("<I", raw, 9)[0], 0xD3B3C7BC)
        self.assertEqual(struct.unpack_from("<I", raw, 4109)[0], 0x9A58604F)
        fd = self.tmpfile(raw)
        self.assertEqual(inno._read_block(fd, 0, len(raw), inno.F557, 1 << 20, "b")[0], payload)
        head = struct.pack("<IB", 4, 0)
        short = struct.pack("<I", zlib.crc32(head)) + head + bytes(4)
        fd = self.tmpfile(short)
        with self.assertRaisesRegex(inno.InnoError, "cut short"):
            inno._read_block(fd, 0, len(short), inno.F557, 1 << 20, "b")

    V9_FIXED = """
0000  01 00 00 00 01 00 00 00 01 00 00 00 01 00 00 00
0010  01 00 00 00 01 00 00 00 01 00 00 00 00 00 00 00
0020  01 00 00 00 01 00 00 00 01 00 00 00 01 00 00 00
0030  01 00 00 00 01 00 00 00 01 00 00 00 01 00 00 00
0040  01 00 00 00 00 00 00 00 00 00 01 06 00 00 00 00
0050  00 00 00 00 00 00 00 00 64 00 00 00 64 00 00 00
0060  00 00 ff ff ff ff ff ff ff ff ff ff ff ff ff ff
0070  ff ff ff ff ff ff ff ff ff ff ff ff 00 00 00 00
0080  00 00 00 00 00 01 00 00 00 00 00 02 00 00 00 04
0090  00 01 00 00 00 00 00 00 00 00 06 00 40 c3 01 00
00a0  00 00"""
    V10 = (
        """
0000  0e 00 00 00 65 00 6e 00 67 00 6c 00 69 00 73 00
0010  68 00 0e 00 00 00 45 00 6e 00 67 00 6c 00 69 00
0020  73 00 68 00 10 00 00 00 53 00 65 00 67 00 6f 00
0030  65 00 20 00 55 00 49 00 10 00 00 00 53 00 65 00
0040  67 00 6f 00 65 00 20 00 55 00 49 00 00 00 00 00
0050  00 00 00 00 00 00 00 00 00 00 00 00 09 04 09 00
0060  00 00 0d 00 00 00 07 00 00 00 0e 00 00 00 00""",
        """
0000  1c 00 00 00 4e 00 61 00 6d 00 65 00 41 00 6e 00
0010  64 00 56 00 65 00 72 00 73 00 69 00 6f 00 6e 00
0020  1a 00 00 00 25 00 31 00 20 00 76 00 65 00 72 00
0030  73 00 69 00 6f 00 6e 00 20 00 25 00 32 00 ff ff
0040  ff ff""",
        """
0000  10 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0010  00 00 00 00""",
        """
0000  08 00 00 00 66 00 75 00 6c 00 6c 00 08 00 00 00
0010  46 00 75 00 6c 00 6c 00 00 00 00 00 00 00 00 00
0020  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0030  00 00 00 00 00 01 00 00 00 00 00 00 00 00""",
        """
0000  08 00 00 00 6d 00 61 00 69 00 6e 00 14 00 00 00
0010  4d 00 61 00 69 00 6e 00 20 00 66 00 69 00 6c 00
0020  65 00 73 00 08 00 00 00 66 00 75 00 6c 00 6c 00
0030  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0040  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0050  00 00 00 00 00 00 01 00 00 00 00 00 00 00 00""",
        """
0000  16 00 00 00 64 00 65 00 73 00 6b 00 74 00 6f 00
0010  70 00 69 00 63 00 6f 00 6e 00 2a 00 00 00 43 00
0020  72 00 65 00 61 00 74 00 65 00 20 00 61 00 20 00
0030  64 00 65 00 73 00 6b 00 74 00 6f 00 70 00 20 00
0040  69 00 63 00 6f 00 6e 00 00 00 00 00 00 00 00 00
0050  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0060  00 00 00 00 00 00 00 00 00 00 00 00 00 00 02""",
        """
0000  14 00 00 00 7b 00 61 00 70 00 70 00 7d 00 5c 00
0010  73 00 61 00 76 00 65 00 00 00 00 00 00 00 00 00
0020  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0030  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0040  00 00 00 00 00 00 00 00 ff ff 01""",
        """
0000  00 00 00 00 20 00 00 00 7b 00 74 00 6d 00 70 00
0010  7d 00 5c 00 68 00 65 00 6c 00 70 00 65 00 72 00
0020  2e 00 64 00 6c 00 6c 00 00 00 00 00 00 00 00 00
0030  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0040  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0050  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0060  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0070  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0080  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0090  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00a0  00 00 00 00 00 ff ff 08 02 08 00 00 00 00 00 00""",
        """
0000  12 00 00 00 7b 00 67 00 72 00 6f 00 75 00 70 00
0010  7d 00 5c 00 41 00 16 00 00 00 7b 00 61 00 70 00
0020  70 00 7d 00 5c 00 61 00 2e 00 65 00 78 00 65 00
0030  00 00 00 00 0a 00 00 00 7b 00 61 00 70 00 70 00
0040  7d 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0050  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0060  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0070  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0080  00 00 00 00 00 00 00 00 00 00 00 00 00 00 01 00
0090  00 00 00 00 00 00""",
        """
0000  16 00 00 00 7b 00 61 00 70 00 70 00 7d 00 5c 00
0010  61 00 2e 00 69 00 6e 00 69 00 02 00 00 00 53 00
0020  02 00 00 00 4b 00 02 00 00 00 56 00 00 00 00 00
0030  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0040  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0050  00 00 00 00 00 00 00 00 10""",
        """
0000  14 00 00 00 53 00 6f 00 66 00 74 00 77 00 61 00
0010  72 00 65 00 5c 00 41 00 08 00 00 00 50 00 61 00
0020  74 00 68 00 0a 00 00 00 7b 00 61 00 70 00 70 00
0030  7d 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0040  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0050  00 00 00 00 00 00 00 00 00 00 00 00 00 00 02 00
0060  00 80 ff ff 01 02 00""",
        """
0000  1a 00 00 00 7b 00 61 00 70 00 70 00 7d 00 5c 00
0010  6f 00 6c 00 64 00 2e 00 74 00 78 00 74 00 00 00
0020  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0030  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0040  00 00 00 00 00 00 00 00 00 00 00""",
        """
0000  16 00 00 00 7b 00 61 00 70 00 70 00 7d 00 5c 00
0010  63 00 61 00 63 00 68 00 65 00 00 00 00 00 00 00
0020  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0030  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0040  00 00 00 00 00 00 01""",
        """
0000  20 00 00 00 7b 00 74 00 6d 00 70 00 7d 00 5c 00
0010  72 00 65 00 64 00 69 00 73 00 74 00 2e 00 65 00
0020  78 00 65 00 0c 00 00 00 2f 00 71 00 75 00 69 00
0030  65 00 74 00 00 00 00 00 00 00 00 00 00 00 00 00
0040  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0050  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0060  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0070  00 00 00 00 00 00 00 00 00 10 00""",
        """
0000  22 00 00 00 7b 00 61 00 70 00 70 00 7d 00 5c 00
0010  63 00 6c 00 65 00 61 00 6e 00 75 00 70 00 2e 00
0020  65 00 78 00 65 00 00 00 00 00 00 00 00 00 10 00
0030  00 00 63 00 6c 00 65 00 61 00 6e 00 75 00 70 00
0040  31 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0050  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0060  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0070  00 00 00 00 00 00 00 00 00 00 01 00 00 00 00 00
0080  00""",
    )

    def v9_block(self) -> bytes:
        s = [""] * 39
        s[0], s[9], s[10], s[13] = "A", "1.0", "{autopf}\\A", "{app}"
        s[24] = s[25] = "yes"
        s[30] = s[31] = "x64compatible"
        s[34:39] = ["yes"] * 5
        strings = b"".join(w.text(x) for x in s) + bytes(16)
        self.assertEqual(len(strings), 304)
        body = strings + hx(self.V9_FIXED) + b"".join(hx(d) for d in self.V10)
        return body + bytes(24)  # six empty wizard image lists

    def test_v9_v10_header_and_entries(self):
        h, e, notes = inno._parse_block1(self.v9_block(), inno.F670, False, False)
        self.assertEqual(notes, [])
        self.assertEqual((h["privileges"], h["compression"], h["password"]), (2, 4, False))
        self.assertEqual(h["strings"]["app name"], "A")
        self.assertEqual(h["strings"]["architectures allowed"], "x64compatible")
        self.assertEqual(e["languages"], [inno.Language("english", 0x0409)])
        self.assertEqual(e["custom messages"][0].language, -1)
        self.assertEqual((e["types"][0].name, e["types"][0].kind), ("full", 1))
        self.assertEqual((e["components"][0].name, e["components"][0].options), ("main", 1))
        self.assertTrue(e["tasks"][0].unchecked)
        self.assertEqual(e["dirs"][0].options, 1)
        f = e["files"][0]
        self.assertEqual((f.dest, f.location, f.kind), ("{tmp}\\helper.dll", 0, inno.FILE_STORED))
        self.assertEqual(f.flags, {"delete after install", "skip if source does not exist",
                                   "do not copy"})
        self.assertEqual(e["icons"][0].target, "{app}\\a.exe")
        self.assertEqual(e["registry"][0].root_name, "HKLM")
        self.assertEqual(e["install delete"][0].kind, 0)
        self.assertEqual(e["uninstall delete"][0].kind, 1)
        r = e["run"][0]
        self.assertEqual((r.filename, r.parameters, r.show, r.wait), ("{tmp}\\redist.exe",
                                                                       "/quiet", 0, 0))
        self.assertEqual(r.flags, {"skip if silent"})
        self.assertFalse(r.silent_runs())
        self.assertEqual(e["uninstall run"][0].run_once_id, "cleanup1")

    def test_v9_block_must_end_exactly(self):
        with self.assertRaises(inno._Damage):
            inno._parse_block1(self.v9_block() + b"\0", inno.F670, False, False)
        with self.assertRaises(inno._Damage):
            inno._parse_block1(self.v9_block()[:-1], inno.F670, False, False)

    def test_v10_file_location(self):
        raw = hx("""
0000  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0010  00 00 00 00 00 00 00 00 03 00 00 00 00 00 00 00
0020  07 00 00 00 00 00 00 00 00 01 02 03 04 05 06 07
0030  08 09 0a 0b 0c 0d 0e 0f 10 11 12 13 14 15 16 17
0040  18 19 1a 1b 1c 1d 1e 1f 00 00 00 00 00 00 d9 01
0050  00 00 00 00 00 00 00 00 12""")
        (loc,) = inno._locations(raw, inno.F652, 1)
        self.assertEqual((loc.size, loc.chunk_size, loc.digest), (3, 7, bytes(range(32))))
        self.assertEqual((loc.compressed, loc.call_filter, loc.encrypted), (True, False, False))
        with self.assertRaises(inno.InnoError):
            inno._locations(raw + b"\0", inno.F652, 1)

    def test_v11_call_filter(self):
        orig = hx("0000  90 e8 10 00 00 00 e9 f0 ff ff ff 55 90 e8 01 02\n0010  03 00 c3 e8")
        at0 = hx("0000  90 e8 16 00 00 00 e9 fb ff ff 00 55 90 e8 13 02\n0010  03 00 c3 e8")
        at64k = hx("0000  90 e8 16 00 01 00 e9 fb ff 00 00 55 90 e8 13 02\n0010  04 00 c3 e8")
        for stored, start in ((at0, 0), (at64k, 0x10000)):
            buf = bytearray(stored)
            inno.unfilter_calls(buf, start)
            self.assertEqual(bytes(buf), orig)
        self.assertEqual(w.filter_calls(orig), at0)

    def test_v12_lzma2_dictionary(self):
        for p, size in ((0, 4096), (1, 6144), (16, 1 << 20), (22, 8 << 20), (23, 12 << 20),
                        (38, 2 << 30), (39, 3 << 30), (40, 0xFFFFFFFF)):
            self.assertEqual(inno.lzma2_dict_size(p), size)
        with self.assertRaises(inno.InnoError):
            inno.lzma2_dict_size(41)

    def test_v13_code_blob(self):
        blob = hx("""
0000  49 46 50 53 17 00 00 00 02 00 00 00 01 00 00 00
0010  00 00 00 00 00 00 00 00 00 00 00 00 1c 00 00 00
0020  00 06 00 00 00 00 00 2f 00 00 00 2a 00 00 00 01
0030  00 00 00 00 10 00 00 00 7b 00 73 00 72 00 63 00
0040  7d 00 5c 00 64 00 61 00 74 00 61 00 2d 00 30 00
0050  31 00 2e 00 62 00 69 00 09""")
        info = inno.read_code(blob)
        self.assertTrue(info.walked)
        self.assertEqual(info.strings, ["{src}\\data-01.bi"])
        broken = inno.read_code(b"XXXX" + blob[4:])
        self.assertFalse(broken.walked)
        self.assertIn("not checked", broken.note)


# --- synthetic installers ----------------------------------------------------------------

class Reading(Base):
    def test_every_format_reads(self):
        for fmt in inno.FORMATS:
            for variant in ((False, True) if fmt == "5.5.0" else (False,)):
                with self.subTest(fmt=fmt, variant=variant):
                    self.write(full_spec(fmt=fmt, variant=variant, compression=3))
                    s = inno.read_setup(self.setup)
                    self.assertEqual((s.format, s.variant), (fmt, variant))
                    self.assertEqual(s.table_version, 1 if inno._F[fmt] <= inno.F643 else 2)
                    self.assertEqual(len(s.files), 7)
                    self.assertEqual(len(s.locations), 5)
                    self.assertEqual([r.filename for r in s.run][:2],
                                     ["{tmp}\\vcredist_x64.exe", "{app}\\bin\\game.exe"])
                    self.assertEqual(s.uninstall_run[0].parameters, "--uninstall")
                    self.assertEqual((len(s.dirs), len(s.icons), len(s.ini), len(s.registry)),
                                     (1, 2, 1, 1))
                    self.assertEqual(s.files[0].kind, inno.FILE_UNINSTALLER)
                    self.assertEqual(s.files[2].location, s.files[1].location)
                    if not s.verified:
                        self.assertTrue(any("unverified" in n for n in s.notes))

    def test_header_stored_and_lzma_and_overlay(self):
        for compressed in (False, True):
            self.write(full_spec(header_compressed=compressed, overlay=b"signature" * 50))
            s = inno.read_setup(self.setup)
            self.assertEqual(s.app_name, "Test App")
            self.assertEqual(s.offset_exe, w.build(full_spec(
                header_compressed=compressed))[1]["offset_exe"])

    def test_refused_ids(self):
        data, _ = w.build(full_spec(fmt="5.5.7"))
        for new, reason in ((b"Inno Setup Setup Data (5.5.7)\0\0\0\0", "ANSI"),
                            (b"Inno Setup Setup Data (5.6.1) (u)", "development"),
                            (b"Inno Setup Setup Data (5.5.7) (U)", "modified"),
                            (b"Inno Setup Setup Data (9.9.9) (u)", "knows"),
                            (b"Something Else Entirely Here!!!!!", "knows")):
            with self.subTest(reason=reason):
                self.write(data.replace(b"Inno Setup Setup Data (5.5.7) (u)", new))
                with self.assertRaisesRegex(inno.InnoError, reason):
                    inno.read_setup(self.setup)
        data, _ = w.build(full_spec(fmt="6.7.0"))
        self.write(data.replace(b"Setup Data (6.7.0)", b"Setup Data (7.2.0)"))
        with self.assertRaisesRegex(inno.InnoError, "development build"):
            inno.read_setup(self.setup)

    def test_fully_encrypted_is_refused(self):
        self.write(full_spec(fmt="7.1.1", encryption_header=2))
        with self.assertRaisesRegex(inno.InnoError, "fully encrypted"):
            inno.read_setup(self.setup)
        self.write(full_spec(fmt="6.7.0", encryption_header=3))
        with self.assertRaisesRegex(inno.InnoError, "unknown value"):
            inno.read_setup(self.setup)

    def test_damage_is_reported(self):
        data, facts = w.build(full_spec())
        cases = {
            "table": (0x200 + 88 + 20, "offset table is damaged"),
            "block header": (facts["offset0"] + 64 + 53 + 1, "header CRC-32"),
            "chunk": (facts["offset0"] + 64 + 53 + 4 + 9 + 4 + 10, "chunk CRC-32"),
        }
        for name, (pos, reason) in cases.items():
            with self.subTest(name):
                bad = bytearray(data)
                bad[pos] ^= 0x55
                self.write(bytes(bad))
                with self.assertRaisesRegex(inno.InnoError, reason):
                    inno.read_setup(self.setup)
        self.write(data[:facts["offset0"] + 200])
        with self.assertRaises(inno.InnoError):
            inno.read_setup(self.setup)

    def test_hostile_bytes_never_escape(self):
        """Bytes inside the header and file location blocks are changed and the blocks framed
        again with valid CRC-32 values, so every case reaches the parsers; a setup that still
        reads goes on through the summary, detect(), the plan and the extraction."""
        rnd = random.Random(5)
        outside = {p for p in self.base.rglob("*")}
        parsed = refused = 0
        for fmt, variant in (("5.5.0", True), ("6.3.0", False), ("7.1.1", False)):
            spec = full_spec(fmt=fmt, variant=variant, compression=1, header_compressed=False,
                             code=w.code_blob(["{src}\\data-01.bin"]))

            def mutate(raw: bytes) -> bytes:
                bad = bytearray(raw)
                for _ in range(rnd.randint(1, 4)):
                    if bad:
                        bad[rnd.randrange(len(bad))] = rnd.getrandbits(8)
                if bad and rnd.random() < 0.1:
                    del bad[rnd.randrange(len(bad)):]
                return bytes(bad)

            for k in range(120):
                in_block1 = rnd.random() < 0.7
                data, _ = w.build(spec, block1_hook=mutate if in_block1 else None,
                                  block2_hook=None if in_block1 else mutate)
                self.write(data)
                with self.subTest(fmt=fmt, case=k):
                    try:
                        s = inno.read_setup(self.setup)
                    except inno.InnoError as exc:
                        self.assertNotIn("CRC-32", str(exc))
                        refused += 1
                        continue
                    parsed += 1
                    inno.summarise(s)
                    detect.describe(detect.detect(self.setup))
                    try:
                        plan = inno.plan_inno_extract(self.setup, "p1")
                    except ExtractRefused:
                        continue
                    plan.describe()
                    try:
                        inno.extract_inno_files(plan)
                    except ExtractRefused:
                        pass
                    if self.dest().exists():
                        shutil.rmtree(self.dest())
        self.assertGreater(parsed, 20)
        self.assertGreater(refused, 20)
        after = {p for p in self.base.rglob("*")} - {self.setup, self.dest().parent}
        self.assertEqual(after - outside, set(), "nothing is written outside the destination")

    def test_caps(self):
        self.write(full_spec(header_compressed=False))
        for name, value, reason in (("MAX_BLOCK1", 2000, "larger than"),
                                    ("MAX_STRING", 4, "longer than"),
                                    ("MAX_COUNT", 0, "impossible")):
            with mock.patch.object(inno, name, value), \
                    self.assertRaisesRegex(inno.InnoError, reason):
                inno.read_setup(self.setup)
        self.write(full_spec(header_compressed=True))
        with mock.patch.object(inno, "MAX_BLOCK1", 2000), \
                self.assertRaisesRegex(inno.InnoError, "more than"):
            inno.read_setup(self.setup)

    def test_variant_and_ambiguous_5_5_0(self):
        self.write(full_spec(fmt="5.5.0", variant=True, encryption_flag=True, decrypt_dll=False))
        s = inno.read_setup(self.setup)
        self.assertTrue(s.variant)
        self.assertEqual(s.encryption, 1)
        self.write(full_spec(fmt="5.5.0", encryption_flag=True, decrypt_dll=False))
        with self.assertRaisesRegex(inno.InnoError, "ambiguous or damaged"):
            inno.read_setup(self.setup)
        self.write(full_spec(fmt="5.5.0", unknown_header_flag=True))
        with self.assertRaisesRegex(inno.InnoError, "ambiguous or damaged"):
            inno.read_setup(self.setup)

    def test_not_inno(self):
        self.write(w.pe_with_table(b"x" * 44, resource_id=1) + b"rest")
        with self.assertRaisesRegex(inno.InnoError, "no Inno Setup offset table"):
            inno.read_setup(self.setup)
        (self.src / "setup-0.bin").write_bytes(b"Inno Setup Setup Data (6.7.0)".ljust(64, b"\0"))
        with self.assertRaisesRegex(inno.InnoError, "separate files"):
            inno.read_setup(self.setup)
        self.write(b"not a program at all")
        with self.assertRaisesRegex(inno.InnoError, "not a Windows program"):
            inno.read_setup(self.setup)
        os.unlink(self.setup)
        os.symlink(self.src / "setup-0.bin", self.setup)
        with self.assertRaises(inno.InnoError):
            inno.read_setup(self.setup)

    def test_code_strings_and_imports(self):
        blob = w.code_blob(["{src}\\data-01.bin", "{app}\\x.txt", "bad\x01"],
                           [("files:helper.dll", "DoIt")], ansi=["{src}\\data-02.bin"])
        self.write(full_spec(code=blob))
        s = inno.read_setup(self.setup)
        self.assertTrue(s.code.walked)
        self.assertEqual(s.code.strings, ["{src}\\data-01.bin", "{app}\\x.txt",
                                          "{src}\\data-02.bin"])
        self.assertEqual(s.code.imports, ["files:helper.dll: DoIt"])
        old = inno.read_code(w.code_blob(["{src}\\a.bin"], build_no=20))
        self.assertEqual(old.strings, ["{src}\\a.bin"])

    @staticmethod
    def shared_code(procs: int, code: bytes) -> bytes:
        """A [Code] container whose procedures all point at the same code bytes."""
        head = struct.pack("<7I", 0x53504649, 23, 1, procs, 0, 0, 0)
        types = w.u8(28) + w.u32(0)
        off = len(head) + len(types) + 9 * procs
        return head + types + (w.u8(0) + w.u32(off) + w.u32(len(code))) * procs + code

    def test_code_ranges_are_scanned_once(self):
        raw = "{src}\\x.bin".encode("utf-16-le")
        code = bytes(4 << 20) + b"\x01" + w.u32(0) + w.u32(len(raw) // 2) + raw
        info = inno.read_code(self.shared_code(2000, code))
        self.assertTrue(info.walked)
        self.assertEqual(info.strings, ["{src}\\x.bin"])  # 2,000 procedures, one scan
        self.assertEqual(inno._merge_ranges([(10, 5), (0, 4), (12, 10), (4, 1), (30, 1)]),
                         [(0, 5), (10, 22), (30, 31)])

    def test_code_scan_has_a_budget(self):
        with mock.patch.object(inno, "MAX_CODE_CANDIDATES", 1000):
            info = inno.read_code(self.shared_code(1, b"\x01" * 5000))
        self.assertTrue(info.walked)
        self.assertIn("stopped at its limit", info.note)
        self.write(full_spec())
        s = inno.read_setup(self.setup)
        s.code = info
        self.assertIn("stopped at its limit", "\n".join(inno.summarise(s)))

    def test_entry_lists_have_a_total_budget(self):
        self.write(full_spec())
        inno.read_setup(self.setup)
        with mock.patch.object(inno, "MAX_ENTRIES", 10), \
                self.assertRaisesRegex(inno.InnoError, "more than 10 entries"):
            inno.read_setup(self.setup)
        for record in (inno.Location, inno.FileEntry, inno.RunEntry):
            self.assertIn("__slots__", vars(record))

    def test_unknown_compression_note_wording(self):
        self.write(full_spec(compression=5, files=[w.WFile("", kind="uninstaller")]))
        s = inno.read_setup(self.setup)
        self.assertTrue(any(inno.METHOD_OTHER in n for n in s.notes), s.notes)
        self.assertFalse(any("does not know" in n and "compression" in n for n in s.notes))

    def test_slices_per_disk_out_of_range(self):
        self.write(full_spec(spanning=True, slices_per_disk=3, first_slice=4))
        self.assertEqual(inno.slice_names(inno.read_setup(self.setup)), ["setup-2b.bin"])
        self.write(full_spec(spanning=True, slices_per_disk=0x7FFFFFFF, first_slice=0x300000))
        s = inno.read_setup(self.setup)
        self.assertTrue(any("slices-per-disk" in n for n in s.notes))
        self.assertEqual(inno.slice_names(s), [])
        self.assertIn("names not known", "\n".join(inno.summarise(s)))
        self.assertIn("names not known", detect.describe(detect.detect(self.setup)))
        with self.assertRaisesRegex(ExtractRefused, "names not known"):
            inno.plan_inno_extract(self.setup, "p1")
        self.write(full_spec(fmt="5.5.0", variant=True, slices_per_disk=27))
        with self.assertRaisesRegex(inno.InnoError, "ambiguous or damaged"):
            inno.read_setup(self.setup)

    def test_detect_survives_a_summary_error(self):
        self.write(full_spec())
        with mock.patch.object(inno, "summarise", side_effect=ValueError("boom")):
            text = detect.describe(detect.detect(self.setup))
        self.assertIn("its summary could not be made", text)


class Evaluate(unittest.TestCase):
    def test_expressions(self):
        t = {"main", "lang\\en"}
        cases = {"main": True, "extras": False, "main or extras": True, "main and extras": False,
                 "main and not extras": True, "extras main": True, "(extras or main) and "
                 "lang\\en": True, "not (main)": False, "MAIN": True, "": True}
        for expr, want in cases.items():
            self.assertIs(inno.evaluate(expr, t), want, expr)
        for bad in ("(main", "main)", "and", "main and", "!main", "*"):
            self.assertIsNone(inno.evaluate(bad, t), bad)


class Plan(Base):
    def test_summary_lines(self):
        (self.src / "data-01.bin").write_bytes(b"1")
        (self.src / "data-02.bin").write_bytes(b"2")
        blob = w.code_blob(["{src}\\data-01.bin", "{src}\\records.inf", "game-01.arc"])
        files = full_files() + [w.WFile("{app}\\data\\", kind="external",
                                        source="{src}\\data-01.bin",
                                        flags=("skip if source does not exist",))]
        self.write(full_spec(code=blob, files=files, password=True))
        s = inno.read_setup(self.setup)
        text = "\n".join(inno.summarise(s))
        self.assertIn("what Inno itself writes: 6 files stored in setup.exe", text)
        self.assertIn("1 external file", text)
        self.assertIn("{tmp}\\vcredist_x64.exe /install /quiet /norestart (waits until it ends)"
                      "; looks like a redistributable", text)
        self.assertIn("{app}\\bin\\game.exe --first-run (waits until it ends, hidden)", text)
        self.assertIn("2 entries are skipped in a silent install", text)
        self.assertIn("data-01.bin: present (and 1 numbered after it)", text)
        self.assertIn("warning: records.inf not found beside the installer; a silent run is "
                      "likely to stop", text)
        self.assertIn("game-01.arc: not found", text)
        self.assertIn("password: Setup asks for a password", text)

    def test_preflight_symlinks_and_subfolders(self):
        real = self.base / "elsewhere.bin"
        real.write_bytes(b"x")
        os.symlink(real, self.src / "pack-01.bin")
        (self.src / "Data").mkdir()
        (self.src / "Data" / "res-01.bin").write_bytes(b"1")
        (self.src / "Data" / "res-02.bin").write_bytes(b"2")
        blob = w.code_blob(["{src}\\pack-01.bin", "{src}\\data\\res-01.bin",
                            "{src}\\data\\gone-01.bin", "{src}/data/RES-02.bin",
                            "{src}\\a\\b\\c\\deep.bin", "{src}\\..\\up.bin",
                            "{src}\\nofolder\\x.bin"])
        self.write(full_spec(code=blob))
        s = inno.read_setup(self.setup)
        got = {b.name: b for b in inno.beside_files(s)}
        self.assertTrue(got["pack-01.bin"].present)
        self.assertTrue(got["Data\\res-01.bin"].present)
        self.assertEqual(got["Data\\res-01.bin"].siblings, 1)
        self.assertTrue(got["Data\\res-02.bin"].present)
        self.assertFalse(got["Data\\gone-01.bin"].present)
        self.assertFalse(got["nofolder\\x.bin"].present)
        self.assertFalse(got["a\\b\\c\\deep.bin"].checked)
        self.assertFalse(any("up.bin" in n for n in got))
        text = "\n".join(inno.summarise(s))
        self.assertIn("pack-01.bin: present", text)
        self.assertIn("deep.bin: not checked", text)
        warning = next(line for line in text.splitlines() if line.startswith("warning:"))
        self.assertIn("gone-01.bin", warning)
        self.assertNotIn("pack-01.bin", warning)
        self.assertNotIn("deep.bin", warning)

    def test_redistributable_names(self):
        for name in ("{tmp}\\vcredist_x64.exe", "{app}\\_Redist\\x.exe", "{tmp}\\DXSETUP.exe",
                     "{app}\\redist\\anything.msi", "{tmp}\\dotNetFx40.exe"):
            self.assertTrue(inno.looks_redistributable(name), name)
        for name in ("{app}\\game.exe", "{app}\\bin\\redistributor.exe", ""):
            self.assertFalse(inno.looks_redistributable(name), name)

    def test_detect_shows_inno_evidence(self):
        self.write(full_spec())
        det = detect.detect(self.setup)
        ev = [e for e in det.evidence if e.format == "Inno Setup"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0].confidence, detect.CONF_LIKELY)
        self.assertIn("setup data format 6.7.0", ev[0].detail)
        text = detect.describe(det)
        self.assertIn("what Inno itself writes", text)
        self.assertIn("route: not chosen here", text)

    def test_detect_falls_back_to_the_id_string(self):
        data, _ = w.build(full_spec(fmt="7.1.1", encryption_header=2))
        self.write(data)
        det = detect.detect(self.setup)
        (ev,) = [e for e in det.evidence if e.format == "Inno Setup"]
        self.assertIn("(7.1.1)", ev.detail)
        self.assertIn("fully encrypted", ev.lines[0])


class Extract(Base):
    def plan(self):
        return inno.plan_inno_extract(self.setup, "p1")

    def tree(self) -> dict:
        root = self.dest()
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    def test_each_method_and_solid(self):
        for method in (0, 1, 2, 3, 4):
            for solid in (False, True):
                for fmt in ("5.5.7", "6.3.0", "6.7.0", "7.1.1"):
                    with self.subTest(method=method, solid=solid, fmt=fmt):
                        if self.dest().exists():
                            for p in sorted(self.dest().rglob("*"), reverse=True):
                                p.rmdir() if p.is_dir() else p.unlink()
                            self.dest().rmdir()
                        self.write(full_spec(fmt=fmt, compression=method, solid=solid))
                        res = inno.extract_inno_files(self.plan())
                        self.assertTrue(res.ok, res.lines())
                        self.assertEqual(self.tree(), {
                            "readme.txt": TEXT, "docs/README-copy.txt": TEXT, "empty.txt": b"",
                            "bin/game.exe": EXE,
                            "data/sub/b.snd": bytes(range(256)) * 40})

    def test_plan_lists_skipped(self):
        self.write(full_spec())
        plan = self.plan()
        text = "\n".join(plan.describe())
        self.assertIn("in total: 5 files", text)
        self.assertIn("{tmp}\\vcredist_x64.exe: looks like a redistributable", text)
        self.assertIn("(no name): Setup's uninstaller", text)
        self.assertIsNone(plan.problem())

    def test_default_choice(self):
        spec = full_spec(
            types=[("full", 0, False), ("compact", 0, False)],
            components=[("main", "full,compact", 1), ("extras", "full", 0),
                        ("lang", "full,compact", 0), ("lang\\en", "full,compact", 8),
                        ("lang\\de", "", 8)],
            tasks=[("desktopicon", 2), ("redist", 0)],
            files=[w.WFile("{app}\\readme.txt", TEXT, components="main"),
                   w.WFile("{app}\\extras\\bonus.txt", b"bonus", components="extras"),
                   w.WFile("{app}\\lang\\strings.txt", b"en", components="lang\\en"),
                   w.WFile("{app}\\lang\\strings.txt", b"de", components="lang\\de"),
                   w.WFile("{app}\\icon.txt", b"i", tasks="desktopicon"),
                   w.WFile("{app}\\task.txt", b"t", tasks="redist"),
                   w.WFile("{app}\\de-only.txt", b"x", languages="german"),
                   w.WFile("{app}\\checked.txt", b"c", check="IsWine")])
        self.write(spec)
        plan = self.plan()
        res = inno.extract_inno_files(plan)
        self.assertTrue(res.ok)
        self.assertEqual(self.tree(), {"readme.txt": TEXT, "extras/bonus.txt": b"bonus",
                                       "lang/strings.txt": b"en", "task.txt": b"t"})
        reasons = dict(plan.skipped)
        self.assertEqual(reasons["{app}\\checked.txt"], "depends on a [Code] check function")
        self.assertEqual(reasons["{app}\\icon.txt"], "not part of the default tasks")
        self.assertEqual(reasons["{app}\\de-only.txt"], "not part of the default languages")

    def test_same_destination_twice(self):
        files = [w.WFile("{app}\\A.txt", b"first"), w.WFile("{app}\\a.txt", b"second")]
        self.write(full_spec(files=files))
        plan = self.plan()
        self.assertTrue(any("later one is kept" in n for n in plan.notes))
        inno.extract_inno_files(plan)
        self.assertEqual(self.tree(), {"a.txt": b"second"})

    def test_refusals(self):
        cases = [
            (full_spec(password=True), "password-protected"),
            (full_spec(spanning=True), "separate disk files \\(setup-1.bin\\)"),
            (full_spec(fmt="6.7.0", encryption_header=1,
                       files=[w.WFile("{app}\\x", b"x", encrypted=True)]), "encrypted"),
            (full_spec(fmt="6.3.0", encryption_flag=True,
                       files=[w.WFile("{app}\\x", b"x", encrypted=True)]), "encrypted"),
            (full_spec(files=[w.WFile("{app}\\..\\evil.txt", b"x")]), "not safe"),
            (full_spec(files=[w.WFile("{app}\\con.txt", b"x")]), "Windows cannot use"),
            (full_spec(files=[w.WFile("{app}\\x", b"1"), w.WFile("{app}\\x\\y", b"2")]),
             "inside a file"),
            (full_spec(files=[w.WFile("{app}\\x\\y", b"1"), w.WFile("{app}\\X", b"2")]),
             "same name as a folder"),
            (full_spec(files=[w.WFile("{tmp}\\helper.dll", b"x", flags=("do not copy",))]),
             "no files for \\{app\\}"),
            (full_spec(compression=5, files=[w.WFile("", kind="uninstaller")]),
             "method gitfurl does not handle"),
        ]
        for spec, reason in cases:
            with self.subTest(reason=reason):
                self.write(spec)
                with self.assertRaisesRegex(ExtractRefused, reason):
                    self.plan()
        self.assertFalse(self.dest().exists())

    def test_bad_hash_is_removed(self):
        files = [w.WFile("{app}\\good.txt", b"good"), w.WFile("{app}\\bad.txt", b"bad",
                                                             bad_hash=True)]
        self.write(full_spec(files=files))
        res = inno.extract_inno_files(self.plan())
        self.assertFalse(res.ok)
        self.assertEqual(res.hash_errors, ["{app}\\bad.txt"])
        self.assertEqual(self.tree(), {"good.txt": b"good"})
        self.assertIn("removed again", "\n".join(res.lines()))

    def test_stop_keeps_checked_files_only(self):
        self.write(full_spec(solid=True))
        cancel = threading.Event()
        res = inno.extract_inno_files(self.plan(), cancel=cancel,
                                      on_progress=lambda done, total: cancel.set())
        self.assertTrue(res.stopped)
        self.assertEqual(res.files, 2)  # the first data location: readme.txt and its copy
        self.assertEqual(set(self.tree()), {"readme.txt", "docs/README-copy.txt"})

    def test_destination_must_be_new_and_installer_unchanged(self):
        self.write(full_spec())
        plan = self.plan()
        self.dest().mkdir(parents=True)
        with self.assertRaisesRegex(ExtractRefused, "already exists"):
            inno.extract_inno_files(plan)
        self.dest().rmdir()
        self.write(full_spec(files=full_files()[:3]))
        with self.assertRaisesRegex(ExtractRefused, "changed after the plan"):
            inno.extract_inno_files(plan)
        self.assertFalse(self.dest().exists())

    def patch_chunk(self, spec, at: int, raw: bytes):
        data, facts = w.build(spec)
        bad = bytearray(data)
        pos = facts["offset1"] + 4 + at
        bad[pos:pos + len(raw)] = raw
        self.write(bytes(bad))

    def test_lzma_dictionary_limits(self):
        files = [w.WFile("{app}\\a.txt", TEXT)]
        self.patch_chunk(full_spec(compression=3, files=files), 1, w.u32((1 << 30) + 1))
        with self.assertRaisesRegex(ExtractRefused, "larger than 1 GiB"):
            self.plan()
        self.patch_chunk(full_spec(compression=3, files=files), 1, w.u32(1 << 30))
        plan = self.plan()
        self.assertLessEqual(plan.memory, len(TEXT) + 4096)
        self.assertTrue(inno.extract_inno_files(plan).ok)
        shutil.rmtree(self.dest())
        for prop, reason in ((40, "larger than 1 GiB"), (41, "invalid LZMA2 property")):
            with self.subTest(prop=prop):
                self.patch_chunk(full_spec(compression=4, files=files), 0, bytes([prop]))
                with self.assertRaisesRegex(ExtractRefused, reason):
                    self.plan()
        self.assertFalse(self.dest().exists())

    @staticmethod
    def set_offset(index: int, value: int):
        """A block 2 hook (6.7.0 layout: 89-byte locations) that moves one file's offset."""
        def hook(raw: bytes) -> bytes:
            out = bytearray(raw)
            struct.pack_into("<q", out, index * 89 + 16, value)
            return bytes(out)
        return hook

    def test_overlap_and_unexplained_decoding_are_refused(self):
        spec = full_spec(solid=True, files=[w.WFile("{app}\\a.txt", b"a" * 1000),
                                            w.WFile("{app}\\b.txt", b"b" * 1000)])
        self.write(w.build(spec, block2_hook=self.set_offset(1, 500))[0])
        with self.assertRaisesRegex(ExtractRefused, "overlap"):
            self.plan()
        self.write(w.build(spec, block2_hook=self.set_offset(1, 512 << 20))[0])
        with self.assertRaisesRegex(ExtractRefused, "more than its files account for"):
            self.plan()
        self.assertFalse(self.dest().exists())

    def test_plan_shows_bytes_decoded_and_thrown_away(self):
        self.write(full_spec(solid=True, files=[
            w.WFile("{app}\\a.txt", b"a", tasks="extras"),
            w.WFile("{app}\\b.txt", TEXT, tasks="extras"),
            w.WFile("{app}\\c.txt", b"c")], tasks=[("extras", 2)]))
        plan = self.plan()
        text = "\n".join(plan.describe())
        self.assertEqual(plan.total_size, 1)
        self.assertEqual(plan.decoded, len(TEXT) + 2)
        self.assertIn("1 byte to write", text)
        self.assertIn("thrown away", text)

    def test_stop_in_the_middle_of_a_file(self):
        class Cancel:
            armed, calls = False, 0

            def is_set(self):
                if self.armed:
                    self.calls += 1
                return self.calls >= 4

        def progress(done, total):
            cancel.armed = True

        cancel = Cancel()
        big = bytes(RND.getrandbits(8) for _ in range(256)) * (5 << 12)
        self.write(full_spec(compression=0, files=[w.WFile("{app}\\a.txt", b"a"),
                                                   w.WFile("{app}\\big.bin", big)]))
        res = inno.extract_inno_files(self.plan(), cancel=cancel, on_progress=progress)
        self.assertTrue(res.stopped)
        self.assertEqual(res.removed, "{app}\\big.bin")
        self.assertEqual(self.tree(), {"a.txt": b"a"})
        self.assertIn("incomplete file (removed again)", "\n".join(res.lines()))

    def test_damaged_chunk_reports_error(self):
        data, facts = w.build(full_spec(files=[w.WFile("{app}\\a.bin", TEXT)]))
        bad = bytearray(data)
        bad[facts["offset1"] + 40] ^= 0xFF
        self.write(bytes(bad))
        res = inno.extract_inno_files(self.plan())
        self.assertFalse(res.ok)
        self.assertTrue(res.error or res.hash_errors)
        self.assertEqual(self.tree(), {})


class Cli(Base):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_dry_run_and_real(self):
        self.write(full_spec())
        rc, out, _ = self.run_cli("extract-inno", str(self.setup), "--prefix", "p1", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("DRY RUN", out)
        self.assertFalse(self.dest().exists())
        with mock.patch("gitfurl.cli.running_as_root", return_value=False), \
                mock.patch("sys.stdin.isatty", return_value=False):
            rc, out, _ = self.run_cli("extract-inno", str(self.setup), "--prefix", "p1")
        self.assertEqual(rc, 2)
        self.assertFalse(self.dest().exists())
        with mock.patch("gitfurl.cli.running_as_root", return_value=False):
            rc, out, _ = self.run_cli("extract-inno", str(self.setup), "--prefix", "p1", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertIn("Extraction finished: 5 files", out)

    def test_refusal_message(self):
        self.write(full_spec(password=True))
        rc, _, err = self.run_cli("extract-inno", str(self.setup), "--prefix", "p1", "--dry-run")
        self.assertEqual(rc, 1)
        self.assertIn("password-protected", err)
        self.assertIn("Nothing was written", err)


if __name__ == "__main__":
    unittest.main()
