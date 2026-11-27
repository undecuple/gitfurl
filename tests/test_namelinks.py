"""Linking the archives under the names the setup expects (--link-names, the window's switch).
Synthetic files only: Inno Setup installers from tests/inno_writer.py and FreeArc archives
from tests/farc_writer.py. The launcher is mocked; Wine and real installers never run."""

import contextlib
import io
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

try:
    import inno_writer as w
    from farc_writer import F, make_archive
except ImportError:  # python -m unittest tests.test_namelinks
    from tests import inno_writer as w
    from tests.farc_writer import F, make_archive

from gitfurl import cli, gui
from gitfurl.detect import ROUTE_NATIVE, ROUTE_WINE, detect
from gitfurl.namelinks import (
    LinkProposal,
    StagingError,
    plan_staging,
    propose,
    propose_for,
    run_staged,
    staging_root,
)
from gitfurl.route import NativeInstallPlan, choose_route, setup_unpacked

NOW = datetime(2026, 11, 27, 12, 0, 0)


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def no_process():
    boom = AssertionError("no process may start")
    with mock.patch("subprocess.Popen", side_effect=boom), \
            mock.patch("subprocess.run", side_effect=boom), \
            mock.patch("os.system", side_effect=boom):
        yield


@contextlib.contextmanager
def patched_runs(side_effect=None):
    run = mock.Mock(return_value=subprocess.CompletedProcess([], 0), side_effect=side_effect)
    with mock.patch("subprocess.run", run), mock.patch("gitfurl.prefixes.run_in_group", run), \
            mock.patch("gitfurl.runner._run_in_group", run), \
            mock.patch("gitfurl.runner.RunControl.started", new_callable=mock.PropertyMock,
                       return_value=True):
        yield run


def tree(root: Path):
    """Every entry under root with its lstat facts (links not followed)."""
    out = []
    for dirpath, dirs, files in os.walk(root):
        for n in sorted(dirs + files):
            p = Path(dirpath, n)
            st = p.lstat()
            out.append((str(p), st.st_mode, st.st_size, st.st_mtime_ns, st.st_ino))
    return sorted(out)


def archive(name: str) -> bytes:
    return make_archive([("storing", [F(name, b"MZ" + name.encode() * 50)])], ("",))


class ProposeTests(unittest.TestCase):
    def test_one_series_each_side_is_mapped_number_to_number(self):
        got = propose(["fg-02.bin", "fg-01.bin"], ["other-01.bin", "other-02.bin"])
        self.assertEqual(got.links, (("fg-01.bin", "other-01.bin"), ("fg-02.bin", "other-02.bin")))
        self.assertTrue(got.ok)
        self.assertEqual(got.stands_for(),
                         "other-01.bin stands for fg-01.bin, other-02.bin for fg-02.bin")
        self.assertEqual(got.mapped_archives(), {"other-01.bin", "other-02.bin"})

    def test_names_without_a_counterpart_stay_missing(self):
        got = propose(["fg-01.bin", "fg.inf", "fg-02.bin"], ["a1.bin", "a2.bin"])
        self.assertEqual([t for _n, t in got.links], ["a1.bin", "a2.bin"])
        self.assertEqual(got.unmatched, ("fg.inf",))
        self.assertIn("still missing, no counterpart in the folder (nothing is made up for it): "
                      "fg.inf", "\n".join(got.lines()))

    def test_numbers_compare_as_numbers_and_case_is_ignored(self):
        got = propose(["FG-1.BIN", "fg-2.bin"], ["Other-001.bin", "other-002.bin"])
        self.assertEqual(got.links, (("FG-1.BIN", "Other-001.bin"), ("fg-2.bin", "other-002.bin")))

    def test_archives_the_setup_names_are_not_candidates(self):
        got = propose(["fg-01.bin"], ["named-01.bin", "other-01.bin"], named={"named-01.bin"})
        self.assertEqual(got.links, (("fg-01.bin", "other-01.bin"),))

    def refused(self, *args, **kw) -> LinkProposal:
        got = propose(*args, **kw)
        self.assertFalse(got.ok)
        self.assertEqual(got.links, ())
        self.assertTrue(got.reason)
        return got

    def test_refusals(self):
        cases = [
            (([], ["a-1.bin"]), "names no missing file"),
            ((["fg.inf"], ["a-1.bin"]), "no missing name is numbered (fg.inf)"),
            ((["fg-01.bin", "xy-01.cab"], ["a-1.bin"]), "more than one numbered series"),
            ((["fg-01.bin", "fg-03.bin"], ["a-1.bin", "a-3.bin"]), "not numbered 1 to 2"),
            ((["fg-02.bin"], ["a-2.bin"]), "not numbered 1 to 1"),
            ((["fg-1.bin", "fg-01.bin"], ["a-1.bin"]), "a number repeats"),
            ((["fg-1.bin"], []), "no FreeArc archive the setup does not already name"),
            ((["fg-1.bin"], ["a-1.bin", "loose.arc"]), "not numbered (loose.arc)"),
            ((["fg-1.bin"], ["a-1.bin", "b-1.bin"]), "more than one numbered series of FreeArc"),
            ((["fg-1.bin", "fg-2.bin"], ["a-1.bin"]), "expects 2 numbered archives"),
            ((["fg-1.bin"], ["a-1.bin", "a-2.bin"]), "the folder's series is 2"),
            ((["fg-1.bin"], ["a-1.bin", "a-01.bin"]), "a number repeats among the FreeArc"),
        ]
        for args, words in cases:
            with self.subTest(args=args):
                self.assertIn(words, self.refused(*args).reason)

    def test_a_name_that_exists_is_never_mapped(self):
        # The setup "expects" fg-01.bin, and the folder has it (in another letter case):
        # it is not missing, so nothing is proposed for it.
        got = self.refused(["fg-01.bin"], ["a-01.bin"], present=["FG-01.BIN", "a-01.bin"])
        self.assertIn("names no missing file", got.reason)
        got = propose(["fg-01.bin", "fg-02.bin"], ["a-01.bin"], present=["fg-02.bin"])
        self.assertEqual(got.links, (("fg-01.bin", "a-01.bin"),))

    def test_names_in_subfolders_are_left_out(self):
        self.assertIn("names no missing file",
                      self.refused(["data\\fg-01.bin"], ["a-01.bin"]).reason)

    def test_no_mapping_lines(self):
        got = propose(["fg-1.bin", "x.inf"], [])
        self.assertEqual(got.unmatched, ("fg-1.bin", "x.inf"))
        self.assertTrue(got.lines()[0].startswith("link names: no mapping ("))


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.src = self.base / "dl"
        self.src.mkdir()
        self.data = self.base / "data"
        self.state = self.base / "state"
        self.env = {"XDG_DATA_HOME": str(self.data), "XDG_STATE_HOME": str(self.state),
                    "HOME": str(self.base)}
        patcher = mock.patch.dict("os.environ", self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.prefix = self.data / "gitfurl" / "prefixes" / "p1"

    def make(self, code=("{src}\\fg-01.bin", "{src}\\fg-02.bin", "{src}\\fg.inf"),
             archives=("other-01.bin", "other-02.bin")):
        data, _ = w.build(w.Spec(code=w.code_blob(list(code))))
        self.setup = self.src / "setup.exe"
        self.setup.write_bytes(data)
        for k, name in enumerate(archives):
            (self.src / name).write_bytes(archive(f"part{k}.dat"))
        (self.src / "readme.txt").write_text("hello\n")
        return self.setup

    def make_prefix(self):
        (self.prefix / "drive_c").mkdir(parents=True)


class ProposeForTests(Base):
    def test_reads_the_script_and_the_folder(self):
        got = propose_for(self.make())
        self.assertEqual(got.links, (("fg-01.bin", "other-01.bin"), ("fg-02.bin", "other-02.bin")))
        self.assertEqual(got.unmatched, ("fg.inf",))

    def test_files_without_the_freearc_signature_are_not_candidates(self):
        inst = self.make(archives=())
        (self.src / "other-01.bin").write_bytes(b"not an archive")
        (self.src / "other-02.bin").write_bytes(b"not an archive")
        got = propose_for(inst)
        self.assertFalse(got.ok)
        self.assertIn("no FreeArc archive", got.reason)

    def test_an_unreadable_setup_gives_a_reason(self):
        (self.src / "setup.exe").write_bytes(b"MZ" + b"\0" * 300)
        got = propose_for(self.src / "setup.exe")
        self.assertFalse(got.ok)
        self.assertIn("could not be read", got.reason)


class RouteTests(Base):
    def test_without_the_switch_the_archives_are_not_linked(self):
        inst = self.make()
        choice = choose_route(inst, "p1", detect(inst), "auto", self.env)
        self.assertEqual(choice.route, ROUTE_WINE)
        self.assertIn("not linked", choice.line())

    def test_with_a_mapping_the_route_becomes_native(self):
        inst = self.make()
        links = propose_for(inst)
        choice = choose_route(inst, "p1", detect(inst), "auto", self.env, links=links)
        self.assertEqual((choice.route, choice.chosen_by), (ROUTE_NATIVE, "gitfurl"),
                         choice.line())
        self.assertIn("the setup script names every archive, with the names linked (other-01.bin "
                      "stands for fg-01.bin, other-02.bin for fg-02.bin)", choice.line())
        self.assertIs(choice.links, links)
        plan = NativeInstallPlan(inst, "p1", self.prefix, choice, False)
        text = "\n".join(plan.describe())
        self.assertIn("names: other-01.bin stands for fg-01.bin, other-02.bin for fg-02.bin; the "
                      "native route reads these archives directly (no links are made)", text)
        self.assertIn("still missing, no counterpart in the folder (not needed for the unpack): "
                      "fg.inf", text)
        # The native plan reads the original archives.
        self.assertEqual(sorted(ap.path.name for ap in choice.extract.archives),
                         ["other-01.bin", "other-02.bin"])
        self.assertIsNotNone(setup_unpacked(inst, detect(inst), links))
        self.assertIsNone(setup_unpacked(inst, detect(inst)))

    def test_no_mapping_leaves_the_route_as_it_was(self):
        inst = self.make(archives=("other-01.bin",))
        links = propose_for(inst)
        self.assertFalse(links.ok)
        choice = choose_route(inst, "p1", detect(inst), "auto", self.env, links=links)
        self.assertEqual(choice.route, ROUTE_WINE)


class StagingTests(Base):
    def staging(self):
        inst = self.make()
        return plan_staging(inst, propose_for(inst), "p1", now=NOW)

    def test_plan_lists_every_link_and_writes_nothing(self):
        inst = self.make()
        before = tree(self.base)
        st = plan_staging(inst, propose_for(inst), "p1", now=NOW)
        self.assertEqual(tree(self.base), before)
        self.assertEqual(st.folder.parent, staging_root(self.env))
        self.assertEqual(st.folder.name, f"p1-20261127-120000-{os.getpid()}")
        self.assertEqual([n for n, _t in st.links],
                         ["setup.exe", "other-01.bin", "other-02.bin", "readme.txt",
                          "fg-01.bin", "fg-02.bin"])
        text = "\n".join(st.describe())
        for name, target in st.links:
            self.assertIn(f"  {name} -> {target}", text)
        self.assertIn(f"fg-01.bin -> {self.src / 'other-01.bin'}   (a name the setup expects)",
                      text)
        self.assertIn("still missing, no counterpart", text)

    def test_folder_holds_links_only_with_the_right_targets_and_is_removed(self):
        st = self.staging()
        before = tree(self.src)
        st.create()
        self.assertEqual(stat.S_IMODE(st.folder.stat().st_mode), 0o700)
        entries = sorted(os.listdir(st.folder))
        self.assertEqual(entries, sorted(n for n, _t in st.links))
        for name, target in st.links:
            p = st.folder / name
            self.assertTrue(p.is_symlink(), name)
            self.assertEqual(os.readlink(p), str(target))
        self.assertEqual((st.folder / "fg-02.bin").read_bytes(),
                         (self.src / "other-02.bin").read_bytes())
        lines = st.remove()
        self.assertFalse(st.folder.exists())
        self.assertIn("its 6 links were removed", lines[0])
        self.assertEqual(tree(self.src), before)
        self.assertEqual(st.remove(), [])  # a second remove does nothing

    def test_an_existing_folder_is_not_reused(self):
        st = self.staging()
        st.folder.mkdir(parents=True)
        (st.folder / "keep").write_text("x")
        with self.assertRaises(StagingError):
            st.create()
        self.assertTrue((st.folder / "keep").exists())
        self.assertEqual(st.remove(), [])  # nothing of it was made by this run

    def test_only_its_own_links_are_removed(self):
        st = self.staging()
        st.create()
        (st.folder / "setup.log").write_text("written by Setup\n")  # not gitfurl's
        os.unlink(st.folder / "readme.txt")
        os.symlink("/elsewhere", st.folder / "readme.txt")  # changed: not the link it made
        lines = st.remove()
        self.assertEqual(sorted(os.listdir(st.folder)), ["readme.txt", "setup.log"])
        self.assertIn("kept", lines[0])
        self.assertIn("readme.txt", lines[0])
        self.assertTrue((self.src / "readme.txt").is_file())

    def test_run_staged_removes_after_success_failure_and_ctrl_c(self):
        inst = self.make()
        before = tree(self.src)
        for outcome in ("ok", "error", "interrupt", "stopped"):
            with self.subTest(outcome=outcome):
                st = plan_staging(inst, propose_for(inst), "p1", now=NOW)
                seen, lines = {}, []

                def fn():
                    seen["links"] = sorted(os.listdir(st.folder))
                    if outcome == "error":
                        raise RuntimeError("failed")
                    if outcome == "interrupt":
                        raise KeyboardInterrupt
                    return 130 if outcome == "stopped" else 0

                if outcome in ("error", "interrupt"):
                    with self.assertRaises((RuntimeError, KeyboardInterrupt)):
                        run_staged(st, fn, lines.append)
                else:
                    run_staged(st, fn, lines.append)
                self.assertEqual(len(seen["links"]), 6)
                self.assertFalse(st.folder.exists())
                self.assertIn("were removed", lines[0])
                self.assertEqual(tree(self.src), before)

    def test_a_name_in_the_folder_is_never_shadowed(self):
        inst = self.make()
        links = LinkProposal((("readme.txt", "other-01.bin"),), "test")
        with self.assertRaises(ValueError):
            plan_staging(inst, links, "p1")


class CliTests(Base):
    def test_dry_run_with_setup_route_shows_the_staging_and_writes_nothing(self):
        inst = self.make()
        before = tree(self.base)
        with no_process():
            code, out, err = run_cli("install", str(inst), "--prefix", "p1", "--dry-run",
                                     "--route", "wine", "--link-names", "--no-memory-limit")
        self.assertEqual(code, 0, err)
        self.assertEqual(tree(self.base), before)
        root = staging_root(self.env)
        self.assertRegex(out, rf"command: +wine {root}/p1-\d{{8}}-\d{{6}}-\d+/setup.exe")
        self.assertRegex(out, rf"working dir: {root}/p1-")
        self.assertIn("link names: the setup expects fg-01.bin to fg-02.bin (2 missing)", out)
        self.assertIn(f"fg-01.bin -> {self.src / 'other-01.bin'}", out)
        self.assertIn(f"readme.txt -> {self.src / 'readme.txt'}", out)
        self.assertIn("6 symbolic links in it", out)
        self.assertIn("fg.inf: missing", out)  # the detection still lists it

    def test_dry_run_auto_goes_native_with_the_names(self):
        inst = self.make()
        with no_process():
            code, out, _ = run_cli("install", str(inst), "--prefix", "p1", "--dry-run",
                                   "--link-names", "--no-memory-limit")
        self.assertEqual(code, 0)
        self.assertIn("route: native (chosen by gitfurl)", out)
        self.assertIn("names: other-01.bin stands for fg-01.bin", out)
        self.assertIn("no staging folder is needed on the native route", out)
        self.assertNotIn("staging folder:", out)

    def test_dry_run_without_the_option_is_unchanged(self):
        inst = self.make()
        with no_process():
            code, out, _ = run_cli("install", str(inst), "--prefix", "p1", "--dry-run",
                                   "--no-memory-limit")
        self.assertEqual(code, 0)
        self.assertIn("route: wine", out)
        self.assertNotIn("link names", out)
        self.assertIn(f"command:     wine {inst}", out)

    def test_dry_run_without_a_mapping_says_why(self):
        inst = self.make(archives=("other-01.bin",))
        with no_process():
            code, out, _ = run_cli("install", str(inst), "--prefix", "p1", "--dry-run",
                                   "--link-names", "--no-memory-limit")
        self.assertEqual(code, 0)
        self.assertIn("link names: no mapping (the setup expects 2 numbered archives", out)
        self.assertIn("Setup runs from the installer's own folder", out)
        self.assertIn(f"command:     wine {inst}", out)

    def test_portable_is_refused_with_a_mapping(self):
        inst = self.make()
        code, _, err = run_cli("install", str(inst), "--prefix", "p1", "--dry-run", "--route",
                               "wine", "--link-names", "--portable", "--no-memory-limit")
        self.assertEqual(code, 1)
        self.assertIn("cannot be combined with --portable", err)

    def real_run(self, fake):
        inst = self.make()
        self.make_prefix()
        before = tree(self.src)
        with mock.patch("shutil.which", return_value="/x/wine"), patched_runs(fake) as run:
            code, out, err = run_cli("install", str(inst), "--prefix", "p1", "--yes",
                                     "--route", "wine", "--link-names", "--no-memory-limit")
        self.assertEqual(tree(self.src), before)  # the user's files are untouched
        root = staging_root(self.env)
        self.assertEqual(list(root.iterdir()), [])  # the staging folder is gone
        return code, out, err, run

    def test_real_run_starts_setup_from_the_staging_folder_and_removes_it(self):
        seen = {}

        def fake(argv, **kw):
            seen["argv"], seen["cwd"] = list(argv), Path(kw["cwd"])
            seen["links"] = {n: os.readlink(Path(kw["cwd"], n)) for n in os.listdir(kw["cwd"])}
            return subprocess.CompletedProcess(argv, 0)

        code, out, err, _ = self.real_run(fake)
        self.assertEqual(code, 0, err)
        self.assertEqual(seen["argv"][1], str(seen["cwd"] / "setup.exe"))
        self.assertEqual(seen["cwd"].parent, staging_root(self.env))
        self.assertEqual(seen["links"]["fg-01.bin"], str(self.src / "other-01.bin"))
        self.assertEqual(seen["links"]["fg-02.bin"], str(self.src / "other-02.bin"))
        self.assertEqual(seen["links"]["setup.exe"], str(self.src / "setup.exe"))
        self.assertEqual(len(seen["links"]), 6)
        self.assertIn("6 symbolic links in it", out)  # listed before the approval
        self.assertIn("its 6 links were removed", out)
        self.assertIn("installer exited with status 0", out)

    def test_failed_run_removes_the_staging_folder(self):
        code, out, _, _ = self.real_run(lambda argv, **kw: subprocess.CompletedProcess(argv, 2))
        self.assertEqual(code, 1)
        self.assertIn("its 6 links were removed", out)

    def test_stopped_run_removes_the_staging_folder(self):
        def fake(argv, **kw):
            kw["control"].cancelled = True  # as a Stop does
            return subprocess.CompletedProcess(argv, 130)

        code, out, _, _ = self.real_run(fake)
        self.assertEqual(code, 130)
        self.assertIn("cancelled", out)
        self.assertIn("its 6 links were removed", out)

    def test_ctrl_c_removes_the_staging_folder(self):
        def fake(argv, **kw):
            raise KeyboardInterrupt

        code, out, _, _ = self.real_run(fake)
        self.assertEqual(code, 130)
        self.assertIn("its 6 links were removed", out)


class WindowTests(Base):
    def test_switch_title_and_note(self):
        self.assertEqual(gui.LINK_NAMES_TITLE,
                         "Link the archives under the names the setup expects")
        self.assertTrue(gui.LINK_NAMES_NOTE.startswith("Off by default."))

    def test_plan_text_with_the_switch(self):
        inst = self.make()
        text, err = gui.describe_plan(str(inst), "p1", now=NOW, link_names=True)
        self.assertIsNone(err)
        folder = staging_root(self.env) / f"p1-20261127-120000-{os.getpid()}"
        self.assertIn(f"command:     wine {folder / 'setup.exe'}", text)
        self.assertIn(f"working dir: {folder}", text)
        self.assertIn(f"fg-02.bin -> {self.src / 'other-02.bin'}", text)
        self.assertIn("still missing, no counterpart", text)
        self.assertFalse(folder.exists())
        off, _ = gui.describe_plan(str(inst), "p1", now=NOW)
        self.assertNotIn("staging", off)

    def test_plan_text_with_portable_is_an_error(self):
        inst = self.make()
        with mock.patch("gitfurl.portable.find_bwrap", return_value="/x/bwrap"), \
                mock.patch("gitfurl.portable.check_portable_supported"), \
                mock.patch("gitfurl.portable.describe_portable", return_value=[]):
            text, err = gui.describe_plan(str(inst), "p1", portable=True, now=NOW,
                                          link_names=True)
        self.assertEqual(text, "")
        self.assertIn("cannot be combined with the portable install", err)

    def test_route_with_the_switch(self):
        inst = self.make()
        _det, choice, _err = gui.window_route(inst, "p1", env=self.env, link_names=True)
        self.assertEqual(choice.route, ROUTE_NATIVE)
        _det, choice, _err = gui.window_route(inst, "p1", env=self.env)
        self.assertEqual(choice.route, ROUTE_WINE)
        text = gui.window_route_text(inst, "p1", self.env, link_names=True)
        self.assertIn("with the names linked (other-01.bin stands for fg-01.bin", text)


if __name__ == "__main__":
    unittest.main()
