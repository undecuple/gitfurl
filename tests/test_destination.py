"""The native route's destination (--dest, the window's Destination row).

Synthetic only: FreeArc archives from tests/farc_writer.py, Inno Setup headers from
tests/inno_writer.py, and plain temporary folders as prefixes. No Wine program runs, no real
installer is used, and the folder dialog is a mock."""

import os
import stat
import unittest
from pathlib import Path
from unittest import mock

try:
    from farc_writer import make_archive
    from test_extract import Base as ExtractBase
    from test_extract import blocks, run_cli
    from test_setup_entries import EntriesBase
    from test_route import no_process
except ImportError:  # python -m unittest tests.test_destination
    from tests.farc_writer import make_archive
    from tests.test_extract import Base as ExtractBase
    from tests.test_extract import blocks, run_cli
    from tests.test_setup_entries import EntriesBase
    from tests.test_route import no_process

from gitfurl import extract, gui
from gitfurl import setup_entries as se
from gitfurl.extract import (
    DestinationRefused,
    ExtractRefused,
    default_destination,
    parse_destination,
    plan_extract,
)


class ParseTests(unittest.TestCase):
    def test_good_paths(self):
        for text, parts in (
                ("C:\\Games\\My Game", ("Games", "My Game")),
                ("C:/Games/My Game", ("Games", "My Game")),
                ("c:\\games\\x", ("games", "x")),
                ("C:\\Games\\My Game\\", ("Games", "My Game")),  # one trailing separator
                ("  C:\\Apps\\Studio\\Game  ", ("Apps", "Studio", "Game")),
                ("C:\\Game", ("Game",)),
                ("C:\\Program Files\\New Game", ("Program Files", "New Game")),
                ("C:\\Program Files (x86)\\A\\B", ("Program Files (x86)", "A", "B")),
                ("C:\\Spiele\\Grüße", ("Spiele", "Grüße"))):
            self.assertEqual(parse_destination(text), parts, text)

    def refused(self, text, fragment):
        with self.assertRaises(DestinationRefused) as cm:
            parse_destination(text)
        self.assertIn(fragment, str(cm.exception), text)
        self.assertIsInstance(cm.exception, ValueError)
        self.assertNotIsInstance(cm.exception, ExtractRefused)  # never a route fallback
        return str(cm.exception)

    def test_not_on_drive_c_or_not_below_it(self):
        self.refused("", "the destination is empty")
        self.refused("   ", "the destination is empty")
        for text in ("D:\\Games\\x", "Games\\x", "/home/user/x", "C:", "C:Games",
                     "\\\\server\\share\\x", "\\Games\\x"):
            self.refused(text, "must be a folder on drive C: of the prefix")
        for text in ("C:\\", "C:/", "c:\\"):
            self.refused(text, "cannot be C:\\ itself")

    def test_empty_parts_dots_and_control_characters(self):
        self.refused("C:\\Games\\\\x", "an empty part")
        self.refused("C:\\\\Games", "an empty part")
        self.refused("C:\\Games\\x\\\\", "an empty part")
        for text in ("C:\\Games\\..\\windows", "C:\\..", "C:\\Games\\.\\x", "C:/Games/../x"):
            self.refused(text, "may not contain '.' or '..'")
        self.refused("C:\\Games\\a\nb", "control character")
        self.refused("C:\\Games\\a\x7fb", "control character")

    def test_names_windows_cannot_use(self):
        for part in ("CON", "nul", "nul.txt", "COM1", "lpt9.log", "a.", "a ", "a|b", "a<b",
                     "a*b", "a?b", 'a"b', "a:b"):
            self.refused(f"C:\\Games\\{part}\\x", "a name Windows cannot use")
            if part.strip() == part:
                self.refused(f"C:\\Games\\{part}", "a name Windows cannot use")
        # space around the whole text is dropped (as typed into the window's entry)
        self.assertEqual(parse_destination("C:\\Games\\a "), ("Games", "a"))
        self.refused("C:\\" + "x" * 300, "too long")

    def test_wine_folders_are_refused(self):
        for text, where in (("C:\\windows\\x", "C:\\windows"), ("C:\\WINDOWS", "C:\\windows"),
                            ("C:\\users\\me\\Game", "C:\\users"), ("C:\\Users", "C:\\users"),
                            ("C:\\ProgramData\\Game", "C:\\ProgramData"),
                            ("c:/programdata", "C:\\ProgramData")):
            msg = self.refused(text, f"cannot be in {where} (Wine's own folder)")
            self.assertIn("C:\\Games\\My Game", msg)
        msg = self.refused("C:\\Program Files", "cannot be C:\\Program Files itself")
        self.assertIn("a folder that is new directly below it is allowed", msg)
        self.refused("C:\\program files (x86)\\", "cannot be C:\\Program Files (x86) itself")

    def test_default(self):
        self.assertEqual(default_destination("p1"), "C:\\Games\\p1")


class DestBase(ExtractBase):
    def setUp(self):
        super().setUp()
        self.arc("a.arc", make_archive(*blocks()))
        self.drive_c = self.prefix / "drive_c"

    def plan(self, dest=None):
        return plan_extract(self.setup, "p1", dest=dest)


class PlanTests(DestBase):
    def test_default_is_unchanged(self):
        plan = self.plan()
        self.assertEqual(plan.target, "C:\\Games\\p1")
        self.assertEqual(plan.dest, self.drive_c / "Games" / "p1")
        self.assertFalse(plan.dest_chosen)
        self.assertEqual(self.plan("C:\\Games\\p1").dest_chosen, False)

    def test_plan_shows_the_folder_and_its_linux_path(self):
        plan = self.plan("C:/Apps/Studio/My Game")
        self.assertEqual(plan.target, "C:\\Apps\\Studio\\My Game")
        self.assertEqual(plan.dest, self.drive_c / "Apps" / "Studio" / "My Game")
        self.assertTrue(plan.dest_chosen)
        text = "\n".join(plan.describe())
        self.assertIn(f"destination: C:\\Apps\\Studio\\My Game (in the prefix: {plan.dest}; "
                      "the folder you chose)", text)
        self.assertIn("folders above it that are missing are created", text)

    def test_a_bad_destination_is_refused_before_the_archives_are_read(self):
        with mock.patch.object(extract.farc, "read_archive") as read, \
                self.assertRaises(DestinationRefused):
            self.plan("C:\\windows\\x")
        read.assert_not_called()

    def test_an_existing_final_folder(self):
        (self.drive_c / "Apps" / "Game").mkdir(parents=True)
        plan = self.plan("C:\\Apps\\Game")
        self.assertIn("the destination already exists", plan.problem())
        with self.assertRaises(ExtractRefused):
            extract.extract(plan)

    def test_an_existing_final_folder_with_other_letter_case(self):
        (self.drive_c / "Apps" / "game").mkdir(parents=True)
        msg = self.plan("C:\\Apps\\Game").problem()
        self.assertEqual(msg, "C:\\Apps\\Game would be the same folder as C:\\Apps\\game, "
                              "which exists (Windows ignores letter case); use that spelling "
                              "or another name")

    def test_a_parent_with_other_letter_case(self):
        (self.drive_c / "games").mkdir()
        self.assertIn("C:\\Games would be the same folder as C:\\games",
                      self.plan("C:\\Games\\x").problem())

    def test_a_link_in_a_parent_is_not_followed(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.drive_c / "Apps").mkdir()
        (self.drive_c / "Apps" / "Link").symlink_to(outside)
        plan = self.plan("C:\\Apps\\Link\\Game")
        self.assertEqual(plan.problem(), f"C:\\Apps\\Link is a symbolic link in the prefix "
                                         f"({self.drive_c / 'Apps' / 'Link'}); gitfurl does not "
                                         "follow links on the way to the destination")
        with self.assertRaises(ExtractRefused):
            extract.extract(plan)
        # even when the check before is passed (a race), the walk does not follow it
        with self.assertRaises(ExtractRefused) as cm:
            extract._make_dest(plan)
        self.assertIn("is a symbolic link or not a folder", str(cm.exception))
        self.assertEqual(cm.exception.created, [])
        self.assertEqual(list(outside.iterdir()), [])

    def test_a_dangling_link_as_the_first_part(self):
        (self.drive_c / "Apps").symlink_to(self.base / "nowhere")
        plan = self.plan("C:\\Apps\\Game")
        self.assertIn("is a symbolic link in the prefix", plan.problem())
        with self.assertRaises(ExtractRefused):
            extract._make_dest(plan)
        self.assertFalse((self.base / "nowhere").exists())

    def test_a_file_in_the_way(self):
        (self.drive_c / "Apps").write_bytes(b"x")
        self.assertEqual(self.plan("C:\\Apps\\Game").problem(),
                         f"C:\\Apps is not a folder in the prefix ({self.drive_c / 'Apps'})")

    def test_program_files_needs_a_new_folder_directly_below_it(self):
        plan = self.plan("C:\\Program Files\\Studio\\Game")
        self.assertEqual(plan.problem(), "C:\\Program Files does not exist in this prefix; "
                                         "gitfurl does not create Wine's own folders")
        with self.assertRaises(ExtractRefused) as cm:
            extract._make_dest(plan)
        self.assertIn("does not create Wine's own folders", str(cm.exception))
        self.assertFalse((self.drive_c / "Program Files").exists())
        (self.drive_c / "Program Files" / "Common Files").mkdir(parents=True)
        msg = self.plan("C:\\Program Files\\Common Files\\Game").problem()
        self.assertEqual(msg, "C:\\Program Files\\Common Files already exists; below "
                              "C:\\Program Files gitfurl only creates a folder that is new "
                              "directly below it (use another name, or C:\\Games)")
        plan = self.plan("C:\\Program Files\\Studio\\Game")
        self.assertIsNone(plan.problem())
        res = extract.extract(plan)
        self.assertTrue(res.ok, res.lines())
        self.assertEqual((plan.dest / "readme.txt").read_bytes(), b"hello\n")


class ExtractTests(DestBase):
    def test_extract_writes_into_a_nested_new_destination(self):
        (self.drive_c / "Apps").mkdir(mode=0o700)  # an existing parent stays as it is
        plan = self.plan("C:\\Apps\\Studio\\Deep\\My Game")
        self.assertIsNone(plan.problem())
        res = extract.extract(plan)
        self.assertTrue(res.ok, res.lines())
        dest = self.drive_c / "Apps" / "Studio" / "Deep" / "My Game"
        self.assertEqual(res.dest, dest)
        self.assertEqual((dest / "readme.txt").read_bytes(), b"hello\n")
        self.assertTrue((dest / "data" / "big.bin").is_file())
        self.assertFalse((self.drive_c / "Games").exists())
        umask = os.umask(0)
        os.umask(umask)
        for folder in (self.drive_c / "Apps" / "Studio", dest.parent, dest):
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o755 & ~umask, folder)
        self.assertEqual(stat.S_IMODE((self.drive_c / "Apps").stat().st_mode), 0o700)
        self.assertIn(str(dest), res.lines()[0])
        with self.assertRaises(ExtractRefused):  # the folder exists now
            extract.extract(self.plan("C:\\Apps\\Studio\\Deep\\My Game"))

    def test_refusal_lists_the_parents_it_created(self):
        real = extract._open_dir

        def racing(parent, name):
            if name == "Game":
                raise FileNotFoundError(2, "No such file or directory")
            return real(parent, name)

        plan = self.plan("C:\\A\\B\\Game")
        with mock.patch.object(extract, "_open_dir", racing), \
                self.assertRaises(ExtractRefused) as cm:
            extract.extract(plan)
        self.assertEqual(cm.exception.created,
                         [self.drive_c / "A", self.drive_c / "A" / "B", plan.dest])


class CliTests(ExtractBase):
    def setUp(self):
        super().setUp()
        self.arc("a.arc", make_archive(*blocks()))
        self.drive_c = self.prefix / "drive_c"

    def test_extract_dry_run_and_yes(self):
        code, out, _ = run_cli("extract", str(self.setup), "--prefix", "p1", "--dry-run",
                               "--dest", "C:/Apps/My Game")
        self.assertEqual(code, 0)
        self.assertIn("destination: C:\\Apps\\My Game (in the prefix: "
                      f"{self.drive_c / 'Apps' / 'My Game'}; the folder you chose)", out)
        self.assertFalse((self.drive_c / "Apps").exists())
        code, out, _ = run_cli("extract", str(self.setup), "--prefix", "p1", "--yes",
                               "--dest", "C:\\Apps\\My Game")
        self.assertEqual(code, 0, out)
        self.assertEqual((self.drive_c / "Apps" / "My Game" / "readme.txt").read_bytes(),
                         b"hello\n")

    def test_extract_with_a_bad_destination(self):
        code, out, err = run_cli("extract", str(self.setup), "--prefix", "p1", "--yes",
                                 "--dest", "C:\\users\\x")
        self.assertEqual(code, 1)
        self.assertIn("error: the destination cannot be in C:\\users (Wine's own folder)", err)
        self.assertIn("Nothing was written.", err)
        self.assertEqual(list(self.drive_c.iterdir()), [])


class InstallTests(EntriesBase):
    """cli install on the native route with --dest: the unpack, the listings, the log, the
    timeline and the setup's {app}."""

    DEST = "C:\\Apps\\The Game"

    def test_app_is_the_chosen_folder(self):
        self.make_prefix()
        choice = self.choose()
        xplan = choice.extract.with_destination(self.DEST)
        plan = se.plan_setup_entries(self.setup, xplan, self.env)
        shown = [v.display() for v in plan.values]
        self.assertIn("HKLM\\Software\\Wow6432Node\\Test 'Path' = string "
                      "'C:\\\\Apps\\\\The Game'", shown)
        self.assertIn("HKLM\\Software\\Test 'Bits' = string 'C:\\\\Apps\\\\The Game\\\\bin'",
                      shown)
        sc = plan.shortcuts[0]
        self.assertEqual(sc.target, "C:\\Apps\\The Game\\bin\\Game.exe")
        lines = sc.text.splitlines()
        self.assertIn(f"Path={self.prefix}/drive_c/Apps/The Game", lines)
        self.assertTrue(any(ln.startswith("Exec=") and "Apps" in ln and "The Game" in ln
                            for ln in lines), lines)
        self.assertFalse(any("Games" in ln for ln in lines if ln.startswith(("Exec", "Path"))))

    def test_choose_route_takes_the_destination(self):
        from gitfurl.detect import detect
        from gitfurl.route import choose_route

        self.make_prefix()
        choice = choose_route(self.setup, "p1", detect(self.setup), "auto", self.env,
                              dest=self.DEST)
        self.assertEqual(choice.route, "native")
        self.assertEqual(choice.extract.target, self.DEST)
        with self.assertRaises(DestinationRefused):
            choose_route(self.setup, "p1", detect(self.setup), "auto", self.env,
                         dest="C:\\windows")

    def test_real_run_uses_the_folder_everywhere(self):
        self.make_prefix()
        with no_process():
            code, out, err = run_cli("install", str(self.setup), "--prefix", "p1", "--yes",
                                     "--dest", self.DEST, "--timeline")
        self.assertEqual(code, 0, err)
        dest = self.prefix / "drive_c" / "Apps" / "The Game"
        self.assertTrue((dest / "bin" / "Game.exe").is_file())
        self.assertFalse((self.prefix / "drive_c" / "Games").exists())
        self.assertIn(f"destination: C:\\Apps\\The Game (in the prefix: {dest}; the folder "
                      "you chose)", out)
        self.assertIn("into C:\\Apps\\The Game;", out)  # what is skipped, in the plan's words
        self.assertIn("start it with:", out)
        self.assertIn("'C:\\Apps\\The Game\\bin\\Game.exe'", out)
        self.assertIn("Destination: C:\\Apps\\The Game", out)  # the timeline row
        logs = self.state / "gitfurl" / "logs"
        log = next(p for p in logs.glob("p1-*.log"))
        self.assertIn(f"destination: C:\\Apps\\The Game ({dest}) (the folder you chose)",
                      log.read_text())
        timeline = next(logs.glob("p1-*.timeline.txt")).read_text()
        self.assertIn("Destination: C:\\Apps\\The Game", timeline)
        self.assertIn("the folder you chose", timeline)

    def test_cleanup_keeps_its_guards(self):
        self.make_prefix()
        with no_process():
            code, out, err = run_cli("install", str(self.setup), "--prefix", "p1", "--yes",
                                     "--dest", self.DEST, "--cleanup", "installer")
        self.assertEqual(code, 0, err)
        self.assertIn("cleanup: deleted 1 file(s)", out)
        self.assertFalse(self.setup.exists())
        self.assertTrue((self.prefix / "drive_c/Apps/The Game/bin/Game.exe").is_file())

    def test_bad_destination_stops_any_route(self):
        self.make_prefix()
        for route in ("auto", "wine"):
            with no_process(), mock.patch("shutil.which", return_value="/x/wine"):
                code, out, err = run_cli("install", str(self.setup), "--prefix", "p1",
                                         "--dry-run", "--route", route, "--dest", "C:\\..")
            self.assertEqual(code, 1, route)
            self.assertIn("error: the destination may not contain '.' or '..'", err)
            self.assertIn("Nothing was run.", err)

    def test_wine_route_says_it_does_not_apply(self):
        self.make_prefix()
        with no_process(), mock.patch("shutil.which", return_value="/x/wine"):
            code, out, _ = run_cli("install", str(self.setup), "--prefix", "p1", "--dry-run",
                                   "--route", "wine", "--dest", self.DEST)
        self.assertEqual(code, 0)
        self.assertIn("--dest does not apply to the Wine/Proton route: Setup runs and picks "
                      "its own folder", out)


class WindowHelperTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.prefix = self.base / "prefixes" / "p1"
        self.drive_c = self.prefix / "drive_c"

    def test_mapping_between_a_picked_folder_and_the_windows_path(self):
        pick = gui.destination_from_pick
        d = self.drive_c
        self.assertEqual(pick(d, str(d / "Games"), "p1"), "C:\\Games\\p1")
        self.assertEqual(pick(d, str(d), "p1"), "C:\\p1")
        self.assertEqual(pick(d, str(d / "Apps" / "Studio"), "My Game"),
                         "C:\\Apps\\Studio\\My Game")
        self.assertEqual(pick(d, str(d / "Apps") + "/", "x"), "C:\\Apps\\x")
        self.assertEqual(pick(d, str(d / "Program Files"), "New"), "C:\\Program Files\\New")

    def test_refusal_outside_drive_c(self):
        pick = gui.destination_from_pick
        d = self.drive_c
        for outside in (self.prefix, self.base, "/", self.prefix / "drive_c2",
                        d / ".." / "dosdevices", self.prefix / "dosdevices" / "c:"):
            with self.assertRaises(DestinationRefused) as cm:
                pick(d, str(outside), "p1")
            self.assertIn("is not inside this prefix's drive C:", str(cm.exception))
            self.assertIn("gitfurl only unpacks into a folder there", str(cm.exception))
        with self.assertRaises(DestinationRefused) as cm:
            pick(d, str(d / "windows"), "p1")
        self.assertIn("C:\\windows (Wine's own folder)", str(cm.exception))
        with self.assertRaises(DestinationRefused):
            pick(d, str(d / "bad:name"), "p1")

    def test_last_part_and_browse_state(self):
        self.assertEqual(gui.destination_last_part("C:\\Apps\\The Game", "p1"), "The Game")
        self.assertEqual(gui.destination_last_part("not a path", "p1"), "p1")
        self.assertEqual(gui.destination_browse_state(self.prefix), (False, gui.DEST_NO_BROWSE))
        self.drive_c.mkdir(parents=True)
        self.assertEqual(gui.destination_browse_state(self.prefix), (True, gui.DEST_NOTE))
        linked = self.base / "linked"
        linked.mkdir()
        (linked / "drive_c").symlink_to(self.drive_c)
        self.assertFalse(gui.destination_browse_state(linked)[0])

    def test_browse_with_a_mocked_folder_dialog(self):
        ask = mock.Mock(return_value="")
        self.assertEqual(gui.pick_destination(self.prefix, "C:\\Games\\p1", "p1", ask),
                         (None, gui.DEST_NO_BROWSE))
        ask.assert_not_called()  # no prefix yet: no dialog
        self.drive_c.mkdir(parents=True)
        self.assertEqual(gui.pick_destination(self.prefix, "C:\\Games\\p1", "p1", ask),
                         (None, None))  # cancelled
        ask.assert_called_once_with(
            title="Choose the folder to unpack into (inside the prefix's drive C:)",
            initialdir=str(self.drive_c), mustexist=True)
        ask.return_value = str(self.drive_c / "Apps")
        self.assertEqual(gui.pick_destination(self.prefix, "C:\\Games\\Kept Name", "p1", ask),
                         ("C:\\Apps\\Kept Name", None))
        self.assertEqual(gui.pick_destination(self.prefix, "", "p1", ask),
                         ("C:\\Apps\\p1", None))
        ask.return_value = str(self.base)
        text, error = gui.pick_destination(self.prefix, "C:\\Games\\p1", "p1", ask)
        self.assertIsNone(text)
        self.assertIn("is not inside this prefix's drive C:", error)
        self.assertTrue(error.endswith("The destination was not changed."))


if __name__ == "__main__":
    unittest.main()
