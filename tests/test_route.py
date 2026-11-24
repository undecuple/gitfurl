"""Route choice and the native install route. Synthetic files only: a fake Inno Setup program
(just its ID string) and FreeArc archives built in memory by tests/farc_writer.py. No process
is started in these tests (Popen and run are patched to fail unless a test mocks wineboot)."""

import contextlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
import zlib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

try:
    from farc_writer import F, make_archive
except ImportError:  # python -m unittest tests.test_route
    from tests.farc_writer import F, make_archive

try:
    import inno_writer as w
except ImportError:  # python -m unittest tests.test_route
    from tests import inno_writer as w

from gitfurl import cli
from gitfurl.detect import ROUTE_NATIVE, ROUTE_UNSUPPORTED, ROUTE_WINE, describe, detect
from gitfurl.route import (
    NATIVE_WINEBOOT_ENV,
    archives_not_linked,
    choose_route,
    is_redist,
    link_check,
    unpack_memory_problem,
    script_names,
)

INNO = b"MZ" + b"\0" * 200 + b"Inno Setup Setup Data (5.5.0) (u)" + b"\0" * 40
GAME = b"MZ" + bytes(range(256)) * 40
TEXT = b"some text\n" * 500


def lzma_blocks():
    return [("lzma:1mb", [F("bin", is_dir=True), F("Game.exe", GAME, dirnum=1),
                          F("readme.txt", TEXT)])], ("", "bin")


def chain_blocks():
    data = bytes(range(256)) * 64
    return [("rep:1mb+exe+delta+lzma:1mb", [F("data", is_dir=True), F("level1.dat", data, dirnum=1),
                                            F("level2.dat", data[::-1], dirnum=1)]),
            ("storing", [F("_Redist", is_dir=True),
                         F("vcredist_x64.exe", b"MZ redist", dirnum=2)])], ("", "data", "_Redist")


@contextlib.contextmanager
def no_process():
    boom = AssertionError("no process may start")
    with mock.patch("subprocess.Popen", side_effect=boom), \
            mock.patch("subprocess.run", side_effect=boom), \
            mock.patch("os.system", side_effect=boom):
        yield


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def snapshot(root: Path):
    out = []
    for dirpath, dirs, files in os.walk(root):
        for n in sorted(dirs + files):
            p = Path(dirpath, n)
            st = p.lstat()
            out.append((str(p), st.st_mode, st.st_size, st.st_mtime_ns))
    return sorted(out)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.src = self.base / "NAME game"  # the archives (NAME-01.bin) are linked by name
        self.src.mkdir()
        self.setup = self.src / "setup.exe"
        self.setup.write_bytes(INNO)
        self.data = self.base / "data"
        self.state = self.base / "state"
        self.env = {"XDG_DATA_HOME": str(self.data), "XDG_STATE_HOME": str(self.state),
                    "HOME": str(self.base)}
        patcher = mock.patch.dict("os.environ", self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.prefix = self.data / "gitfurl" / "prefixes" / "p1"

    def arc(self, name, blocks_folders, **kw):
        blocks, folders = blocks_folders
        (self.src / name).write_bytes(make_archive(blocks, folders, **kw))
        return self.src / name

    def choose(self, requested="auto", installer=None):
        inst = installer or self.setup
        return choose_route(inst, "p1", detect(inst), requested, self.env)

    def make_prefix(self):
        (self.prefix / "drive_c").mkdir(parents=True)


class ChooseRouteTests(Base):
    def test_inno_with_supported_archives_is_native_chosen_by_gitfurl(self):
        self.arc("NAME-01.bin", lzma_blocks())
        self.arc("NAME-02.bin", chain_blocks())
        with no_process():
            choice = self.choose()
        self.assertEqual(choice.route, ROUTE_NATIVE)
        self.assertEqual(choice.chosen_by, "gitfurl")
        line = choice.line()
        self.assertIn("route: native (chosen by gitfurl)", line)
        self.assertIn("an Inno Setup installer; its payload is 2 FreeArc archives", line)
        self.assertIn("storing, lzma, exe, delta, rep, 4x4 (with lzma inside) or srep", line)
        self.assertIn("the archives share the installer's name or its folder's", line)
        self.assertEqual(len(choice.extract.archives), 2)

    def test_unhandled_method_falls_back_to_wine_with_the_reason(self):
        self.arc("NAME-01.bin", ([("zzzmethod:9", [F("a.bin", b"x" * 50)])], ("",)))
        choice = self.choose()
        self.assertEqual(choice.route, ROUTE_WINE)
        self.assertIn("a method gitfurl does not handle", choice.line())
        self.assertIn("Setup unpacks its own archives", choice.line())
        self.assertNotIn("zzzmethod", choice.line())

    def test_checksum_mismatch_falls_back_to_wine_with_the_reason(self):
        self.arc("NAME-01.bin", lzma_blocks(), bad_desc_crc=True)
        choice = self.choose()
        self.assertEqual(choice.route, ROUTE_WINE)
        self.assertIn("NAME-01.bin", choice.line())
        self.assertIn("CRC", choice.line())

    def test_unsafe_name_or_case_clash_falls_back_to_wine(self):
        cases = {
            "unsafe": ([("storing", [F("../x", b"1")])], ("",)),
            "clash": ([("storing", [F("A.txt", b"1"), F("a.txt", b"2")])], ("",)),
        }
        for label, bf in cases.items():
            for p in self.src.glob("*.bin"):
                p.unlink()
            self.arc("NAME-01.bin", bf)
            choice = self.choose()
            self.assertEqual(choice.route, ROUTE_WINE, label)
            self.assertIn("cannot be unpacked by gitfurl", choice.line(), label)

    def test_no_archives_is_wine(self):
        choice = self.choose()
        self.assertEqual(choice.route, ROUTE_WINE)
        self.assertIn("no FreeArc archives", choice.line())

    def test_other_windows_program_with_archives_is_wine(self):
        other = self.src / "other.exe"
        other.write_bytes(b"MZ" + b"\0" * 500)
        self.arc("NAME-01.bin", lzma_blocks())
        choice = self.choose(installer=other)
        self.assertEqual(choice.route, ROUTE_WINE)
        self.assertIn("the native route covers Inno Setup installers", choice.line())

    def test_a_freearc_archive_as_the_installer_is_native(self):
        inst = self.arc("game.arc", lzma_blocks())
        self.setup.unlink()
        choice = self.choose(installer=inst)
        self.assertEqual(choice.route, ROUTE_NATIVE)
        self.assertIn("a FreeArc archive;", choice.line())

    def test_route_native_refuses_when_not_possible(self):
        with self.assertRaises(ValueError) as cm:
            self.choose("native")
        self.assertIn("native route not possible", str(cm.exception))
        self.assertIn("no FreeArc archives", str(cm.exception))

    def test_route_wine_is_chosen_by_you(self):
        self.arc("NAME-01.bin", lzma_blocks())
        choice = self.choose("wine")
        self.assertEqual((choice.route, choice.chosen_by), (ROUTE_WINE, "you"))
        self.assertIsNone(choice.extract)

    def test_route_native_when_possible_is_chosen_by_you(self):
        self.arc("NAME-01.bin", lzma_blocks())
        choice = self.choose("native")
        self.assertEqual((choice.route, choice.chosen_by), (ROUTE_NATIVE, "you"))

    def test_unreadable_installer_is_unsupported(self):
        missing = self.src / "nope.exe"
        choice = choose_route(missing, "p1", detect(missing), "auto", self.env)
        self.assertEqual(choice.route, ROUTE_UNSUPPORTED)

    def test_describe_prints_the_choice(self):
        self.arc("NAME-01.bin", lzma_blocks())
        det = detect(self.setup)
        choice = choose_route(self.setup, "p1", det, "auto", self.env)
        text = describe(det, choice)
        self.assertIn("route: native (chosen by gitfurl)", text)
        self.assertIn("route: not chosen here", describe(det))

    def test_choosing_writes_nothing(self):
        self.arc("NAME-01.bin", lzma_blocks())
        before = snapshot(self.base)
        with no_process():
            self.choose()
        self.assertEqual(before, snapshot(self.base))

    def test_archives_not_linked_by_name_fall_back_to_wine(self):
        # Review finding: any readable archive in the folder made the route native, even one
        # Setup has nothing to do with.
        self.arc("unrelated.arc", lzma_blocks())
        with no_process():
            choice = self.choose()
        self.assertEqual(choice.route, ROUTE_WINE)
        self.assertIn("not linked to it by name", choice.line())
        self.assertIn("use --route native", choice.line())
        self.assertIsNone(choice.extract)
        self.assertEqual(self.choose("native").route, ROUTE_NATIVE)  # still possible on request

    def test_archives_with_different_base_names_fall_back_to_wine(self):
        self.arc("NAME-01.bin", lzma_blocks())
        self.arc("other-02.bin", chain_blocks())
        choice = self.choose()
        self.assertEqual(choice.route, ROUTE_WINE)
        self.assertIn("do not share one base name", choice.line())

    def test_links_by_name(self):
        def names(folder, inst, *arcs):
            return archives_not_linked(Path("/x", folder, inst),
                                       [Path("/x", folder, a) for a in arcs])

        self.assertIsNone(names("NAME game", "setup.exe", "NAME-01.bin", "NAME-02.bin"))
        self.assertIsNone(names("Pocket Game [pocket-game]", "setup.exe",
                                "pocket-game-01.bin", "pocket-game-02.bin"))
        self.assertIsNone(names("dl", "setup.exe", "setup-1.bin", "setup-2.bin"))
        self.assertIsNone(names("dl", "data.arc", "data.arc"))  # the installer is the archive
        self.assertIsNone(names("dl", "Data.arc", "Data.arc", "data-2.arc"))
        self.assertIsNotNone(names("dl", "setup.exe", "data1.bin"))
        self.assertIsNotNone(names("dl", "setup.exe", "01.bin"))  # no base name at all
        self.assertIsNotNone(names("ab", "setup.exe", "ab-01.bin"))  # too short to match a folder
        self.assertIsNotNone(names("NAME game", "setup.exe", "NAME-01.bin", "other.bin"))

    def script_setup(self, folder, *names, code=()):
        """A synthetic Inno Setup installer in `folder` whose script names `names` as external
        [Files] sources ({src}\\name) and `code` as [Code] strings."""
        where = self.base / folder
        where.mkdir(exist_ok=True)
        files = [w.WFile("{app}\\", kind="external", source="{src}\\" + n) for n in names]
        data, _ = w.build(w.Spec(files=files, code=w.code_blob(list(code)) if code else b""))
        inst = where / "setup.exe"
        inst.write_bytes(data)
        return inst

    def put(self, inst, name, blocks_folders):
        blocks, folders = blocks_folders
        (inst.parent / name).write_bytes(make_archive(blocks, folders))

    def test_archives_the_setup_script_names_are_linked(self):
        # Names unrelated to the installer and its folder: the name check alone says Wine.
        inst = self.script_setup("dl", "data-01.bin")
        self.put(inst, "data-01.bin", lzma_blocks())
        self.put(inst, "data-02.bin", chain_blocks())  # the numbered file after a named one
        with no_process():
            choice = choose_route(inst, "p1", detect(inst), "auto", self.env)
        self.assertEqual((choice.route, choice.chosen_by), (ROUTE_NATIVE, "gitfurl"),
                         choice.line())
        self.assertIn("the setup script names every archive", choice.line())
        self.assertEqual(script_names(inst), {"data-01.bin", "data-02.bin"})

    def test_archive_named_in_code_is_linked(self):
        inst = self.script_setup("dl", code=["{src}\\pack.arc"])
        self.put(inst, "pack.arc", lzma_blocks())
        choice = choose_route(inst, "p1", detect(inst), "auto", self.env)
        self.assertEqual(choice.route, ROUTE_NATIVE, choice.line())

    def test_an_archive_the_script_does_not_name_falls_back_to_wine(self):
        inst = self.script_setup("dl", "data-01.bin")
        self.put(inst, "data-01.bin", lzma_blocks())
        self.put(inst, "extra.arc", chain_blocks())
        choice = choose_route(inst, "p1", detect(inst), "auto", self.env)
        self.assertEqual(choice.route, ROUTE_WINE)
        line = choice.line()
        self.assertIn("the setup script names 1 of the 2 (not named: extra.arc;", line)
        self.assertIn("use --route native", line)
        choice = choose_route(inst, "p1", detect(inst), "native", self.env)
        self.assertEqual((choice.route, choice.chosen_by), (ROUTE_NATIVE, "you"))
        self.assertIn("you chose this route although the FreeArc archives", choice.line())

    def test_script_names_never_raises(self):
        self.assertIsNone(script_names(self.setup))  # INNO is a header only, not setup data
        self.assertIsNone(script_names(self.base / "missing.exe"))
        self.arc("unrelated.arc", lzma_blocks())
        self.assertIn("could not be read for the names it uses", self.choose().line())

    def test_link_check_with_script_names(self):
        inst, arcs = Path("/x/dl/setup.exe"), [Path("/x/dl/a.bin"), Path("/x/dl/b.bin")]
        self.assertEqual(link_check(inst, arcs, {"a.bin", "b.bin", "c.bin"}),
                         (None, "the setup script names every archive"))
        why, how = link_check(inst, arcs, {"a.bin"})
        self.assertIsNone(how)
        self.assertIn("do not share one base name, and the setup script names 1 of the 2", why)
        self.assertEqual(link_check(Path("/x/dl/a.arc"), [Path("/x/dl/a.arc")], set()),
                         (None, "the installer is the archive"))
        why, _ = link_check(Path("/x/dl/a.arc"), [Path("/x/dl/a.arc"), Path("/x/dl/z.arc")],
                            has_script=False)
        self.assertIn("the installer has no setup script gitfurl can read", why)
        # The name check still links archives the script does not name.
        self.assertEqual(link_check(inst, [Path("/x/dl/setup-1.bin")], set())[0], None)

    def test_setup_only_options_choose_wine_on_auto(self):
        # Review finding: --unattended or --portable were only listed as "do not apply" and the
        # run went native, so a scripted Setup run silently skipped Setup.
        self.arc("NAME-01.bin", lzma_blocks())
        for opt in ("--unattended", "--portable", "--show-changes", "--runtime proton",
                    "--wine-debug"):
            choice = choose_route(self.setup, "p1", detect(self.setup), "auto", self.env,
                                  setup_options=[opt])
            self.assertEqual((choice.route, choice.chosen_by), (ROUTE_WINE, "gitfurl"), opt)
            self.assertIn(f"only a Setup run uses ({opt})", choice.line())
            self.assertIn("--route native", choice.line())
            choice = choose_route(self.setup, "p1", detect(self.setup), "native", self.env,
                                  setup_options=[opt])
            self.assertEqual(choice.route, ROUTE_NATIVE, opt)

    def test_unpack_memory_problem_compares_with_memavailable_minus_the_reserve(self):
        gib = 1 << 30
        info = self.base / "meminfo"
        info.write_text(f"MemTotal: {4 * gib // 1024} kB\nMemAvailable: {2 * gib // 1024} kB\n")
        self.assertIsNone(unpack_memory_problem(0, str(info)))
        self.assertIsNone(unpack_memory_problem(gib // 2, str(info)))  # 2 GiB - 1 GiB reserve
        text = unpack_memory_problem(2 * gib, str(info))
        self.assertIn("may need up to 2.0 GiB", text)
        self.assertIn("MemAvailable 2.00 GiB", text)
        self.assertIn("could not read", unpack_memory_problem(1, str(self.base / "missing")))

    def test_redistributable_names(self):
        self.assertTrue(is_redist(["_Redist"], "x.exe"))
        self.assertTrue(is_redist([], "VCRedist_x86.exe"))
        self.assertTrue(is_redist(["DirectX"], "dxsetup.exe"))
        self.assertFalse(is_redist([], "Game.exe"))
        self.assertFalse(is_redist(["_Redist"], "readme.txt"))


class NativeCliTests(Base):
    def setUp(self):
        super().setUp()
        self.arc("NAME-01.bin", lzma_blocks())
        self.arc("NAME-02.bin", chain_blocks())

    def dest(self):
        return self.prefix / "drive_c" / "Games" / "p1"

    def test_dry_run_shows_every_step_and_changes_nothing(self):
        self.make_prefix()
        before = snapshot(self.base)
        with no_process():
            code, out, _ = run_cli("install", str(self.setup), "--prefix", "p1", "--dry-run",
                                   "--cleanup", "installer")
        self.assertEqual(code, 0)
        self.assertEqual(before, snapshot(self.base))
        self.assertIn("DRY RUN: nothing was executed, created or changed.", out)
        self.assertIn("route: native (chosen by gitfurl)", out)
        self.assertIn("Setup is not run. Skipped: Setup's own files", out)
        self.assertIn("step 1: unpack the archives", out)
        self.assertIn("step 2: list program candidates", out)
        self.assertIn("step 3: only after a complete unpack", out)
        self.assertNotIn("options that do not apply", out)
        self.assertIn("compared with MemAvailable", out)
        self.assertIn("One approval covers every step above.", out)
        self.assertNotIn("a real run would stop now", out)

    def test_route_native_lists_setup_only_options_as_not_applying(self):
        self.make_prefix()
        with no_process():
            code, out, _ = run_cli("install", str(self.setup), "--prefix", "p1", "--dry-run",
                                   "--route", "native", "--unattended")
        self.assertEqual(code, 0)
        self.assertIn("route: native (chosen by you)", out)
        self.assertIn("options that do not apply to the native route", out)
        self.assertIn("--unattended", out)

    def test_wine_addons_stays_native_and_says_it_does_not_apply(self):
        self.make_prefix()
        with no_process():
            code, out, _ = run_cli("install", str(self.setup), "--prefix", "p1", "--dry-run",
                                   "--wine-addons")
        self.assertEqual(code, 0)
        self.assertIn("route: native (chosen by gitfurl)", out)
        self.assertIn("--wine-addons does not apply to the native route", out)

    def test_stall_advice_does_not_change_the_route(self):
        # Asking for advice must not turn a native run into a Setup run (the window's own
        # stall advice never changes the route either).
        self.make_prefix()
        with no_process():
            code, out, _ = run_cli("install", str(self.setup), "--prefix", "p1", "--dry-run",
                                   "--stall-after", "600")
        self.assertEqual(code, 0)
        self.assertIn("route: native (chosen by gitfurl)", out)
        self.assertIn("--stall-after does not apply to the native route", out)

    def test_setup_only_options_on_auto_take_the_wine_route(self):
        self.make_prefix()
        before = snapshot(self.base)
        with no_process():
            code, out, _ = run_cli("install", str(self.setup), "--prefix", "p1", "--dry-run",
                                   "--unattended", "--no-memory-limit")
        self.assertEqual(code, 0)
        self.assertEqual(before, snapshot(self.base))
        self.assertIn("route: wine (chosen by gitfurl)", out)
        self.assertIn("only a Setup run uses (--unattended)", out)
        self.assertIn("/VERYSILENT", out)
        self.assertNotIn("Setup is not run.", out)

    def test_dry_run_with_a_missing_prefix_lists_its_creation(self):
        before = snapshot(self.base)
        with no_process(), mock.patch("shutil.which", return_value="/x/wine"):
            code, out, _ = run_cli("install", str(self.setup), "--prefix", "p1", "--dry-run",
                                   "--create-prefix", "--no-memory-limit")
        self.assertEqual(code, 0)
        self.assertEqual(before, snapshot(self.base))
        self.assertIn("step 1: create the prefix", out)
        self.assertIn("WINEDLLOVERRIDES=mscoree,mshtml= /x/wine wineboot --init", out)
        self.assertIn("Mono and Gecko install dialogs", out)
        self.assertNotIn("a real run would stop now", out)
        self.assertIn("step 2: unpack the archives", out)

    def test_real_run_writes_files_log_and_listings(self):
        self.make_prefix()
        with no_process():
            code, out, err = run_cli("install", str(self.setup), "--prefix", "p1", "--yes",
                                     "--detailed-log", "--progress", "--progress-interval",
                                     "0.000001")
        self.assertEqual(code, 0, err)
        dest = self.dest()
        self.assertEqual((dest / "bin" / "Game.exe").read_bytes(), GAME)
        self.assertEqual((dest / "readme.txt").read_bytes(), TEXT)
        for f in ("level1.dat", "level2.dat"):
            self.assertTrue((dest / "data" / f).is_file())
        self.assertIn("Files extracted; nothing was installed.", out)
        self.assertNotIn("installation succeeded", out)
        self.assertIn("[progress] native unpack:", out)
        self.assertIn("program candidates", out)
        self.assertIn("bin/Game.exe", out)
        self.assertIn("start it with: WINEPREFIX=", out)
        self.assertIn("redistributable installers found (not run):", out)
        self.assertIn("_Redist/vcredist_x64.exe", out)
        self.assertIn("Setup is not run.", out)
        logs = list((self.state / "gitfurl" / "logs").glob("p1-*.log"))
        self.assertEqual(len(logs), 1)
        self.assertEqual(stat.S_IMODE(logs[0].stat().st_mode), 0o600)
        self.assertIn("route: native", logs[0].read_text())
        ev_path = logs[0].with_suffix(".events.jsonl")
        self.assertEqual(stat.S_IMODE(ev_path.stat().st_mode), 0o600)
        events = [json.loads(ln) for ln in ev_path.read_text().splitlines()]
        kinds = [e["event"] for e in events]
        self.assertEqual(kinds[0], "start")
        self.assertEqual(events[0]["route"], "native")
        self.assertIn("progress", kinds)
        self.assertEqual(kinds[-1], "exit")
        self.assertEqual(events[-1]["status"], 0)

    def test_missing_prefix_without_create_is_refused(self):
        with no_process():
            code, _, err = run_cli("install", str(self.setup), "--prefix", "p1", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("prefix does not exist", err)
        self.assertFalse(self.prefix.exists())

    def test_create_prefix_runs_wineboot_once_then_unpacks(self):
        calls = []

        def fake_create(name, wine, env=None, run=None, extra_env=None, wrap=None, should_stop=None):
            calls.append((name, wine))
            self.assertEqual(extra_env, NATIVE_WINEBOOT_ENV)
            self.assertIsNone(should_stop)  # the command line stops wineboot with Ctrl-C
            (self.prefix / "drive_c").mkdir(parents=True)
            return 0

        with no_process(), mock.patch("shutil.which", return_value="/x/wine"), \
                mock.patch("gitfurl.prefixes.create_prefix", side_effect=fake_create):
            code, _, err = run_cli("install", str(self.setup), "--prefix", "p1", "--yes",
                                   "--create-prefix", "--no-memory-limit")
        self.assertEqual(code, 0, err)
        self.assertEqual(calls, [("p1", "/x/wine")])
        self.assertTrue((self.dest() / "bin" / "Game.exe").is_file())

    def test_dry_run_lists_every_reason_a_real_run_would_stop(self):
        # Review finding: without wine, or with a memory-limit problem, the dry run with
        # --create-prefix exited 0 with no warning while the real run refused.
        with no_process(), mock.patch("shutil.which", return_value=None), \
                mock.patch("gitfurl.memlimit.describe",
                           return_value=(["memory limit: on"], "memory limit: no user manager")):
            code, out, _ = run_cli("install", str(self.setup), "--prefix", "p1", "--dry-run",
                                   "--create-prefix")
        self.assertEqual(code, 0)
        self.assertIn("a real run would stop now: wine not found on PATH", out)
        self.assertIn("a real run would stop now: memory limit: no user manager", out)
        with no_process(), mock.patch("shutil.which", return_value=None):
            code, _, err = run_cli("install", str(self.setup), "--prefix", "p1", "--yes",
                                   "--create-prefix", "--no-memory-limit")
        self.assertEqual(code, 1)
        self.assertIn("wine not found", err)
        self.assertFalse(self.prefix.exists())

    def test_too_little_space_for_a_new_prefix_stops_before_it_is_created(self):
        # Review finding: the space check ran only after wineboot had created the prefix.
        small = mock.Mock(f_frsize=4096, f_bsize=4096, f_bavail=10, f_bfree=10, f_blocks=100,
                          f_files=10**6, f_favail=10**6)
        create = mock.Mock()
        with no_process(), mock.patch("shutil.which", return_value="/x/wine"), \
                mock.patch("gitfurl.extract.os.statvfs", return_value=small), \
                mock.patch("gitfurl.prefixes.create_prefix", create):
            code, out, _ = run_cli("install", str(self.setup), "--prefix", "p1", "--dry-run",
                                   "--create-prefix", "--no-memory-limit")
            self.assertEqual(code, 0)
            self.assertIn("a real run would stop now: not enough free space", out)
            self.assertIn("for the new prefix", out)
            code, _, err = run_cli("install", str(self.setup), "--prefix", "p1", "--yes",
                                   "--create-prefix", "--no-memory-limit")
        self.assertEqual(code, 1)
        self.assertIn("not enough free space", err)
        self.assertIn("Nothing was run or created.", err)
        create.assert_not_called()
        self.assertFalse(self.prefix.exists())

    def test_decoder_memory_above_free_memory_stops_unless_no_memory_limit(self):
        self.make_prefix()
        tight = (2 << 30, 1 << 30)  # MemTotal 2 GiB, MemAvailable 1 GiB: no room after reserve
        with no_process(), mock.patch("gitfurl.memlimit.read_meminfo", return_value=tight):
            code, out, _ = run_cli("install", str(self.setup), "--prefix", "p1", "--dry-run")
            self.assertEqual(code, 0)
            self.assertIn("a real run would stop now: the unpack may need up to", out)
            code, _, err = run_cli("install", str(self.setup), "--prefix", "p1", "--yes")
            self.assertEqual(code, 2)
            self.assertIn("--no-memory-limit", err)
            self.assertFalse(self.dest().exists())
            code, _, err = run_cli("install", str(self.setup), "--prefix", "p1", "--yes",
                                   "--no-memory-limit")
        self.assertEqual(code, 0, err)
        self.assertTrue((self.dest() / "bin" / "Game.exe").is_file())

    def test_incomplete_prefix_is_refused(self):
        # Review finding: a prefix whose creation timed out was accepted on a re-run.
        self.make_prefix()
        (self.prefix / "gitfurl-incomplete").write_bytes(b"")
        with no_process():
            code, _, err = run_cli("install", str(self.setup), "--prefix", "p1", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("not completely created", err)
        self.assertFalse(self.dest().exists())

    def test_native_prefix_creation_sets_the_overrides_and_marks_until_done(self):
        from gitfurl.route import NativeInstallPlan, native_prefix_create

        choice = choose_route(self.setup, "p1", detect(self.setup), "auto", self.env)
        plan = NativeInstallPlan(self.setup, "p1", self.prefix, choice, True, "/x/wine")
        seen = {}

        def run(argv, env, timeout, check):
            seen.update(argv=argv, env=env)
            seen["marker"] = (self.prefix / "gitfurl-incomplete").exists()
            return subprocess.CompletedProcess(argv, status)

        status = 0
        with no_process():
            self.assertEqual(native_prefix_create(plan, False, self.env, run), 0)
        self.assertEqual(seen["argv"], ["/x/wine", "wineboot", "--init"])
        self.assertEqual(seen["env"]["WINEDLLOVERRIDES"], "mscoree,mshtml=")
        self.assertEqual(seen["env"]["WINEPREFIX"], str(self.prefix))
        self.assertTrue(seen["marker"])
        self.assertFalse((self.prefix / "gitfurl-incomplete").exists())
        # a failed wineboot leaves the marker, and the prefix is then refused
        self.prefix = self.data / "gitfurl" / "prefixes" / "p2"
        choice = choose_route(self.setup, "p2", detect(self.setup), "auto", self.env)
        plan = NativeInstallPlan(self.setup, "p2", self.prefix, choice, True, "/x/wine")
        status = 1
        with no_process():
            self.assertEqual(native_prefix_create(plan, False, self.env, run), 1)
        (self.prefix / "drive_c").mkdir()
        self.assertTrue((self.prefix / "gitfurl-incomplete").exists())
        self.assertIn("not completely created", choice.extract.problem())

    def test_ctrl_c_after_the_unpack_reports_the_result_not_nothing_written(self):
        # Review finding: a Ctrl-C while the listings were made was reported as "interrupted
        # before any file was written", with no result in the log.
        self.make_prefix()
        with no_process(), mock.patch("gitfurl.route.listings",
                                      side_effect=KeyboardInterrupt):
            code, out, err = run_cli("install", str(self.setup), "--prefix", "p1", "--yes",
                                     "--detailed-log", "--cleanup", "installer")
        self.assertEqual(code, 130)
        self.assertNotIn("before any file was written", err)
        self.assertIn("Files extracted", out)
        self.assertIn("interrupted after the unpack", out)
        self.assertIn("cleanup: skipped", out)
        self.assertTrue(self.setup.exists())
        self.assertTrue((self.dest() / "bin" / "Game.exe").is_file())
        log = next((self.state / "gitfurl" / "logs").glob("p1-*.log"))
        text = log.read_text()
        self.assertIn("--- interrupted after the unpack", text)
        self.assertIn("--- Extraction finished", text)
        events = [json.loads(ln) for ln in log.with_suffix(".events.jsonl").read_text()
                  .splitlines()]
        self.assertEqual(events[-1]["event"], "interrupted")
        self.assertEqual(events[-1]["stage"], "after_unpack")

    def test_ctrl_c_out_of_the_unpack_after_files_were_written_says_so(self):
        self.make_prefix()
        from gitfurl import extract as ex

        real = ex.extract

        def late(plan, cancel=None, on_progress=None):
            real(plan, cancel, on_progress)
            raise KeyboardInterrupt

        with no_process(), mock.patch.object(ex, "extract", side_effect=late):
            code, _, err = run_cli("install", str(self.setup), "--prefix", "p1", "--yes")
        self.assertEqual(code, 130)
        self.assertIn("interrupted during the unpack; files already written", err)
        self.assertNotIn("before any file was written", err)
        log = next((self.state / "gitfurl" / "logs").glob("p1-*.log"))
        self.assertIn("--- interrupted during the unpack", log.read_text())

    def test_ctrl_c_before_the_unpack_starts_says_nothing_was_written(self):
        self.make_prefix()
        from gitfurl import extract as ex

        with no_process(), mock.patch.object(ex, "extract", side_effect=KeyboardInterrupt):
            code, _, err = run_cli("install", str(self.setup), "--prefix", "p1", "--yes")
        self.assertEqual(code, 130)
        self.assertIn("interrupted before any file was written", err)
        self.assertFalse(self.dest().exists())

    def test_no_terminal_and_no_yes_is_not_approved(self):
        self.make_prefix()
        with no_process(), mock.patch("sys.stdin.isatty", return_value=False):
            code, out, _ = run_cli("install", str(self.setup), "--prefix", "p1")
        self.assertEqual(code, 2)
        self.assertIn("Not approved", out)
        self.assertFalse(self.dest().exists())

    def test_existing_destination_is_refused(self):
        self.make_prefix()
        self.dest().mkdir(parents=True)
        with no_process():
            code, _, err = run_cli("install", str(self.setup), "--prefix", "p1", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("the destination already exists", err)
        self.assertEqual(list(self.dest().iterdir()), [])

    def test_ctrl_c_during_the_unpack_exits_130_and_skips_cleanup(self):
        self.make_prefix()
        from gitfurl import extract as ex

        real = ex.extract

        def interrupting(plan, cancel=None, on_progress=None):
            def hook(done, total):
                raise KeyboardInterrupt
            return real(plan, cancel, hook)

        with no_process(), mock.patch.object(ex, "extract", side_effect=interrupting):
            code, out, _ = run_cli("install", str(self.setup), "--prefix", "p1", "--yes",
                                   "--cleanup", "installer")
        self.assertEqual(code, 130)
        self.assertIn("Extraction stopped", out)
        self.assertIn("cleanup: skipped", out)
        self.assertTrue(self.setup.exists())
        self.assertTrue(any(self.dest().rglob("*")))

    def test_crc_mismatch_exits_1_and_skips_cleanup(self):
        self.make_prefix()
        for p in self.src.glob("*.bin"):
            p.unlink()

        def mutate(d):
            # the last CRC-32 in the directory (readme.txt's) is changed
            good = zlib.crc32(TEXT).to_bytes(4, "little")
            i = d.rindex(good)
            return d[:i] + bytes([d[i] ^ 1]) + d[i + 1:]

        self.arc("NAME-01.bin", lzma_blocks(), dir_mutator=mutate)
        with no_process():
            code, out, _ = run_cli("install", str(self.setup), "--prefix", "p1", "--yes",
                                   "--cleanup", "installer")
        self.assertEqual(code, 1)
        self.assertIn("failed the CRC-32 check", out)
        self.assertIn("readme.txt", out)
        self.assertIn("cleanup: skipped", out)
        self.assertTrue(self.setup.exists())

    def test_cleanup_installer_after_a_complete_unpack_deletes_only_the_installer(self):
        self.make_prefix()
        with no_process():
            code, out, err = run_cli("install", str(self.setup), "--prefix", "p1", "--yes",
                                     "--cleanup", "installer")
        self.assertEqual(code, 0, err)
        self.assertIn("cleanup: deleted 1 file(s)", out)
        self.assertFalse(self.setup.exists())
        self.assertTrue((self.src / "NAME-01.bin").exists())

    def test_route_native_dry_run_refuses_when_not_possible(self):
        for p in self.src.glob("*.bin"):
            p.unlink()
        self.arc("NAME-01.bin", lzma_blocks(), bad_desc_crc=True)
        before = snapshot(self.base)
        code, _, err = run_cli("install", str(self.setup), "--prefix", "p1", "--dry-run",
                               "--route", "native")
        self.assertEqual(code, 1)
        self.assertIn("native route not possible", err)
        self.assertIn("Nothing was run.", err)
        self.assertEqual(before, snapshot(self.base))

    def test_wine_fallback_prints_the_reason_and_runs_setup(self):
        for p in self.src.glob("*.bin"):
            p.unlink()
        self.arc("NAME-01.bin", lzma_blocks(), bad_desc_crc=True)
        self.make_prefix()
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0))
        with mock.patch("shutil.which", return_value="/x/wine"), \
                mock.patch("subprocess.run", run), mock.patch("gitfurl.runner._run_in_group", run), \
                mock.patch("gitfurl.runner.RunControl.started", new_callable=mock.PropertyMock,
                           return_value=True):
            code, out, _ = run_cli("install", str(self.setup), "--prefix", "p1", "--yes",
                                   "--no-memory-limit")
        self.assertEqual(code, 0)
        self.assertIn("route: wine (chosen by gitfurl)", out)
        self.assertIn("cannot be unpacked by gitfurl", out)
        self.assertIn("Setup unpacks its own archives", out)
        self.assertEqual(run.call_args[0][0][:2], ["/x/wine", str(self.setup)])
        self.assertFalse(self.dest().exists())

    def test_route_wine_overrides_a_possible_native_route(self):
        code, out, _ = run_cli("install", str(self.setup), "--prefix", "p1", "--dry-run",
                               "--route", "wine", "--no-memory-limit")
        self.assertEqual(code, 0)
        self.assertIn("route: wine (chosen by you)", out)
        self.assertIn("command:     wine", out)


class UnpackedSizeTests(Base):
    """The Setup route's preflight: free space against what the archives unpack to."""

    def test_sizes_from_the_indexes_readable_or_not(self):
        from gitfurl.extract import unpacked_size

        self.assertIsNone(unpacked_size(self.setup))  # no archive beside it
        (self.src / "NAME-03.bin").write_bytes(b"ArC\x01" + b"\0" * 64)  # unreadable
        self.assertIsNone(unpacked_size(self.setup))  # none readable
        self.arc("NAME-01.bin", lzma_blocks())
        self.arc("NAME-02.bin", chain_blocks())
        two = unpacked_size(self.setup)
        want = len(GAME) + len(TEXT) + 2 * 256 * 64 + len(b"MZ redist")
        self.assertEqual((two.total, two.read, two.found, two.complete), (want, 2, 3, True))
        with mock.patch("gitfurl.extract.find_archives", side_effect=OSError(5, "EIO")):
            self.assertIsNone(unpacked_size(self.setup))  # a folder that cannot be listed
        with mock.patch("gitfurl.extract.find_archives",
                        return_value=([self.src / "NAME-01.bin"], ["stopped early"])):
            self.assertFalse(unpacked_size(self.setup).complete)

    def test_only_archives_that_belong_to_the_installer_count(self):
        from gitfurl.route import setup_unpacked

        self.arc("NAME-01.bin", lzma_blocks())
        self.assertIsNotNone(setup_unpacked(self.setup, detect(self.setup)))
        other = self.base / "Downloads"
        other.mkdir()
        nsis = other / "tool-setup.exe"
        nsis.write_bytes(b"MZ" + b"\0" * 4096)  # neither Inno Setup nor FreeArc
        (other / "unrelated-game.bin").write_bytes(make_archive(*lzma_blocks()))
        self.assertIsNone(setup_unpacked(nsis, detect(nsis)))
        inno = other / "setup.exe"
        inno.write_bytes(INNO)  # Inno Setup, but the archive's name does not link it
        self.assertIsNone(setup_unpacked(inno, detect(inno)))

    def test_preflight_lines_and_warning(self):
        from gitfurl.extract import Unpacked
        from gitfurl.preflight import Preflight

        pf = Preflight(10**9, Path("/x"), 10**6, True, 8 * 10**9, 4,
                       Unpacked(3 * 10**9, 1, 2))
        text = pf.describe()
        self.assertIn("the 1 FreeArc archive beside the installer unpack to 3.00 GB "
                      "(1 of 2 readable; at least)", text)
        pf.unpacked = Unpacked(3 * 10**9, 2, 2, complete=False)
        self.assertIn("(at least; the folder was not looked at in full)", pf.describe())
        pf.unpacked = Unpacked(3 * 10**9, 1, 2)
        self.assertIn("space the install needs: at least that much", text)
        self.assertIn("advisory: free space (1.00 GB) is smaller than what the archives", text)
        pf.free_bytes = 4 * 10**9
        self.assertEqual(pf.warnings(), [])
        pf.unpacked = None
        self.assertIn("space the install needs: unknown", pf.describe())

    def test_setup_route_dry_run_shows_the_unpacked_size(self):
        self.arc("NAME-01.bin", lzma_blocks())
        with no_process():
            code, out, _ = run_cli("install", str(self.setup), "--prefix", "p1", "--dry-run",
                                   "--route", "wine", "--no-memory-limit")
        self.assertEqual(code, 0)
        self.assertIn("read from the archives' own indexes: the 1 FreeArc archive", out)
        self.assertIn("space the install needs: at least that much", out)


class GuiRouteLineTests(Base):
    def test_window_takes_the_automatic_route_or_setup_when_asked(self):
        try:
            from gitfurl.gui import SETUP_INSTEAD_TITLE, window_route_text
        except ImportError as exc:  # no tkinter
            self.skipTest(str(exc))
        self.arc("NAME-01.bin", lzma_blocks())
        text = window_route_text(self.setup, "p1", self.env)
        self.assertIn("route: native (chosen by gitfurl)", text)
        text = window_route_text(self.setup, "p1", self.env, setup_instead=True)
        self.assertIn("route: wine (chosen by you)", text)
        text = window_route_text(self.setup, "p1", self.env,
                                 setup_options=["portable install"])
        self.assertIn("route: wine (chosen by gitfurl)", text)
        self.assertIn("(portable install)", text)
        self.assertIn("turn those options off", text)
        self.assertNotIn("--route", text)
        self.assertTrue(SETUP_INSTEAD_TITLE)
        for p in self.src.glob("*.bin"):
            p.unlink()
        text = window_route_text(self.setup, "p1", self.env)
        self.assertIn("route: wine (chosen by gitfurl)", text)


if __name__ == "__main__":
    unittest.main()
