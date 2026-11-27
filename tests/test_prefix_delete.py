"""Deleting a prefix gitfurl made (gitfurl.prefix_delete, `gitfurl cli prefix delete`).

Synthetic only: every prefix, menu entry and link target is made in a temporary folder that
HOME and the XDG folders point at. No Wine runs; the Wine-server and in-use checks are
injected (or read a fake /proc) except in the command-line tests, which use the real
read-only checks on a temporary prefix no process knows about.
"""

import io
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from gitfurl import cli, gui, prefix_delete
from gitfurl.desktop import MARKER
from gitfurl.prefix_delete import (
    DeleteRefused,
    in_use,
    perform_delete_prefix,
    plan_delete_prefix,
    window_text,
)
from gitfurl.setup_entries import shortcut_text

NONE = {"running": lambda _p: None, "using": lambda _p: None}


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    data_name = "data"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(os.path.realpath(tmp.name))  # no link on the way
        self.data = self.base / self.data_name
        self.state = self.base / "state"
        self.env = {"HOME": str(self.base), "XDG_DATA_HOME": str(self.data),
                    "XDG_STATE_HOME": str(self.state), "PATH": os.environ.get("PATH", "")}
        patcher = mock.patch.dict("os.environ", self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = self.data / "gitfurl" / "prefixes"
        self.apps = self.data / "applications"
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.outside / "keep.txt").write_text("keep me")

    def make_prefix(self, name="game"):
        p = self.root / name
        (p / "drive_c" / "windows" / "system32").mkdir(parents=True)
        (p / "drive_c" / "windows" / "system32" / "a.dll").write_bytes(b"x" * 100)
        (p / "drive_c" / "Games" / "g").mkdir(parents=True)
        (p / "drive_c" / "Games" / "g" / "Game.exe").write_bytes(b"y" * 50)
        (p / "system.reg").write_text("WINE REGISTRY Version 2\n")
        (p / "user.reg").write_text("WINE REGISTRY Version 2\n")
        (p / "dosdevices").mkdir()
        os.symlink(self.outside, p / "dosdevices" / "z:")  # like z: -> /, but a temp folder
        os.symlink(self.outside / "keep.txt", p / "drive_c" / "link.txt")
        return p

    def entry(self, prefix_name, slug, *, marker=True, prefix_path=None):
        self.apps.mkdir(parents=True, exist_ok=True)
        text = shortcut_text("Game", prefix_path or self.root / prefix_name, prefix_name,
                             "C:\\Games\\g\\Game.exe", [], None)
        if not marker:
            text = text.replace(MARKER + "\n", "")
        path = self.apps / f"gitfurl-{prefix_name}-{slug}.desktop"
        path.write_text(text)
        return path


class RefusalTests(Base):
    def refused(self, name="game", **kw):
        with self.assertRaises(DeleteRefused) as cm:
            plan_delete_prefix(name, **{**NONE, **kw})
        return str(cm.exception)

    def test_bad_names(self):
        for name in ("", "../game", "-game", "a/b", "x" * 65, "game two"):
            with self.subTest(name=name):
                self.assertIn("invalid prefix name", self.refused(name))

    def test_missing(self):
        self.assertIn("there is no prefix game", self.refused())

    def test_not_a_folder(self):
        self.root.mkdir(parents=True)
        (self.root / "game").write_text("a file")
        self.assertIn("is not a folder", self.refused())

    def test_not_a_wine_prefix(self):
        (self.root / "game" / "stuff").mkdir(parents=True)
        self.assertIn("does not look like a Wine prefix", self.refused())
        os.symlink(self.outside, self.root / "game" / "drive_c")  # a link is not drive_c
        self.assertIn("does not look like a Wine prefix", self.refused())
        self.assertTrue((self.outside / "keep.txt").exists())

    def test_prefix_is_a_link(self):
        real = self.make_prefix("real")
        os.symlink(real, self.root / "game")
        self.assertIn(f"{self.root / 'game'} is a link", self.refused())

    def test_link_higher_up(self):
        elsewhere = self.base / "elsewhere"
        self.make_prefix()
        os.rename(self.data / "gitfurl", elsewhere)
        os.symlink(elsewhere, self.data / "gitfurl")
        self.assertIn(f"{self.data / 'gitfurl'} is a link", self.refused())

    def test_not_an_absolute_root(self):
        os.environ["XDG_DATA_HOME"] = "relative/data"
        self.assertIn("is not a plain absolute path", self.refused())

    def test_wine_server_running(self):
        self.make_prefix()
        why = self.refused(running=lambda p: "process 42 works in this prefix's Wine server "
                                             "folder")
        self.assertIn("a Wine server for this prefix is running (process 42", why)

    def test_run_in_progress(self):
        self.make_prefix()
        self.assertIn("a run on this prefix seems to be in progress (process 7",
                      self.refused(using=lambda p: "process 7 holds a file inside this "
                                                   "prefix open"))
        self.assertEqual(self.refused(busy="a run in this window is in progress"),
                         "a run in this window is in progress")

    def test_default_server_check_is_used(self):
        self.make_prefix()
        with mock.patch("gitfurl.procgroup.prefix_server_running",
                        return_value="the lock file is held") as m:
            why = self.refused(running=None)
        m.assert_called_once_with(self.root / "game")
        self.assertIn("the lock file is held", why)


class InUseTests(Base):
    def test_fake_proc(self):
        prefix = self.make_prefix()
        proc = self.base / "proc"
        (proc / "100").mkdir(parents=True)
        (proc / "100" / "environ").write_bytes(b"A=1\0WINEPREFIX=" + str(prefix).encode()
                                               + b"-two\0")
        self.assertIsNone(in_use(prefix, str(proc), skip_pid=1))  # game-two is not game
        (proc / "101" / "fd").mkdir(parents=True)
        os.symlink(prefix / "drive_c" / "x.bin", proc / "101" / "fd" / "3")
        self.assertIn("process 101 holds a file", in_use(prefix, str(proc), skip_pid=1))
        self.assertIsNone(in_use(prefix, str(proc), skip_pid=101))
        (proc / "102").mkdir()
        os.symlink(prefix / "drive_c", proc / "102" / "cwd")
        self.assertIn("process 102 works in a folder", in_use(prefix, str(proc), skip_pid=101))
        proc2 = self.base / "proc2"
        (proc2 / "99").mkdir(parents=True)
        (proc2 / "99" / "environ").write_bytes(b"WINEPREFIX=" + str(prefix).encode() + b"\0")
        self.assertIn("process 99 runs with WINEPREFIX", in_use(prefix, str(proc2), skip_pid=1))


class DeleteTests(Base):
    def test_plan_counts_without_following_links(self):
        self.make_prefix()
        plan = plan_delete_prefix("game", **NONE)
        self.assertEqual((plan.files, plan.links, plan.size), (4, 2, 150 + 2 * 24))
        self.assertEqual(plan.dirs, 6)  # drive_c, windows, system32, Games, g, dosdevices
        text = "\n".join(plan.describe())
        self.assertIn("This cannot be undone", text)
        self.assertIn("2 link(s) are removed as links", text)

    def test_delete_keeps_what_links_point_to(self):
        prefix = self.make_prefix()
        other = self.make_prefix("game-two")
        plan = plan_delete_prefix("game", **NONE)
        res = perform_delete_prefix(plan, **NONE)
        self.assertTrue(res.folder_gone, res.left)
        self.assertEqual(res.left, [])
        self.assertFalse(os.path.lexists(prefix))
        self.assertEqual((self.outside / "keep.txt").read_text(), "keep me")
        self.assertTrue((other / "system.reg").is_file())
        self.assertEqual((res.removed_files, res.removed_dirs), (6, 6))
        log = res.log_path
        self.assertEqual(log.parent, self.state / "gitfurl" / "logs")
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertIn("deleted the prefix folder", log.read_text())
        self.assertEqual(stat.S_IMODE(res.timeline_path.stat().st_mode), 0o600)
        tl = res.timeline_path.read_text()
        self.assertIn("Prefix folder deleted", tl)
        self.assertIn("Deleted the prefix game.", tl)

    def test_prefix_swapped_for_a_link_is_refused(self):
        prefix = self.make_prefix()
        plan = plan_delete_prefix("game", **NONE)
        moved = self.base / "moved"
        os.rename(prefix, moved)
        os.symlink(moved, prefix)
        with self.assertRaises(DeleteRefused) as cm:
            perform_delete_prefix(plan, **NONE)
        self.assertIn("is a link", str(cm.exception))
        self.assertTrue((moved / "system.reg").is_file())
        self.assertFalse((self.state / "gitfurl" / "logs").exists())

    def test_prefix_replaced_by_another_folder_is_refused(self):
        prefix = self.make_prefix()
        plan = plan_delete_prefix("game", **NONE)
        os.rename(prefix, self.base / "moved")
        self.make_prefix()
        with self.assertRaises(DeleteRefused) as cm:
            perform_delete_prefix(plan, **NONE)
        self.assertIn("not the folder the plan was made for", str(cm.exception))
        self.assertTrue((prefix / "system.reg").is_file())

    def test_server_started_after_the_plan_is_refused(self):
        prefix = self.make_prefix()
        plan = plan_delete_prefix("game", **NONE)
        with self.assertRaises(DeleteRefused):
            perform_delete_prefix(plan, running=lambda _p: "process 9", using=NONE["using"])
        self.assertTrue((prefix / "system.reg").is_file())

    def test_inner_folder_swapped_for_a_link_mid_way(self):
        """A folder that becomes a link between its lstat and its open: O_NOFOLLOW refuses
        it, the deletion stops with a report and the link's target is untouched."""
        prefix = self.make_prefix()
        plan = plan_delete_prefix("game", **NONE)
        real_stat = os.stat
        swapped = {}

        def stat_then_swap(name, *a, **kw):
            st = real_stat(name, *a, **kw)
            if name == "windows" and kw.get("dir_fd") is not None and not swapped:
                os.rename(prefix / "drive_c" / "windows", self.base / "windows-moved")
                os.symlink(self.outside, prefix / "drive_c" / "windows")
                swapped["yes"] = True
            return st

        with mock.patch.object(prefix_delete.os, "stat", side_effect=stat_then_swap):
            res = perform_delete_prefix(plan, **NONE)
        self.assertTrue(swapped)
        self.assertFalse(res.folder_gone)
        self.assertTrue(any("could not be opened as a folder without following links" in w
                            for w in res.left), res.left)
        self.assertEqual((self.outside / "keep.txt").read_text(), "keep me")
        self.assertTrue(os.path.lexists(prefix))
        self.assertIn("not fully deleted", res.headline(plan))

    def test_other_file_system_is_not_entered(self):
        d = self.base / "t"
        (d / "sub").mkdir(parents=True)
        (d / "sub" / "f").write_text("x")
        fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaises(prefix_delete._Stop) as cm:
                prefix_delete._empty(fd, d, -1, 0, prefix_delete.DeleteResult())
        finally:
            os.close(fd)
        self.assertIn("on another file system", str(cm.exception))
        self.assertTrue((d / "sub" / "f").exists())


class MountTests(Base):
    """A bind mount of the same file system has the same device number, so the walk cannot
    see it; the mount table must."""

    def table(self, *points):
        lines = [f"{20 + i} 1 0:{30 + i} / {pt} rw,relatime shared:1 - ext4 /dev/x rw"
                 for i, pt in enumerate(("/",) + points)]
        t = self.base / "mountinfo"
        t.write_text("\n".join(lines) + "\n")
        return t

    def test_bind_mount_inside_is_refused_at_plan(self):
        p = self.make_prefix()
        t = self.table(str(p / "drive_c" / "Games").replace(" ", "\\040"))
        with self.assertRaises(DeleteRefused) as cm:
            plan_delete_prefix("game", **NONE, mountinfo=t)
        self.assertIn("mounted inside the prefix", str(cm.exception))

    def test_mount_appearing_after_the_plan_is_refused_and_nothing_deleted(self):
        p = self.make_prefix()
        plan = plan_delete_prefix("game", **NONE, mountinfo=self.table())
        t = self.table(str(p / "drive_c"))
        with self.assertRaises(DeleteRefused):
            perform_delete_prefix(plan, **NONE, mountinfo=t)
        self.assertTrue((p / "system.reg").is_file())

    def test_escaped_space_in_mount_point(self):
        from gitfurl.prefix_delete import mounts_inside

        p = self.base / "a b"
        t = self.table(str(p / "in").replace(" ", "\\040"))
        self.assertEqual(mounts_inside(p, t), [str(p / "in")])

    def test_similar_name_and_the_prefix_itself_are_not_inside(self):
        from gitfurl.prefix_delete import mounts_inside

        p = self.make_prefix()
        t = self.table(str(p), str(self.root / "game-two" / "x"))
        self.assertEqual(mounts_inside(p, t), [])
        plan_delete_prefix("game", **NONE, mountinfo=t)

    def test_unreadable_table_is_refused(self):
        self.make_prefix()
        with self.assertRaises(DeleteRefused) as cm:
            plan_delete_prefix("game", **NONE, mountinfo=self.base / "missing")
        self.assertIn("mount table", str(cm.exception))


class MenuEntryTests(Base):
    def setUp(self):
        super().setUp()
        self.make_prefix()
        self.make_prefix("game-two")
        self.ours = self.entry("game", "game")
        self.ours2 = self.entry("game", "editor")
        self.two = self.entry("game-two", "game")  # gitfurl-game-two-game.desktop
        self.unmarked = self.entry("game", "unmarked", marker=False)
        target = self.base / "marked-elsewhere.desktop"
        target.write_text(self.ours.read_text())
        self.linked = self.apps / "gitfurl-game-linked.desktop"
        os.symlink(target, self.linked)
        self.own = self.apps / "gitfurl.desktop"
        self.own.write_text(f"[Desktop Entry]\nExec=gitfurl\n{MARKER}\n")
        self.other = self.entry("game", "moved", prefix_path=self.base / "somewhere" / "game")

    def test_matching(self):
        plan = plan_delete_prefix("game", **NONE)
        self.assertEqual(sorted(e.path for e in plan.menu), sorted([self.ours, self.ours2]))
        kept = dict(plan.kept)
        self.assertIn("another WINEPREFIX", kept[self.two])
        self.assertIn("does not carry gitfurl's marker", kept[self.unmarked])
        self.assertIn("could not be opened safely", kept[self.linked])
        self.assertIn("another WINEPREFIX", kept[self.other])
        self.assertNotIn(self.own, kept)
        res = perform_delete_prefix(plan, **NONE)
        self.assertEqual(sorted(res.menu_removed), sorted([self.ours, self.ours2]))
        for p in (self.two, self.unmarked, self.own, self.other):
            self.assertTrue(p.is_file(), p)
        self.assertTrue(self.linked.is_symlink())
        self.assertTrue((self.base / "marked-elsewhere.desktop").is_file())
        self.assertTrue((self.root / "game-two" / "system.reg").is_file())

    def test_entry_changed_after_the_plan_is_kept(self):
        plan = plan_delete_prefix("game", **NONE)
        self.ours.write_text(self.ours.read_text() + "# edited, still marked\n")
        os.unlink(self.ours2)
        os.symlink(self.base / "marked-elsewhere.desktop", self.ours2)
        res = perform_delete_prefix(plan, **NONE)
        self.assertEqual(res.menu_removed, [])
        self.assertTrue(self.ours.is_file())
        self.assertTrue(self.ours2.is_symlink())
        self.assertEqual(len([w for w in res.left if w.startswith("menu entry")]), 2)
        self.assertIn("some things were left", res.headline(plan))

    def test_window_text(self):
        plan = plan_delete_prefix("game", **NONE)
        text = window_text(plan)
        self.assertTrue(text.startswith("Delete the prefix game? This cannot be undone."))
        self.assertIn(f"Folder: {self.root / 'game'}", text)
        self.assertIn("Size: 198 bytes in 4 file(s) and 6 folder(s)", text)
        self.assertIn("Menu entries to remove (2):", text)
        self.assertIn("  gitfurl-game-game.desktop", text)
        self.assertIn("gitfurl-game-two-game.desktop: its program does not start in this "
                      "prefix", text)
        self.assertNotIn("gitfurl.desktop:", text)

    def test_window_result_text(self):
        plan = plan_delete_prefix("game", **NONE)
        res = perform_delete_prefix(plan, how="window", **NONE)
        text = gui.delete_result_text(plan, res)
        self.assertTrue(text.startswith("Deleted the prefix game and 2 menu entries."))
        self.assertIn(f"removed the menu entry {self.ours}", text)
        self.assertIn(f"Local log: {res.log_path}", text)
        self.assertIn("You approved the plan", res.timeline_path.read_text())
        self.assertEqual(gui.run_state_text("delete", False), "Deleting prefix")


class SpacedPathMenuTests(MenuEntryTests):
    """The same with a space in the data folder: Exec then quotes WINEPREFIX=..."""
    data_name = "my data"

    def test_quoted(self):
        self.assertIn('Exec=env "WINEPREFIX=', self.ours.read_text())


class CliTests(Base):
    def test_dry_run_deletes_nothing(self):
        prefix = self.make_prefix()
        ours = self.entry("game", "game")
        code, out, err = run_cli("prefix", "delete", "game", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("Plan (dry run, nothing changed):", out)
        self.assertIn(f"delete the prefix folder {prefix}", out)
        self.assertIn(f"remove the menu entry {ours}", out)
        self.assertTrue((prefix / "system.reg").is_file())
        self.assertTrue(ours.is_file())
        self.assertFalse(self.state.exists())

    def test_yes(self):
        prefix = self.make_prefix()
        ours = self.entry("game", "game")
        code, out, err = run_cli("prefix", "delete", "game", "--yes")
        self.assertEqual(code, 0, out + err)
        self.assertFalse(os.path.lexists(prefix))
        self.assertFalse(ours.exists())
        self.assertIn("Deleted the prefix game and 1 menu entry.", out)
        self.assertIn("log: ", out)
        code, out, _ = run_cli("log", "game")
        self.assertEqual(code, 0)
        self.assertIn("Approved with --yes", out)

    def test_declined(self):
        prefix = self.make_prefix()
        with mock.patch.object(sys, "stdin") as stdin, \
                mock.patch("builtins.input", return_value="no"):
            stdin.isatty.return_value = True
            code, out, _ = run_cli("prefix", "delete", "game")
        self.assertEqual(code, 2)
        self.assertIn("Not approved. Nothing was deleted.", out)
        self.assertTrue((prefix / "system.reg").is_file())
        with mock.patch.object(sys, "stdin") as stdin:
            stdin.isatty.return_value = False
            code, out, _ = run_cli("prefix", "delete", "game")
        self.assertEqual(code, 2)
        self.assertIn("no terminal to ask on", out)
        self.assertTrue((prefix / "system.reg").is_file())

    def test_typed_yes(self):
        prefix = self.make_prefix()
        with mock.patch.object(sys, "stdin") as stdin, \
                mock.patch("builtins.input", return_value="yes"):
            stdin.isatty.return_value = True
            code, out, err = run_cli("prefix", "delete", "game")
        self.assertEqual(code, 0, out + err)
        self.assertFalse(os.path.lexists(prefix))

    def test_refused(self):
        (self.root / "game").mkdir(parents=True)
        code, _out, err = run_cli("prefix", "delete", "game", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("does not look like a Wine prefix", err)
        self.assertIn("Nothing was deleted.", err)
        code, _out, err = run_cli("prefix", "delete", "../x", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("invalid prefix name", err)


if __name__ == "__main__":
    unittest.main()
