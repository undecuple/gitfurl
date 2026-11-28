"""Setup's own questions (gitfurl.inno.choices, gitfurl.setup_choices): read from synthetic Inno
Setup installers built in memory by tests/inno_writer.py, asked with a mocked terminal, and
applied per route (Inno's switches with --unattended; [Files], [Icons] and [Registry] entries
on the native route). No Wine and no real installer: the launcher is mocked, and the native
route writes only into a temporary folder."""

import contextlib
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

try:
    import inno_writer as w
    from farc_writer import F, make_archive
except ImportError:  # python -m unittest tests.test_setup_choices
    from tests import inno_writer as w
    from tests.farc_writer import F, make_archive

from gitfurl import cli, inno
from gitfurl import setup_choices as sc
from gitfurl import setup_entries as se
from gitfurl.detect import detect
from gitfurl.route import choose_route

HERE = Path(__file__).resolve().parent


def spec(**kw):
    """Two languages, three setup types (one custom), a fixed component, a parent with an
    exclusive group of children, tasks with a group heading, an unchecked task and one that
    needs a component, and a postinstall entry."""
    kw.setdefault("languages", [("english", 0x0409), ("german", 0x0407)])
    kw.setdefault("types", [("full", 0, False), ("compact", 0, False), ("custom", 0, True)])
    kw.setdefault("components", [
        ("main", "full,compact,custom", 1, "Game files"),
        ("music", "full", 0, "Music"),
        ("voices", "full,custom", 0, "Voice packs"),
        ("voices\\en", "full,custom", 8, "English voices"),
        ("voices\\de", "", 8, "German voices")])
    kw.setdefault("tasks", [
        ("desktopicon", 0, "Create a desktop icon", "Additional icons:"),
        ("quicklaunch", 2, "Create a Quick Launch icon", "Additional icons:"),
        ("musicfix", 0, "Fix the music volume", "", "music")])
    kw.setdefault("files", [
        w.WFile("{app}\\game.exe", b"MZ game", components="main"),
        w.WFile("{app}\\music.dat", b"m" * 3000, components="music"),
        w.WFile("{app}\\en.dat", b"e", components="voices\\en"),
        w.WFile("{app}\\de.dat", b"d", components="voices\\de"),
        w.WFile("{app}\\quick.txt", b"q", tasks="quicklaunch")])
    kw.setdefault("run", [w.WRun("{app}\\game.exe", flags=("post-install", "skip if silent"),
                                 wait=1, description="Start the game now")])
    return w.Spec(**kw)


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def patched_runs(side_effect=None):
    run = mock.Mock(return_value=subprocess.CompletedProcess([], 0), side_effect=side_effect)
    with mock.patch("subprocess.run", run), mock.patch("gitfurl.prefixes.run_in_group", run), \
            mock.patch("gitfurl.runner._run_in_group", run), \
            mock.patch("gitfurl.runner.RunControl.started", new_callable=mock.PropertyMock,
                       return_value=True):
        yield run


@contextlib.contextmanager
def terminal(answers):
    """A terminal on stdin that answers `answers` in order (then end of input)."""
    it = iter(answers)

    def fake_input(prompt=""):
        print(prompt, end="")
        try:
            got = next(it)
        except StopIteration:
            raise EOFError from None
        print(got)
        return got

    with mock.patch.object(sys.stdin, "isatty", return_value=True), \
            mock.patch("builtins.input", fake_input):
        yield


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.src = self.base / "src"
        self.src.mkdir()
        self.setup = self.src / "setup.exe"
        self.data, self.state = self.base / "data", self.base / "state"
        self.env = {"XDG_DATA_HOME": str(self.data), "XDG_STATE_HOME": str(self.state),
                    "HOME": str(self.base)}
        patcher = mock.patch.dict("os.environ", self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.prefix = self.data / "gitfurl" / "prefixes" / "p1"

    def write(self, sp=None):
        data, _ = w.build(sp or spec())
        self.setup.write_bytes(data)
        return inno.read_setup(self.setup)

    def choices(self, sp=None):
        return inno.choices(self.write(sp))


class ChoicesTests(Base):
    def test_defaults_from_types_and_flags(self):
        ch = self.choices()
        d = ch.defaults()
        self.assertEqual((d.language, d.setup_type), ("english", "full"))
        self.assertEqual(d.components, {"main", "music", "voices", "voices\\en"})
        self.assertEqual(d.tasks, {"desktopicon", "musicfix"})  # quicklaunch is unchecked
        by = {i.name: i for i in ch.components}
        self.assertTrue(by["main"].fixed and by["main"].default)
        self.assertTrue(by["voices\\de"].exclusive and not by["voices\\de"].default)
        self.assertEqual(by["music"].size, 3000)
        self.assertEqual(ch.tasks[0].group, "Additional icons:")
        self.assertEqual([x.display for x in ch.languages], ["English", "German"])
        compact = ch.answer(setup_type="compact")
        self.assertEqual(compact.components, {"main"})
        self.assertEqual(compact.tasks, {"desktopicon"})  # musicfix needs the music component
        self.assertTrue(compact.answered and not d.answered)

    def test_exclusive_groups(self):
        ch = self.choices()
        c = ch.answer(components=["voices\\de"])  # picks the custom type
        self.assertEqual(c.setup_type, "custom")
        self.assertIn("voices\\de", c.components)
        self.assertNotIn("voices\\en", c.components)
        with self.assertRaisesRegex(ValueError, "only one of these components"):
            ch.answer(components=["voices\\en", "voices\\de"])
        # turning the parent off turns its children off; a child turns its parent on
        self.assertEqual(ch.answer(components=["!voices"]).components, {"main"})
        self.assertIn("voices", ch.answer(components=["!voices", "voices\\de"]).components)
        # exclusive tasks: the first checked one is the default
        sp = spec(tasks=[("low", 1), ("high", 1), ("none", 3)])
        ch = self.choices(sp)
        self.assertEqual(ch.defaults().tasks, {"low"})
        self.assertEqual(ch.answer(tasks=["high"]).tasks, {"high"})

    def test_names_are_checked(self):
        ch = self.choices()
        for kw, part in (
                ({"components": ["sound"]}, "unknown component 'sound'; this setup's "
                                            "components: main, music, voices"),
                ({"tasks": ["!desk"]}, "unknown task 'desk'; this setup's tasks: desktopicon"),
                ({"language": "french"}, "this setup's languages: english, german"),
                ({"setup_type": "huge"}, "this setup's setup types: full, compact, custom"),
                ({"components": ["!main"]}, "is fixed"),
                ({"setup_type": "full", "components": ["music"]}, "needs the custom type"),
                ({"setup_type": "compact", "tasks": ["musicfix"]}, "not offered")):
            with self.assertRaisesRegex(ValueError, part.replace("(", r"\(")) as cm:
                ch.answer(**kw)
            self.assertIn(part, str(cm.exception))
        nocustom = self.choices(spec(types=[("full", 0, False), ("compact", 0, False)]))
        with self.assertRaisesRegex(ValueError, "no custom setup type"):
            nocustom.answer(components=["music"])
        one = self.choices(spec(languages=[("english", 0x0409)]))
        with self.assertRaisesRegex(ValueError, "only one language"):
            one.answer(language="english")

    def test_summary_and_switches(self):
        ch = self.choices()
        c = ch.answer(language="German", components=["voices\\de", "!music"],
                      tasks=["quicklaunch", "!desktopicon"])
        self.assertEqual(ch.summary(c), "choices: type custom; components main, voices, "
                         "voices\\de; tasks quicklaunch (not desktopicon); language German")
        args, notes = ch.switches(c)
        self.assertEqual(args, ["/LANG=german", "/TYPE=custom",
                                "/COMPONENTS=main,voices,voices\\de",
                                "/TASKS=!desktopicon,quicklaunch"])
        self.assertEqual(notes, [])
        args, notes = ch.switches(ch.answer())
        self.assertEqual(args, ["/LANG=english", "/TYPE=full",
                                "/TASKS=desktopicon,!quicklaunch,musicfix"])
        self.assertIn("the setup type picks them", notes[0])
        odd = self.choices(spec(types=[], components=[("a,b", "", 0)], tasks=[]))
        args, notes = odd.switches(odd.answer())
        self.assertFalse(any(a.startswith("/COMPONENTS") for a in args))
        self.assertIn("/COMPONENTS is not passed", notes[0])

    def test_what_is_not_seen_is_said(self):
        sp = spec(tasks=[("once", 8), ("checked", 0, "", "", "", "IsWine")])
        ch = self.choices(sp)
        text = "\n".join(ch.lines())
        self.assertIn("not seen: the setup may ask other questions in its own code; gitfurl "
                      "cannot see or answer those", text)
        self.assertIn("checkedonce tasks are checked, as for a first install", text)
        self.assertIn("depends on a [Code] check gitfurl cannot evaluate; not asked", text)
        self.assertIn("[x] Start the game now ({app}\\game.exe)", text)
        self.assertIn("install folder: {autopf}\\Test App (DefaultDirName)", text)
        self.assertNotIn("checked", ch.defaults().tasks)

    def test_a_setup_without_questions(self):
        ch = self.choices(spec(languages=[("english", 0x0409)], types=[], components=[],
                               tasks=[]))
        self.assertFalse(ch.asks)
        self.assertIsNone(sc.pick(self.setup, sc.MODE_SWITCHES))
        with self.assertRaisesRegex(ValueError, "asks for no language"):
            sc.pick(self.setup, sc.MODE_SWITCHES, answers={"tasks": ["x"]})


class AskTests(Base):
    def ask(self, func, answers, *args):
        out: list[str] = []
        it = iter(answers)

        def answer(prompt):
            out.append(prompt)
            try:
                return next(it)
            except StopIteration:
                raise EOFError from None

        return func(*args, input_fn=answer, out=out.append), out

    def test_yes_no_with_the_default_in_capitals_and_asked_again(self):
        got, out = self.ask(sc.yes_no, ["maybe", "", ], "Task x?", True)
        self.assertTrue(got)
        self.assertEqual(out[0], "Task x? [Y/n] ")
        self.assertIn("Please answer y or n", out[1])
        self.assertEqual(self.ask(sc.yes_no, ["NO"], "q", True)[0], False)
        got, out = self.ask(sc.yes_no, ["yes"], "Task y?", False)
        self.assertTrue(got)
        self.assertEqual(out[0], "Task y? [y/N] ")
        self.assertFalse(self.ask(sc.yes_no, [""], "q", False)[0])

    def test_numbered_choice_is_asked_again(self):
        got, out = self.ask(sc.number, ["0", "9", "x", "2"], "Language", ["a", "b"], 0)
        self.assertEqual(got, 1)
        self.assertEqual(sum("Please enter a number from 1 to 2" in o for o in out), 3)
        self.assertEqual(self.ask(sc.number, [""], "T", ["a", "b", "c"], 2)[0], 2)

    def test_every_question_in_the_wizard_order(self):
        ch = self.choices()
        # language 2, type 3 (custom), music: Enter (no for custom), voices: y, voices group:
        # 2 (German), desktop icon: n, quick launch: y
        got, out = self.ask(sc.ask, ["2", "3", "", "y", "2", "n", "y"], ch)
        self.assertEqual(ch.summary(got), "choices: type custom; components main, voices, "
                         "voices\\de; tasks quicklaunch (not desktopicon); language German")
        text = "\n".join(out)
        self.assertIn("Component Game files (main): fixed, always installed", text)
        self.assertIn("Component Music (music)? [y/N] ", text)
        self.assertIn("Additional icons:", text)
        self.assertLess(text.index("Language"), text.index("Setup type"))
        self.assertNotIn("musicfix", text)  # not offered without the music component

    def test_children_of_a_no_are_not_asked(self):
        ch = self.choices()
        got, out = self.ask(sc.ask, ["", "3", "", "n", "y", ""], ch)
        self.assertEqual(got.components, {"main"})
        self.assertNotIn("English voices", "\n".join(out))

    def test_end_of_input_stops(self):
        with self.assertRaises(sc.ChoicesStopped):
            self.ask(sc.ask, ["1"], self.choices())


class CliTests(Base):
    def setUp(self):
        super().setUp()
        self.write()

    def test_dry_run_prints_the_questions_and_defaults(self):
        with patched_runs() as run:
            code, out, _ = run_cli("install", str(self.setup), "--prefix", "p1", "--dry-run",
                                   "--unattended", "--no-memory-limit")
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertIn("Setup's choices: the questions, with the script's defaults:", out)
        self.assertIn("  language: English (english) [default]; German (german)", out)
        self.assertIn("(o) English voices (voices\\en) - exclusive", out)
        self.assertIn("/TYPE=full '/TASKS=desktopicon,!quicklaunch,musicfix'", out)
        self.assertIn("Setup's choices (the script's defaults; a real run asks)", out)
        self.assertIn("postinstall: Start the game now: does not run in a silent install", out)

    def test_wizard_route_shows_the_questions_for_information(self):
        code, out, _ = run_cli("install", str(self.setup), "--prefix", "p1", "--dry-run",
                               "--no-memory-limit")
        self.assertEqual(code, 0)
        self.assertIn("for information only: Setup shows its own wizard", out)
        self.assertNotIn("/TASKS", out)
        self.assertIn("postinstall: Start the game now: Setup offers it at the end", out)

    def real_run(self, *extra, answers=None):
        (self.prefix / "drive_c").mkdir(parents=True, exist_ok=True)
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, 0)

        ctx = terminal(answers) if answers is not None else contextlib.nullcontext()
        with mock.patch("shutil.which", return_value="/x/wine"), patched_runs(fake_run), ctx:
            code, out, err = run_cli("install", str(self.setup), "--prefix", "p1",
                                     "--unattended", "--no-memory-limit", *extra)
        return code, out, err, seen.get("argv")

    def test_yes_takes_the_defaults_and_records_them(self):
        code, out, _, argv = self.real_run("--yes", "--timeline")
        self.assertEqual(code, 0)
        self.assertIn("/TYPE=full", argv)
        self.assertIn("/TASKS=desktopicon,!quicklaunch,musicfix", argv)
        self.assertIn("taken without asking: --yes", out)
        record = ("choices: type full; components main, music, voices, voices\\en; tasks "
                  "desktopicon, musicfix (not quicklaunch); language English")
        self.assertIn(record, out)  # the plan shown before the approval
        logs = self.state / "gitfurl" / "logs"
        log = next(p for p in logs.glob("p1-*.log")).read_text()
        self.assertIn(record, log)
        timeline = next(logs.glob("p1-*.timeline.txt")).read_text()
        self.assertIn("Setup's choices: answered", timeline)
        self.assertIn(record, timeline)

    def test_no_terminal_takes_the_defaults(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=False), \
                mock.patch("builtins.input", side_effect=AssertionError("asked")):
            code, out, _, argv = self.real_run()
        self.assertEqual(code, 2)  # the approval itself needs a terminal or --yes
        self.assertIsNone(argv)
        self.assertIn("taken without asking: no terminal to ask on", out)
        self.assertIn("Not approved: no terminal to ask on", out)

    def test_a_terminal_is_asked_and_the_answers_reach_the_command_line(self):
        code, out, _, argv = self.real_run(answers=["2", "3", "", "y", "2", "n", "y", "yes"])
        self.assertEqual(code, 0, out)
        self.assertIn("/LANG=german", argv)
        self.assertIn("/COMPONENTS=main,voices,voices\\de", argv)
        self.assertIn("/TASKS=!desktopicon,quicklaunch", argv)
        self.assertLess(out.index("Setup's choices (Enter keeps"), out.index("About to run"))
        self.assertIn("Setup's choices (asked)", out)

    def test_options_answer_and_are_checked(self):
        code, out, _, argv = self.real_run("--yes", "--type", "custom", "--components",
                                           "!music,voices\\de", "--tasks", "quicklaunch",
                                           "--language", "GERMAN")
        self.assertEqual(code, 0, out)
        self.assertIn("/COMPONENTS=main,voices,voices\\de", argv)
        self.assertIn("/TASKS=desktopicon,quicklaunch", argv)
        self.assertIn("Setup's choices (given on the command line)", out)
        for opt, part in ((["--components", "sound"], "this setup's components: main"),
                          (["--tasks", "x"], "this setup's tasks: desktopicon"),
                          (["--type", "full", "--components", "music"], "custom type")):
            code, _o, err, argv = self.real_run("--yes", *opt)
            self.assertEqual(code, 1)
            self.assertIsNone(argv)
            self.assertIn(part, err)
            self.assertIn("Nothing was run.", err)


class NativeTests(Base):
    """[Files], [Icons] and [Registry] entries kept or skipped by the answers."""

    def test_extract_inno_files_follow_the_answers(self):
        self.write()
        (self.prefix / "drive_c").mkdir(parents=True)
        code, out, err = run_cli("extract-inno", str(self.setup), "--prefix", "p1", "--dry-run",
                                 "--type", "custom", "--components", "voices\\de")
        self.assertEqual(code, 0, err)
        self.assertIn("{app}\\music.dat: not part of the chosen components", out)
        self.assertIn("{app}\\quick.txt: not part of the chosen tasks", out)
        self.assertIn("[Files] 5 (2 kept, 3 left out)", out)
        code, out, err = run_cli("extract-inno", str(self.setup), "--prefix", "p1", "--yes",
                                 "--type", "custom", "--components", "voices\\de",
                                 "--tasks", "quicklaunch")
        self.assertEqual(code, 0, err + out)
        dest = self.prefix / "drive_c" / "Games" / "p1"
        self.assertEqual(sorted(p.name for p in dest.iterdir()),
                         ["de.dat", "game.exe", "quick.txt"])
        self.assertIn("postinstall: Start the game now: would be offered by Setup; not run by "
                      "gitfurl", out)

    def native(self, comps=(), archive="music-01.bin", code=()):
        regs = [w.WReg("HKCU", "Software\\Test", "Music", "1", components="music"),
                w.WReg("HKCU", "Software\\Test", "Icon", "1", tasks="desktopicon")]
        icons = [w.WIcon("{group}\\Play", "{app}\\Game.exe"),
                 w.WIcon("{commondesktop}\\Edit", "{app}\\Editor.exe", tasks="desktopicon")]
        self.write(spec(registry=regs, icons=icons, files=[],
                        code=w.code_blob(list(code)) if code else b"",
                        **({"components": list(comps)} if comps else {})))
        (self.src / archive).write_bytes(make_archive(
            [("storing", [F("Game.exe", b"MZ" + bytes(300)),
                          F("Editor.exe", b"MZ" + bytes(200))])], ("",)))
        (self.prefix / "drive_c" / "users" / "tester").mkdir(parents=True)
        (self.prefix / "system.reg").write_text("WINE REGISTRY Version 2\n\n#arch=win64\n")
        (self.prefix / "user.reg").write_text("WINE REGISTRY Version 2\n\n#arch=win64\n")
        choice = choose_route(self.setup, "p1", detect(self.setup), "native", self.env)
        self.assertEqual(choice.route, "native", choice.line())
        return choice

    def test_icons_and_registry_follow_the_answers(self):
        choice = self.native()
        ch = inno.choices(inno.read_setup(self.setup))
        off = se.plan_setup_entries(self.setup, choice.extract, self.env,
                                    choice=ch.answer(setup_type="compact",
                                                     tasks=["!desktopicon"]))
        names = [s.name for s in off.shortcuts]
        self.assertEqual(len(names), 1, off.skipped)
        self.assertTrue(any("not part of the chosen tasks" in s for s in off.skipped), off.skipped)
        self.assertEqual([v.name for v in off.values], [])
        on = se.plan_setup_entries(self.setup, choice.extract, self.env, choice=ch.answer())
        self.assertEqual(len(on.shortcuts), 2, on.skipped)
        self.assertEqual(sorted(v.name for v in on.values), ["Icon", "Music"])
        self.assertIn("part of your choices", on.values[0].notes[0])
        # without answers (the old behaviour): every conditional shortcut is skipped
        plain = se.plan_setup_entries(self.setup, choice.extract, self.env)
        self.assertEqual(len(plain.shortcuts), 1)

    def test_cli_native_plan_says_what_is_and_is_not_applied(self):
        self.native()
        code, out, err = run_cli("install", str(self.setup), "--prefix", "p1", "--dry-run",
                                 "--route", "native", "--apply-setup-entries")
        self.assertEqual(code, 0, err)
        self.assertIn("the answers decide which of the setup's registry entries and shortcuts",
                      out)
        self.assertIn("[Icons] 1 (1 kept, 0 left out), [Registry] 2 (2 kept, 0 left out)", out)
        self.assertIn("the components look tied to the archives (component music and "
                      "music-01.bin); gitfurl unpacks every archive; it cannot tell which "
                      "files belong to which component", out)
        self.assertIn("postinstall: Start the game now: would be offered by Setup; not run by "
                      "gitfurl", out)
        code, out, _ = run_cli("install", str(self.setup), "--prefix", "p1", "--dry-run",
                               "--route", "native")
        self.assertIn("for information only: on this route the answers change nothing", out)
        self.assertIn("gitfurl unpacks every archive; it cannot tell", out)

    def test_code_strings_that_name_components_and_archives(self):
        self.native(comps=[("base", "full,compact,custom", 1), ("extra", "full", 0)],
                    archive="data-01.bin", code=["extra", "{src}\\data-01.bin"])
        picked = sc.pick(self.setup, sc.MODE_NATIVE)
        note = sc.native_notes(picked, ["data-01.bin"])[0]
        self.assertIn("its [Code] names components (extra) and archives (data-01.bin)", note)
        self.assertTrue(note.endswith(sc.FREEARC_NOTE))

    def test_native_log_records_the_answers(self):
        from gitfurl.route import NativeInstallPlan, run_native

        choice = self.native()
        picked = sc.pick(self.setup, sc.MODE_ENTRIES, answers={"tasks": ["!desktopicon"]})
        plan = NativeInstallPlan(self.setup, "p1", self.prefix, choice, False, choices=picked)
        res = run_native(plan, env=self.env)
        self.assertEqual(res.status, 0, res.lines)
        self.assertIn("choices: type full; components main, music, voices, voices\\en; tasks "
                      "musicfix (not desktopicon, quicklaunch); language English (given on "
                      "the command line; applied)", res.log_path.read_text())


class WindowTests(Base):
    def test_rows_start_at_the_defaults(self):
        ch = self.choices()
        rows = {r.key: r for r in sc.window_rows(ch)}
        self.assertEqual(rows["language"].value, "english")
        self.assertEqual(rows["type"].value, "full")
        self.assertTrue(rows["c:music"].value)
        self.assertFalse(rows["c:music"].enabled)  # the full type decides the components
        self.assertEqual(rows["g:c:voices"].value, "voices\\en")
        self.assertEqual(rows["g:c:voices"].indent, 1)
        self.assertFalse(rows["t:quicklaunch"].value)
        self.assertTrue(rows["t:quicklaunch"].enabled)
        self.assertTrue(any(r.text.startswith("Not seen: the setup may ask") for r in rows.values()))

    def test_values_become_a_choice(self):
        ch = self.choices()
        c = sc.window_choice(ch, {"type": "custom", "c:music": False, "g:c:voices": "voices\\de",
                                  "t:desktopicon": False, "language": "german"})
        self.assertEqual(ch.summary(c), "choices: type custom; components main, voices, "
                         "voices\\de; tasks none (not desktopicon, quicklaunch); language German")
        rows = {r.key: r for r in sc.window_rows(ch, c)}
        self.assertTrue(rows["c:music"].enabled)
        self.assertNotIn("t:musicfix", [k for k, r in rows.items() if r.enabled])
        # a non-custom type ignores the component boxes
        self.assertEqual(sc.window_choice(ch, {"type": "compact", "c:music": True}).components,
                         {"main"})
        self.assertEqual(sc.window_mode(True, False, False), sc.MODE_NATIVE)
        self.assertEqual(sc.window_mode(False, False, True), sc.MODE_SWITCHES)

    def test_window_under_xvfb(self):
        if shutil.which("xvfb-run") is None:
            self.skipTest("xvfb-run not available")
        try:
            import tkinter  # noqa: F401
        except ImportError:
            self.skipTest("tkinter not installed")
        self.write()
        other = self.base / "other" / "setup.exe"
        other.parent.mkdir()
        shutil.copy(self.setup, other)
        env = {**os.environ, "PYTHONPATH": f"{HERE.parent / 'src'}:{HERE}",
               "HOME": str(self.base), "XDG_CONFIG_HOME": str(self.base / "c")}
        env.pop("PROTONPATH", None)
        proc = subprocess.Popen(["xvfb-run", "-a", sys.executable,
                                 str(HERE / "gui_choices_driver.py"), str(self.setup),
                                 str(other)],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        try:
            out, err = proc.communicate(timeout=90)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)  # its own process group only
            proc.communicate()
            self.fail("the window driver did not finish")
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertIn("OK choices", out)


if __name__ == "__main__":
    unittest.main()
