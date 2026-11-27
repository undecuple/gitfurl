"""The setup's registry entries and shortcuts on the native route (gitfurl.setup_entries).

Synthetic only: Inno Setup installers built by tests/inno_writer.py, FreeArc archives by
tests/farc_writer.py, and small hand-written registry files in Wine's text format. No Wine
program runs and no real installer is used; a running Wine server is mocked."""

import os
import stat
import unittest
from pathlib import Path
from unittest import mock

try:
    from test_route import GAME, Base, lzma_blocks, no_process, run_cli, snapshot
except ImportError:  # python -m unittest tests.test_setup_entries
    from tests.test_route import GAME, Base, lzma_blocks, no_process, run_cli, snapshot

try:
    import inno_writer as w
except ImportError:
    from tests import inno_writer as w

from gitfurl import procgroup
from gitfurl import setup_entries as se
from gitfurl.route import NativeInstallPlan, run_native

SYSTEM_REG = (
    "WINE REGISTRY Version 2\n"
    ";; All keys relative to \\\\Machine\n"
    "\n"
    "#arch=win64\n"
    "\n"
    "[Software\\\\Test] 1700000000\n"
    "#time=1d9e0a0b0c0d0e0\n"
    "\"Old\"=\"keep\"\n"
    "\"Path\"=\"before\"\n"
    "\n"
    "[Software\\\\Wine] 1700000000\n"
    "#time=1d9e0a0b0c0d0e0\n"
    "\"Bin\"=hex:01,02,\\\n"
    "  03,04\n"
    "\"Version\"=\"x\"\n"
).encode()

USER_REG = (
    "WINE REGISTRY Version 2\n"
    ";; All keys relative to \\\\User\\\\S-1-5-21-0-0-0-1000\n"
    "\n"
    "#arch=win64\n"
    "\n"
    "[Control Panel\\\\Desktop] 1700000000\n"
    "#time=1d9e0a0b0c0d0e0\n"
    "\"Wallpaper\"=\"\"\n"
).encode()


def ctx(arch="win64", mode64=False, admin=True, user="tester"):
    return se.Context("C:\\Games\\p1", arch, mode64, admin, user)


def value(key, name, text, kind="string", hive="system", **kw):
    return se.RegValue(hive, "HKLM", key, name, kind, text, text, **kw)


class ExpandTests(unittest.TestCase):
    def test_the_table(self):
        c = ctx()
        for text, want in (
                ("{app}\\bin", "C:\\Games\\p1\\bin"), ("{sys}", "C:\\windows\\system32"),
                ("{win}", "C:\\windows"), ("{pf}", "C:\\Program Files (x86)"),
                ("{commonpf64}", "C:\\Program Files"), ("{cf32}",
                                                         "C:\\Program Files (x86)\\Common Files"),
                ("{userappdata}", "C:\\users\\tester\\AppData\\Roaming"),
                ("{localappdata}", "C:\\users\\tester\\AppData\\Local"),
                ("{commonappdata}", "C:\\ProgramData"), ("{autopf}", "C:\\Program Files (x86)"),
                ("{{literal}", "{literal}"), ("plain", "plain")):
            self.assertEqual(se.expand(text, c), (want, ""), text)
        self.assertEqual(se.expand("{pf}", ctx(mode64=True))[0], "C:\\Program Files")
        self.assertEqual(se.expand("{pf}", ctx(arch="win32"))[0], "C:\\Program Files")
        self.assertEqual(se.expand("{autopf}", ctx(admin=False))[0],
                         "C:\\users\\tester\\AppData\\Local\\Programs")
        self.assertEqual(se.expand("a{break}b", c, multi=True)[0], "a\0b")

    def test_skip_reasons(self):
        c = ctx()
        for text, part in (("{code:GetDir}", "{code:...} (a [Code] function)"),
                           ("{reg:HKLM\\X,Y|d}", "{reg:...} (a registry lookup)"),
                           ("{param:dir}", "{param:...} (a command-line parameter)"),
                           ("{cm:Play}", "{cm:...}"), ("{%PATH}", "environment variable"),
                           ("{tmp}\\x", "uses {tmp}, which gitfurl does not expand"),
                           ("{src}", "uses {src}"), ("{break}", "uses {break}"),
                           ("{app", "without its '}'"), ("{code:A|{app}}", "{code:...}")):
            got, why = se.expand(text, c)
            self.assertIsNone(got, text)
            self.assertIn(part, why, text)
        self.assertIn("user folder is not known", se.expand("{userappdata}", ctx(user=None))[1])
        self.assertIn("architecture", se.expand("{pf}", ctx(arch=None, mode64=None))[1])
        self.assertIn("install mode", se.expand("{autopf}", ctx(admin=None))[1])
        self.assertIn("architecture", se.expand("{pf64}", ctx(arch="win32"))[1])


class RegFormatTests(unittest.TestCase):
    def test_escape_and_back(self):
        for text in ("C:\\Games\\p1", 'say "hi"', "tab\there", "nul\0end", "caf\u00e9 \u4e2d",
                     "[x]", "\U0001f600"):
            esc = se.reg_escape(text, '"')
            self.assertTrue(esc.isascii())
            back, end = se.reg_unescape('"' + esc + '"', 1, '"')
            self.assertEqual(back.encode("utf-16-le", "surrogatepass"),
                             text.encode("utf-16-le", "surrogatepass"))
            self.assertEqual(end, len(esc) + 1)
        self.assertEqual(se.reg_escape("a\\b", '"'), "a\\\\b")
        self.assertEqual(se.reg_escape("[k]", "[]"), "\\[k\\]")

    def test_value_texts(self):
        self.assertEqual(se._format("string", "C:\\x")[0], '"C:\\\\x"')
        self.assertEqual(se._format("expandsz", "%PATH%")[0], 'str(2):"%PATH%"')
        self.assertEqual(se._format("multisz", "a\0b")[0], 'str(7):"a\\0b\\0"')
        self.assertEqual(se._format("dword", "42")[0], "dword:0000002a")
        self.assertEqual(se._format("dword", "$ff")[0], "dword:000000ff")
        self.assertEqual(se._format("dword", "-1")[0], "dword:ffffffff")
        self.assertEqual(se._format("qword", "1")[0], "hex(b):01,00,00,00,00,00,00,00")
        self.assertEqual(se._format("binary", "00 01 ff")[0], "hex:00,01,ff")
        self.assertIn("not a number", se._format("dword", "abc"))
        self.assertIn("not hex bytes", se._format("binary", "zz"))
        self.assertIn("longer than", se._format("string", "x" * (se.MAX_DATA + 1)))


class EditRegistryTests(unittest.TestCase):
    def test_value_added_into_an_existing_key_and_the_rest_is_byte_identical(self):
        new, done = se.edit_registry(SYSTEM_REG, [value("software\\TEST", "New", '"v"')])
        self.assertEqual(done, ["added: HKLM\\software\\TEST 'New' = string \"v\""])
        added = b'"New"="v"\n'
        self.assertEqual(new.count(added), 1)
        self.assertEqual(new.replace(added, b"", 1), SYSTEM_REG)
        self.assertLess(new.index(added), new.index(b"[Software\\\\Wine]"))
        self.assertTrue(new.startswith(b"WINE REGISTRY Version 2\n"))

    def test_value_replaced_in_place(self):
        new, done = se.edit_registry(SYSTEM_REG, [value("Software\\Test", "path", '"after"')])
        self.assertEqual(new, SYSTEM_REG.replace(b'"Path"="before"', b'"path"="after"'))
        self.assertTrue(done[0].startswith("replaced:"))

    def test_value_over_continuation_lines_is_replaced_whole(self):
        new, _ = se.edit_registry(SYSTEM_REG, [value("Software\\Wine", "Bin", "hex:ff",
                                                     kind="binary")])
        self.assertIn(b'"Bin"=hex:ff\n"Version"="x"\n', new)
        self.assertNotIn(b"03,04", new)

    def test_new_key_is_appended_and_nothing_else_changes(self):
        new, done = se.edit_registry(SYSTEM_REG, [
            value("Software\\Classes\\.foo", "", '"FooFile"'),
            value("Software\\Classes\\.foo", "Content Type", '"text/x-foo"')], now=1700000000)
        self.assertTrue(new.startswith(SYSTEM_REG))
        tail = new[len(SYSTEM_REG):].decode()
        self.assertTrue(tail.startswith("\n[Software\\\\Classes\\\\.foo] 1700000000\n#time="))
        self.assertTrue(tail.endswith('@="FooFile"\n"Content Type"="text/x-foo"\n'))
        self.assertEqual([d.split(":")[0] for d in done], ["added", "added"])

    def test_flags_keep_and_dont_create(self):
        new, done = se.edit_registry(SYSTEM_REG, [
            value("Software\\Test", "Old", '"no"', only_if_missing=True),
            value("Software\\Missing", "A", '"b"', key_must_exist=True),
            value("Software\\Test", "", "", kind="none"),
            value("Software\\Empty", "", "", kind="none")])
        self.assertIn("kept the value that was there", done[0])
        self.assertIn("dontcreatekey", done[1])
        self.assertIn("already there", done[2])
        self.assertIn("created the key", done[3])
        self.assertNotIn(b"Missing", new)
        self.assertTrue(new.startswith(SYSTEM_REG))
        self.assertIn(b"[Software\\\\Empty]", new)

    def test_a_file_without_the_header_is_refused(self):
        with self.assertRaises(se.EntriesRefused):
            se.edit_registry(b"REGEDIT4\n", [value("A", "b", '"c"')])


class FakeProc:
    """A /proc with one process and a /proc/locks file, in a temporary folder."""

    def __init__(self, root: Path):
        self.root = root
        (root / "proc" / "123").mkdir(parents=True)
        (root / "proc" / "locks").write_text("")
        (root / "tmp").mkdir()


class ServerCheckTests(Base):
    def test_finds_a_server_by_its_working_folder_or_its_lock(self):
        self.make_prefix()
        fake = FakeProc(self.base / "fake")
        proc, tmp = str(fake.root / "proc"), str(fake.root / "tmp")
        self.assertIsNone(procgroup.prefix_server_running(self.prefix, proc, tmp))
        sdir = procgroup.wine_server_dir(self.prefix, tmp)
        sdir.mkdir(parents=True)
        (sdir / "socket").write_text("")  # a left-over socket alone does not count
        self.assertIsNone(procgroup.prefix_server_running(self.prefix, proc, tmp))
        os.symlink(sdir, fake.root / "proc" / "123" / "cwd")
        self.assertIn("process 123", procgroup.prefix_server_running(self.prefix, proc, tmp))
        os.unlink(fake.root / "proc" / "123" / "cwd")
        (sdir / "lock").write_text("")
        st = os.lstat(sdir / "lock")
        (fake.root / "proc" / "locks").write_text(
            f"1: POSIX  ADVISORY  WRITE 99 {os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:"
            f"{st.st_ino} 0 EOF\n")
        self.assertIn("lock file", procgroup.prefix_server_running(self.prefix, proc, tmp))
        st_prefix = os.stat(self.prefix)
        self.assertEqual(sdir.name, f"server-{st_prefix.st_dev:x}-{st_prefix.st_ino:x}")


class EntriesBase(Base):
    """An Inno Setup installer with [Registry] and [Icons] entries, its FreeArc archive beside
    it (bin/Game.exe, readme.txt) and a prefix with Wine's registry files."""

    REGS = [
        w.WReg("HKLM", "Software\\Test", "Path", "{app}"),
        w.WReg("HKLM", "Software\\Test", "Bits", "{app}\\bin", flags=("64bit",)),
        w.WReg("HKCU", "Software\\Test", "Count", "7", type="dword",
               flags=("uninsdeletekey",)),
        w.WReg("HKCR", ".foo", "", "FooFile"),
        w.WReg("HKCU", "Software\\Test", "Data", "{userappdata}\\x", type="expandsz"),
        w.WReg("HKCU", "Software\\Test", "Code", "{code:GetDir}"),
        w.WReg("HKCU", "Software\\Test", "Checked", "1", check="IsWin64"),
        w.WReg("HKCU", "Software\\Test", "Hooked", "1", before_install="Hook"),
        w.WReg("HKCU", "Software\\Test", "Task", "1", tasks="extra"),
        w.WReg("HKCU", "Software\\Test", "Default task", "1", tasks="main"),
        w.WReg("HKU", ".DEFAULT\\Test", "A", "1"),
        w.WReg("HKCU", "Software\\Gone", "", "", type="none", flags=("deletekey",)),
        w.WReg("HKCU", "Software\\Test", "Old", "x", below_nt=6 << 24),
    ]
    ICONS = [
        w.WIcon("{group}\\Play the Game", "{app}\\bin\\Game.exe", parameters='-w "a b"',
                working_dir="{app}"),
        w.WIcon("{commondesktop}\\Play the Game", "{app}\\bin\\Game.exe", tasks="desktopicon"),
        w.WIcon("{group}\\Again", "{app}\\bin\\Game.exe"),
        w.WIcon("{group}\\Readme", "{app}\\missing.txt"),
        w.WIcon("{group}\\Notepad", "{win}\\notepad.exe"),
        w.WIcon("{group}\\Uninstall", "{uninstallexe}"),
        w.WIcon("{group}\\Code", "{code:Exe}"),
    ]

    def setUp(self):
        super().setUp()
        data, _ = w.build(w.Spec(registry=self.REGS, icons=self.ICONS, privileges=2,
                                 tasks=[("main", 0), ("extra", 2), ("desktopicon", 0)]))
        self.setup.write_bytes(data)
        self.arc("NAME-01.bin", lzma_blocks())
        self.apps = self.data / "applications"

    def make_prefix(self, reg=True):
        super().make_prefix()
        (self.prefix / "drive_c" / "users" / "tester").mkdir(parents=True)
        (self.prefix / "drive_c" / "users" / "Public").mkdir()
        if reg:
            (self.prefix / "system.reg").write_bytes(SYSTEM_REG)
            (self.prefix / "user.reg").write_bytes(USER_REG)
            os.chmod(self.prefix / "system.reg", 0o644)

    def entries(self):
        choice = self.choose()
        self.assertEqual(choice.route, "native", choice.line())
        return choice, se.plan_setup_entries(self.setup, choice.extract, self.env)

    def skipped(self, plan, label):
        got = [s for s in plan.skipped if label in s]
        self.assertEqual(len(got), 1, (label, plan.skipped))
        return got[0]


class PlanTests(EntriesBase):
    def test_values_and_skip_reasons(self):
        self.make_prefix()
        _c, plan = self.entries()
        shown = [f"{se.HIVES[v.hive]}: {v.display()}" for v in plan.values]
        self.assertEqual(shown, [
            "system.reg: HKLM\\Software\\Wow6432Node\\Test 'Path' = string 'C:\\\\Games\\\\p1'",
            "system.reg: HKLM\\Software\\Test 'Bits' = string 'C:\\\\Games\\\\p1\\\\bin'",
            "user.reg: HKCU\\Software\\Test 'Count' = dword 7",
            "system.reg: HKLM\\Software\\Classes\\.foo (default) = string 'FooFile'",
            "user.reg: HKCU\\Software\\Test 'Data' = expandsz "
            "'C:\\\\users\\\\tester\\\\AppData\\\\Roaming\\\\x'",
            "user.reg: HKCU\\Software\\Test 'Default task' = string '1'",
        ])
        self.assertIn("uninsdeletekey not applied", plan.values[2].notes[0])
        self.assertIn("default components or tasks", plan.values[5].notes[0])
        self.assertIn("{code:...}", self.skipped(plan, "'Code'"))
        self.assertIn("Check function", self.skipped(plan, "'Checked'"))
        self.assertIn("calls [Code]", self.skipped(plan, "'Hooked'"))
        self.assertIn("not part of the default tasks", self.skipped(plan, "'Task'"))
        self.assertIn("HKU is a root gitfurl does not write", self.skipped(plan, "HKU\\"))
        self.assertIn("only deletes", self.skipped(plan, "Software\\Gone"))
        self.assertIn("Windows versions below", self.skipped(plan, "'Old'"))

    def test_hkcr_and_hka_follow_the_install_mode(self):
        class Lowest:
            privileges = 3

        class Admin:
            privileges = 2

        c = ctx()
        self.assertIn("does not guess", se._place("HKCR", ".foo", False, c, Lowest))
        self.assertEqual(se._place("HKCR", ".foo", False, c, Admin),
                         ("system", "HKLM", "Software\\Classes\\.foo"))
        self.assertEqual(se._place("HKA", "Software\\X", True, c, Lowest),
                         ("user", "HKCU", "Software\\X"))
        self.assertEqual(se._place("HKA", "Software\\X", False, c, Admin),
                         ("system", "HKLM", "Software\\Wow6432Node\\X"))

    def test_32_bit_prefix_has_no_redirection(self):
        self.make_prefix()
        (self.prefix / "system.reg").write_bytes(SYSTEM_REG.replace(b"win64", b"win32"))
        _c, plan = self.entries()
        self.assertEqual(plan.values[0].key, "Software\\Test")
        self.assertIn("64-bit registry view", self.skipped(plan, "'Bits'"))
        self.assertIn("C:\\\\Program Files", se._format("string", se.expand(
            "{pf}", se.Context("C:\\Games\\p1", "win32", False, True, "u"))[0])[0])

    def test_shortcuts(self):
        self.make_prefix()
        _c, plan = self.entries()
        self.assertEqual([sc.path.name for sc in plan.shortcuts],
                         ["gitfurl-p1-play-the-game.desktop"])
        sc = plan.shortcuts[0]
        self.assertEqual(sc.target, "C:\\Games\\p1\\bin\\Game.exe")
        lines = sc.text.splitlines()
        self.assertTrue(lines[0].startswith("# Written by gitfurl"))
        self.assertIn("Name=Play the Game", lines)
        self.assertIn(f"Exec=env WINEPREFIX={self.prefix} wine "
                      '"C:\\\\\\\\Games\\\\\\\\p1\\\\\\\\bin\\\\\\\\Game.exe" -w "a b"', lines)
        self.assertIn(f"Path={self.prefix}/drive_c/Games/p1", lines)
        self.assertIn("X-Gitfurl-Generated=true", lines)
        self.assertIn("has a condition (Tasks: desktopicon)",
                      self.skipped(plan, "{commondesktop}\\Play"))
        self.assertIn("same program as gitfurl-p1-play-the-game.desktop",
                      self.skipped(plan, "\\Again"))
        self.assertIn("not among the files", self.skipped(plan, "\\Readme"))
        self.assertIn("not under {app}", self.skipped(plan, "\\Notepad"))
        self.assertIn("uses {uninstallexe}", self.skipped(plan, "\\Uninstall"))
        self.assertIn("{code:...}", self.skipped(plan, "{group}\\Code"))
        self.assertEqual(plan.new_dirs, [self.apps])

    def test_a_file_gitfurl_did_not_write_is_skipped(self):
        self.make_prefix()
        self.apps.mkdir(parents=True)
        (self.apps / "gitfurl-p1-play-the-game.desktop").write_text("[Desktop Entry]\n")
        _c, plan = self.entries()
        self.assertNotIn("gitfurl-p1-play-the-game.desktop",
                         [sc.path.name for sc in plan.shortcuts])
        self.assertIn("was not written by gitfurl", self.skipped(plan, "{group}\\Play the Game ->"))

    def test_no_registry_files_yet(self):
        self.make_prefix(reg=False)
        _c, plan = self.entries()
        self.assertEqual(plan.values, [])
        self.assertIn("no system.reg and user.reg yet", self.skipped(plan, "[Registry]"))
        self.assertEqual(len(plan.shortcuts), 1)

    def test_a_prefix_to_be_created_is_assumed_and_said_so(self):
        choice = self.choose()
        plan = se.plan_setup_entries(self.setup, choice.extract, {**self.env, "USER": "tester"})
        self.assertTrue(plan.facts.assumed)
        self.assertEqual((plan.facts.arch, plan.facts.user), ("win64", "tester"))
        self.assertIn("assumed", "\n".join(plan.describe()))

    def test_an_unreadable_setup_plans_nothing(self):
        self.make_prefix()
        choice = self.choose()
        plan = se.plan_setup_entries(self.base / "missing.exe", choice.extract, self.env)
        self.assertTrue(plan.empty)
        self.assertIn("could not be read", plan.notes[0])

    def test_describe_lists_every_value_file_and_skip(self):
        self.make_prefix()
        _c, plan = self.entries()
        text = "\n".join(plan.describe())
        for v in plan.values:
            self.assertIn(v.display(), text)
        self.assertIn(str(plan.shortcuts[0].path), text)
        for s in plan.skipped:
            self.assertIn(s, text)
        self.assertIn("copied into the log folder (mode 0600)", text)


class ApplyTests(EntriesBase):
    def apply(self, plan, running=lambda p: None, **kw):
        logs = self.base / "logs"
        logs.mkdir(exist_ok=True)
        return se.apply_entries(plan, logs, "20261127-120000", running=running,
                                sleep=lambda s: None, **kw), logs

    def test_writes_values_backups_and_the_menu_entry(self):
        self.make_prefix()
        _c, plan = self.entries()
        ino = os.stat(self.prefix / "system.reg").st_ino
        out, logs = self.apply(plan)
        text = "\n".join(out)
        system = (self.prefix / "system.reg").read_bytes()
        self.assertTrue(system.startswith(SYSTEM_REG.split(b"[Software\\\\Wine]")[0][:60]))
        self.assertIn(b'[Software\\\\Wow6432Node\\\\Test]', system)
        self.assertIn(b'"Path"="C:\\\\Games\\\\p1"', system)
        self.assertIn(b'[Software\\\\Classes\\\\.foo]', system)
        self.assertIn(b'"Bits"="C:\\\\Games\\\\p1\\\\bin"\n', system)
        # the key that was there keeps every line, with the new value added
        self.assertIn(b'"Old"="keep"\n"Path"="before"\n"Bits"=', system)
        self.assertIn(b'[Software\\\\Wine] 1700000000\n#time=1d9e0a0b0c0d0e0\n"Bin"=hex:01,02,'
                      b'\\\n  03,04\n"Version"="x"\n', system)
        user = (self.prefix / "user.reg").read_bytes()
        self.assertTrue(user.startswith(USER_REG))
        self.assertIn(b'"Count"=dword:00000007', user)
        self.assertIn(b'"Data"=str(2):"C:\\\\users\\\\tester\\\\AppData\\\\Roaming\\\\x"', user)
        # backups: the bytes before, mode 0600
        for name, before in (("system.reg", SYSTEM_REG), ("user.reg", USER_REG)):
            backup = logs / f"p1-20261127-120000-{name}"
            self.assertEqual(backup.read_bytes(), before)
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
            self.assertIn(str(backup), text)
        # temporary file and rename: a new inode, the old mode, nothing left beside it
        st = os.stat(self.prefix / "system.reg")
        self.assertNotEqual(st.st_ino, ino)
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o644)
        self.assertEqual(sorted(p.name for p in self.prefix.iterdir()),
                         ["drive_c", "system.reg", "user.reg"])
        desktop = self.apps / "gitfurl-p1-play-the-game.desktop"
        self.assertEqual(desktop.read_text(), plan.shortcuts[0].text)
        self.assertEqual(stat.S_IMODE(desktop.stat().st_mode), 0o644)
        self.assertIn(f"menu entry: wrote {desktop}", text)
        self.assertIn("added: HKLM\\Software\\Wow6432Node\\Test 'Path'", text)

    def test_counts_what_was_done(self):
        self.make_prefix()
        _c, plan = self.entries()
        applied = se.Applied()
        out, _ = self.apply(plan, applied=applied)
        done = [ln for ln in out if ln.lstrip().startswith(("added: ", "replaced: ",
                                                            "created the key: "))]
        self.assertEqual(sum(applied.values.values()), len(done))
        self.assertGreater(len(done), 0)
        self.assertEqual(set(applied.values), {"system.reg", "user.reg"})
        self.assertEqual(applied.menu_entries, 1)

    def test_refused_while_a_wine_server_runs(self):
        self.make_prefix()
        _c, plan = self.entries()
        clock = iter(range(0, 1000, 10))
        applied = se.Applied()
        out, logs = self.apply(plan, running=lambda p: "process 9 works in it",
                               clock=lambda: next(clock), applied=applied)
        self.assertEqual(applied.values, {})
        self.assertIn("a Wine server for this prefix is running", applied.not_done[0])
        self.assertIn("nothing written: a Wine server for this prefix is running", out[0])
        self.assertEqual((self.prefix / "system.reg").read_bytes(), SYSTEM_REG)
        self.assertEqual((self.prefix / "user.reg").read_bytes(), USER_REG)
        self.assertEqual(list(logs.iterdir()), [])

    def test_waits_for_a_server_that_ends(self):
        self.make_prefix()
        _c, plan = self.entries()
        answers = iter(["still there", "still there", None, None, None])
        out, _ = self.apply(plan, running=lambda p: next(answers))
        self.assertIn(b"Wow6432Node", (self.prefix / "system.reg").read_bytes())
        self.assertFalse(any("nothing written" in ln for ln in out))

    def test_a_server_that_starts_meanwhile_stops_the_rename(self):
        self.make_prefix()
        _c, plan = self.entries()
        answers = iter([None, "started"])
        out, _ = self.apply(plan, running=lambda p: next(answers, "started"))
        self.assertIn("system.reg not changed: a Wine server for this prefix started", out[0])
        self.assertEqual((self.prefix / "system.reg").read_bytes(), SYSTEM_REG)
        self.assertEqual(sorted(p.name for p in self.prefix.iterdir()),
                         ["drive_c", "system.reg", "user.reg"])

    def test_a_prefix_unlike_the_plan_gets_no_registry_values(self):
        self.make_prefix()
        _c, plan = self.entries()
        (self.prefix / "system.reg").write_bytes(SYSTEM_REG.replace(b"win64", b"win32"))
        out, _ = self.apply(plan)
        self.assertIn("not what the plan assumed", out[0])
        self.assertEqual((self.prefix / "user.reg").read_bytes(), USER_REG)

    def test_a_foreign_menu_file_that_appears_later_is_kept(self):
        self.make_prefix()
        _c, plan = self.entries()
        self.apps.mkdir(parents=True)
        path = self.apps / "gitfurl-p1-play-the-game.desktop"
        path.write_text("mine\n")
        out, _ = self.apply(plan)
        self.assertEqual(path.read_text(), "mine\n")
        self.assertTrue(any("was not written by gitfurl" in ln for ln in out))

    def test_ours_is_replaced(self):
        self.make_prefix()
        self.apps.mkdir(parents=True)
        path = self.apps / "gitfurl-p1-play-the-game.desktop"
        path.write_text("old\nX-Gitfurl-Generated=true\n")
        _c, plan = self.entries()
        self.assertTrue(plan.shortcuts[0].replaces)
        self.assertIn("replace gitfurl's entry", "\n".join(plan.describe()))
        self.apply(plan)
        self.assertEqual(path.read_text(), plan.shortcuts[0].text)

    def test_a_linked_registry_file_is_refused(self):
        self.make_prefix()
        _c, plan = self.entries()
        real = self.base / "elsewhere.reg"
        real.write_bytes(USER_REG)
        (self.prefix / "user.reg").unlink()
        os.symlink(real, self.prefix / "user.reg")
        out, _ = self.apply(plan)
        self.assertTrue(any(ln.startswith("registry: user.reg not changed") for ln in out))
        self.assertEqual(real.read_bytes(), USER_REG)


class RunTests(EntriesBase):
    def native_plan(self, entries=True):
        choice, plan = self.entries()
        return NativeInstallPlan(self.setup, "p1", self.prefix, choice, False, None,
                                 setup_entries=plan if entries else None)

    def test_plan_text_lists_the_step_and_says_what_is_still_skipped(self):
        self.make_prefix()
        text = "\n".join(self.native_plan().describe())
        self.assertIn("step 3: only after a complete unpack: add the registry entries", text)
        self.assertIn("shortcuts and registry entries other than those the plan lists", text)
        self.assertIn("gitfurl-p1-play-the-game.desktop", text)
        off = "\n".join(self.native_plan(entries=False).describe())
        self.assertNotIn("add the registry entries", off)
        self.assertIn("shortcuts, registry entries, redistributables", off)

    def test_run_native_writes_them_after_the_unpack_and_logs_them(self):
        self.make_prefix()
        plan = self.native_plan()
        with no_process(), mock.patch.object(procgroup, "prefix_server_running",
                                              return_value=None):
            res = run_native(plan, env=self.env)
        self.assertEqual(res.status, 0)
        self.assertIn(b"Wow6432Node", (self.prefix / "system.reg").read_bytes())
        log = res.log_path.read_text()
        self.assertIn("--- setup entries (registry values and menu entries):", log)
        self.assertIn("menu entry: wrote", log)
        backups = sorted(res.log_path.parent.glob("p1-*-system.reg"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), SYSTEM_REG)

    def run_with_timeline(self, plan, **kw):
        from gitfurl.timeline import Timeline

        tl = Timeline()
        with no_process(), mock.patch.object(procgroup, "prefix_server_running",
                                              return_value=None):
            res = run_native(plan, env=self.env, timeline=tl, **kw)
        tl.end("done")
        return res, tl

    def test_timeline_rows_for_the_values_menu_entries_and_skips(self):
        self.make_prefix()
        plan = self.native_plan()
        res, tl = self.run_with_timeline(plan)
        self.assertEqual(res.status, 0)
        rows = [(e.kind, e.what, e.why) for e in tl.entries()]
        kinds = [k for k, _w, _y in rows]
        whats = [w for _k, w, _y in rows]
        sys_n = sum(1 for v in plan.setup_entries.values if v.hive == "system")
        self.assertIn(f"Registry values written into system.reg: {sys_n}", whats)
        self.assertTrue(any(w.startswith("Registry values written into user.reg: ")
                            for w in whats))
        self.assertIn("Menu entries written: 1", whats)
        skips = [y for k, w, y in rows if w == "Setup entry not applied"]
        self.assertEqual(len(skips), min(len(plan.setup_entries.skipped), 20))
        self.assertTrue(any("has a condition (Tasks: desktopicon)" in y for y in skips))
        # after the unpack's rows, before the end
        self.assertLess(whats.index("Menu entries written: 1"), kinds.index("end"))
        self.assertGreater(whats.index("Menu entries written: 1"),
                           next(i for i, w in enumerate(whats) if "CRC-32 checked" in w))
        saved = tl.path.read_text()
        self.assertIn("Registry values written into system.reg", saved)
        self.assertIn("Menu entries written: 1", saved)

    def test_timeline_says_why_no_value_was_written(self):
        self.make_prefix()
        plan = self.native_plan()
        (self.prefix / "system.reg").write_bytes(SYSTEM_REG.replace(b"win64", b"win32"))
        _res, tl = self.run_with_timeline(plan)
        rows = [(e.kind, e.what, e.why) for e in tl.entries()]
        self.assertIn("No registry value was written", [w for _k, w, _y in rows])
        self.assertTrue(any(w == "Setup entry not applied" and "not what the plan assumed" in y
                            for _k, w, y in rows))

    def test_timeline_counts_the_skips_past_the_cap(self):
        from gitfurl import route

        self.make_prefix()
        plan = self.native_plan()
        n = len(plan.setup_entries.skipped)
        self.assertGreater(n, 2)
        with mock.patch.object(route, "TL_SKIPPED_ROWS", 2):
            _res, tl = self.run_with_timeline(plan)
        rows = [(e.what, e.why) for e in tl.entries()]
        self.assertEqual(sum(1 for w, _y in rows if w == "Setup entry not applied"), 2)
        self.assertIn((f"{n - 2} more setup entries not applied",
                       "the log lists each one with its reason"), rows)

    def test_stop_before_the_entries_writes_none(self):
        self.make_prefix()
        plan = self.native_plan()
        import threading

        cancel = threading.Event()
        from gitfurl import extract as extract_mod

        real = extract_mod.extract

        def then_stop(*a, **kw):
            out = real(*a, **kw)
            cancel.set()
            return out

        with no_process(), mock.patch.object(extract_mod, "extract", then_stop):
            res = run_native(plan, cancel=cancel, env=self.env)
        self.assertIn("setup entries: not added, Stop was asked for", res.lines)
        self.assertEqual((self.prefix / "system.reg").read_bytes(), SYSTEM_REG)
        self.assertFalse(self.apps.exists())

    def test_timeline_after_a_stop_says_they_were_not_added(self):
        self.make_prefix()
        plan = self.native_plan()
        import threading

        from gitfurl import extract as extract_mod
        from gitfurl.timeline import Timeline

        cancel = threading.Event()
        real = extract_mod.extract

        def then_stop(*a, **kw):
            out = real(*a, **kw)
            cancel.set()
            return out

        tl = Timeline()
        with no_process(), mock.patch.object(extract_mod, "extract", then_stop):
            run_native(plan, cancel=cancel, env=self.env, timeline=tl)
        rows = [(e.kind, e.what, e.why) for e in tl.entries()]
        self.assertIn(("skip", "The setup's registry entries and menu entries were not added",
                       "Stop was asked for"), rows)
        self.assertFalse(any(w.startswith("Registry values written") for _k, w, _y in rows))


class CliTests(EntriesBase):
    def test_dry_run_lists_everything_and_writes_nothing(self):
        self.make_prefix()
        before = snapshot(self.base)
        with no_process():
            code, out, err = run_cli("install", str(self.setup), "--prefix", "p1",
                                     "--dry-run", "--apply-setup-entries")
        self.assertEqual(code, 0, err)
        self.assertEqual(before, snapshot(self.base))
        self.assertIn("DRY RUN", out)
        self.assertIn("add the registry entries and menu entries the setup declares", out)
        self.assertIn("system.reg: HKLM\\Software\\Wow6432Node\\Test 'Path'", out)
        self.assertIn("gitfurl-p1-play-the-game.desktop", out)
        self.assertIn("skipped (not applied):", out)
        self.assertIn("{code:...}", out)

    def test_real_run_writes_and_reports(self):
        self.make_prefix()
        with no_process(), mock.patch.object(procgroup, "prefix_server_running",
                                              return_value=None):
            code, out, err = run_cli("install", str(self.setup), "--prefix", "p1", "--yes",
                                     "--apply-setup-entries")
        self.assertEqual(code, 0, err)
        self.assertEqual((self.prefix / "drive_c/Games/p1/bin/Game.exe").read_bytes(), GAME)
        self.assertIn("setup entries (registry values and menu entries):", out)
        self.assertIn("registry: wrote into", out)
        self.assertIn("menu entry: wrote", out)
        self.assertIn(b'"Count"=dword:00000007', (self.prefix / "user.reg").read_bytes())
        self.assertTrue((self.apps / "gitfurl-p1-play-the-game.desktop").is_file())

    def test_real_run_with_the_timeline_prints_and_saves_the_rows(self):
        self.make_prefix()
        with no_process(), mock.patch.object(procgroup, "prefix_server_running",
                                              return_value=None):
            code, out, err = run_cli("install", str(self.setup), "--prefix", "p1", "--yes",
                                     "--apply-setup-entries", "--timeline")
        self.assertEqual(code, 0, err)
        self.assertIn("Registry values written into system.reg", out)
        self.assertIn("Menu entries written: 1", out)
        self.assertIn("Setup entry not applied", out)
        saved = list((self.state / "gitfurl" / "logs").glob("p1-*.timeline.txt"))
        self.assertEqual(len(saved), 1)
        self.assertIn("Menu entries written: 1", saved[0].read_text())

    def test_switch_off_writes_none_of_it(self):
        self.make_prefix()
        with no_process():
            code, out, err = run_cli("install", str(self.setup), "--prefix", "p1", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual((self.prefix / "system.reg").read_bytes(), SYSTEM_REG)
        self.assertEqual((self.prefix / "user.reg").read_bytes(), USER_REG)
        self.assertFalse(self.apps.exists())
        self.assertNotIn("setup entries", out)
        self.assertEqual(list((self.state / "gitfurl" / "logs").glob("*.reg")), [])

    def test_wine_route_says_it_does_not_apply(self):
        self.make_prefix()
        with no_process(), mock.patch("shutil.which", return_value="/x/wine"):
            code, out, _ = run_cli("install", str(self.setup), "--prefix", "p1", "--dry-run",
                                   "--route", "wine", "--apply-setup-entries")
        self.assertEqual(code, 0)
        self.assertIn("--apply-setup-entries does not apply to the Wine/Proton route", out)


if __name__ == "__main__":
    unittest.main()
