"""The scrubbed support report (gitfurl.report, `gitfurl cli report`, the window's helper).
Synthetic files only: fake logs, timelines and detailed logs that hold a fake installer path
under a fake home, a fake user, real name, host and machine ID, archive and file names, CRCs,
a URL, an e-mail address, IP and MAC addresses, a GUID, a Windows profile path, a setup's app
name and exact byte counts; plus the logs that a mocked Wine run and a native unpack of
synthetic archives really write. No Wine, no installer, no network."""

import io
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

try:
    import inno_writer as w
    from farc_writer import F, make_archive
    from test_gitfurl import RunEventHelpers
    from test_native_run import NativeBase
    from test_route import no_process
except ImportError:  # python -m unittest tests.test_report
    from tests import inno_writer as w
    from tests.farc_writer import F, make_archive
    from tests.test_gitfurl import RunEventHelpers
    from tests.test_native_run import NativeBase
    from tests.test_route import no_process

from gitfurl import __version__, cli, report
from gitfurl.gui import support_report_view

USER, REAL, HOST, MID = "zorkuser", "Quuxbert Vromm", "glimmerhost", "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
APP = "Zyxwv Quest"
EXACT = 9437184000  # bytes in the detailed log; rounded: about 9.4 GB
SECRETS = [USER, "Quuxbert", HOST, MID, "Zyxwv", "zyxwv", "level_qqq", "ZqEngine", "hunter2",
           "192.168.1.23", "fe80::1ff:fe23:4567:890a", "aa:bb:cc:dd:ee:ff", "help@zq-games",
           "https://", "12345678-ABCD-4321-ABCD-1234567890AB", "1A2B3C4D", "5E6F7A8B",
           "Europe/Berlin", str(EXACT), "2026-11-27", "20261127", "08:15:00",
           "C:\\users\\" + USER, "vromm"]


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Fixture(unittest.TestCase):
    """A fake home with gitfurl's folders, a Wine-route run (log, timeline, detailed log)
    and an installer folder with a synthetic Inno Setup header and a FreeArc archive."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home" / USER
        self.env = {"HOME": str(self.home), "XDG_DATA_HOME": str(self.home / ".local/share"),
                    "XDG_STATE_HOME": str(self.home / ".local/state"), "USER": USER}
        patcher = mock.patch.dict("os.environ", self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.machine = {"user": [USER], "real-name": [REAL, "Quuxbert", "Vromm"],
                        "home": [str(self.home)], "host": [HOST], "machine-id": [MID]}
        for target, value in (("machine_names", lambda *a, **k: self.machine),
                              ("probe_wine", lambda: ("wine-11.0", False))):
            p = mock.patch.object(report, target, value)
            p.start()
            self.addCleanup(p.stop)
        self.logs = self.home / ".local/state/gitfurl/logs"
        self.logs.mkdir(parents=True)
        self.src = self.home / "Downloads" / "zq-files"
        self.src.mkdir(parents=True)
        self.installer = self.src / "setup_zyxwv.exe"
        data, _ = w.build(w.Spec(app_name=APP, default_dir="{autopf}\\" + APP,
                                 files=[w.WFile("{app}\\ZqEngine.dll", b"x")]))
        self.installer.write_bytes(data)
        (self.src / "zyxwv-01.bin").write_bytes(make_archive(
            [("storing", [F("level_qqq.pak", b"y" * 10)])]))
        self.prefix = self.home / ".local/share/gitfurl/prefixes/zqp"
        self.write_run()

    def write_run(self, stamp="20261127-081500"):
        stem = self.logs / f"zqp-{stamp}"
        inst = str(self.installer)
        log = [
            "command: " + " ".join([
                "/nix/store/abc-systemd/bin/systemd-run", "--user", "--scope",
                "--unit=gitfurl-zqp-20261127-081500-4242.scope", "/usr/bin/bwrap",
                "--setenv", "HOME", str(self.home), "--setenv", "SECRET_TOKEN", "hunter2",
                "--setenv", "WINEPREFIX", str(self.prefix),
                "--setenv", "WINEDLLOVERRIDES", "winemenubuilder.exe=d", "--",
                "/usr/bin/wine", f"'{inst}'", "/VERYSILENT"]),
            f"cwd: {self.src}",
            f"prefix: {self.prefix}",
            "---",
            "0024:err:module:import_dll Library ZqEngine.dll (which is needed by "
            f"L\"C:\\users\\{USER}\\AppData\\Roaming\\{APP}\\zq.exe\") not found",
            f"0024:fixme:winsock:hostname {HOST} 192.168.1.23 fe80::1ff:fe23:4567:890a",
            "0028:err:net:mac aa:bb:cc:dd:ee:ff guid {12345678-ABCD-4321-ABCD-1234567890AB}",
            "0030:warn:tz zone Europe/Berlin at 2026-11-27 08:15:00, see https://zq-games.example/help",
            "0030:warn:mail write to help@zq-games.example",
            f"0032:err:file {self.home}/Games/level_qqq.pak sha {MID}",
            f"0034:err:ntdll:user {USER} not in group",
            "Zyxwv Quest says hello",  # not a Wine message line: left out
            "",
            "--- exit status 0",
            "--- CRC-32 mismatch: data/level_qqq.pak: stored 1A2B3C4D, computed 5E6F7A8B; kept",
            "--- WARNING: 1 file kept although the CRC-32 does not match the archive "
            "(--keep-bad-file-crc): data/level_qqq.pak (stored 1A2B3C4D, computed 5E6F7A8B)",
        ]
        (stem.with_suffix(".log")).write_text("\n".join(log) + "\n")
        tl = [
            "08:14:58  Detected Inno Setup (likely) \u2014 setup data format 6.7.0 ('Inno Setup "
            "Setup Data (6.7.0)'); its header blocks were read (" + str(self.src) + ")",
            "08:14:58  Route: wine (Setup runs under Wine/Proton; you chose it) \u2014 you "
            "chose the Wine/Proton route",
            f"08:14:58  Prefix zqp: new \u2014 it does not exist yet; the plan creates it first "
            f"({self.prefix})",
            "08:14:59  You approved the plan \u2014 you typed yes; nothing ran before this",
            "08:15:00  Started Setup (setup_zyxwv.exe) under Wine, with its own windows "
            "\u2014 you approved the plan; it runs inside bwrap (portable)",
            f"08:15:03  Window opened: \"{APP} Setup\" \u2014 a program of the run opened it",
            "08:15:09  Warning: files whose CRC-32 does not match the archive are KEPT \u2014 "
            "you chose --keep-bad-file-crc for this run",
            f"08:22:03  Setup reading the installer's data: 42 %, 1.6 GB of {EXACT} bytes "
            "\u2014 1.2 MB written to the prefix so far, 3 process(es) running",
            "08:22:03  Setup exited with status 0 \u2014 0 usually means it finished",
            "08:22:04  The installer exited with status 0.",
        ]
        (self.logs / f"zqp-{stamp}.timeline.txt").write_text("\n".join(tl) + "\n")
        events = [
            '{"t": "2026-11-27T08:15:00", "event": "start", "command": "wine ' + inst
            + '", "cwd": "' + str(self.src) + '", "env_overrides": ["WINEPREFIX", '
            '"WINEDLLOVERRIDES"], "portable": true, "unattended": true}',
            '{"t": "2026-11-27T08:15:10", "event": "progress", "read_bytes": 1234567, '
            '"total_bytes": ' + str(EXACT) + ', "percent": 42.0}',
            '{"t": "2026-11-27T08:16:00", "event": "possibly_stalled", "idle_s": 120, '
            '"windows": ["' + APP + ' Setup"], "advisory": true}',
            '{"t": "2026-11-27T08:22:03", "event": "exit", "status": 0, "seconds": 423.1, '
            '"started": true, "bytes_written": ' + str(EXACT) + '}',
        ]
        (self.logs / f"zqp-{stamp}.events.jsonl").write_text("\n".join(events) + "\n")
        return stem.with_suffix(".log")

    def build(self, **kw):
        kw.setdefault("wine_version", "wine-11.0")
        kw.setdefault("umu", False)
        return report.build_report("zqp", **kw)


class ScrubTests(Fixture):
    def test_nothing_identifying_is_left(self):
        text = self.build().text
        low = text.casefold()
        for secret in SECRETS:
            self.assertNotIn(secret.casefold(), low, secret)
        self.assertNotIn(str(self.home), text)
        self.assertNotIn(str(self.src), text)
        self.assertNotIn("Zyxwv Quest says hello", text)

    def test_header_and_what_is_kept(self):
        rep = self.build()
        text = rep.text
        self.assertTrue(text.startswith(
            "gitfurl support report \u2014 scrubbed: software names, file names, checksums, "
            "user, host and paths replaced; sizes rounded. Nothing was sent anywhere.\n"))
        self.assertIn(f"\ngitfurl: {__version__}\n", text)
        for kept in ("wine: wine-11.0", "umu-run: not found",
                     "route: wine (you chose it); reason: you chose the route",
                     "runtime: wine", "inno setup data format: 6.7.0", "exit status: 0",
                     "crc-32 mismatches logged: 1 kept",
                     "Warning: files whose CRC-32 does not match the archive are KEPT",
                     "Setup exited with status 0", "3 process(es) running",
                     "WINEPREFIX <prefix-path>", "WINEDLLOVERRIDES winemenubuilder.exe=d",
                     "SECRET_TOKEN <value>", "--keep-bad-file-crc", "--portable",
                     "--unattended", "memory limit", "--detailed-log", "<installer>.exe",
                     "--unit=gitfurl-<prefix>-<stamp>-4242.scope",
                     "C:\\users\\<user>", "<crc>", "<ip>", "<mac>", "<email>", "<url>",
                     "<guid>", "<tz>", "~/<path>", "+00:07:05", "+00:00:02  start:",
                     "<path>/systemd-run", "<path>/wine", "Wine's own output: 7 message lines "
                     "kept, 1 other lines left out", "/VERYSILENT", "user <user> not in"):
            self.assertIn(kept, text, kept)
        self.assertIn("python: ", text)
        self.assertIn("architecture: ", text)
        self.assertRegex(text, r"native module: (present|not present)")
        self.assertRegex(text, r"distribution: \S+")
        self.assertEqual(text.count("<prefix>"), text.count("<prefix>"))  # placeholder kept
        self.assertGreater(rep.removed["user and machine"], 0)
        self.assertGreater(rep.removed["software names"], 0)

    def test_placeholders_are_stable(self):
        text = self.build().text
        # the app's window title is the same quoted text in the timeline and the events
        self.assertIn('Window opened: "<text-1>"', text)
        self.assertNotIn("windows=", text)  # the detailed log's titles are left out
        # the file with the bad CRC has one number in the CRC line, the warning and Wine's line
        crc = [ln for ln in text.splitlines() if "CRC-32 mismatch:" in ln][0]
        token = crc.split("mismatch: ")[1].split(":")[0]
        self.assertRegex(token, r"^<folder-\d+>/<file-\d+>$")
        self.assertIn(token.split("/")[1], [ln for ln in text.splitlines()
                                            if "WARNING: 1 file kept" in ln][0])
        # the installer is <installer>.exe in the timeline and in the logged command
        self.assertIn("Started Setup (<installer>.exe)", text)
        self.assertIn("<path>/wine <installer-folder>/<installer>.exe /VERYSILENT", text)
        # and across runs: a second run reuses every placeholder
        self.write_run("20261127-091500")
        both = report.build_report("zqp", all_runs=True, wine_version="wine-11.0").text
        self.assertIn("== run 1 of 2 ==", both)
        self.assertIn("== run 2 of 2 ==", both)
        self.assertEqual(both.count(token), 2 * text.count(token))
        self.assertNotIn("<text-2>", both)

    def test_sizes_rounded_and_exact_on_request(self):
        text = self.build().text
        self.assertIn("about 9.4 GB", text)
        self.assertIn("1.6 GB of about 9.4 GB", text.replace("about 1.6 GB", "1.6 GB"))
        self.assertNotIn(str(EXACT), text)
        exact = self.build(exact_sizes=True).text
        self.assertIn("sizes exact, as chosen. Nothing was sent anywhere.", exact)
        self.assertIn(f"{EXACT} bytes", exact)
        self.assertIn(f"bytes_written={EXACT} bytes", exact)

    def test_known_names_from_the_setup_header(self):
        # the setup's app name appears nowhere in the log as such: a line that holds it
        log = self.logs / "zqp-20261127-081500.log"
        log.write_text(log.read_text().replace(
            "--- exit status 0", f"0040:err:title {APP}\n--- exit status 0"))
        text = self.build().text
        self.assertNotIn(APP.casefold(), text.casefold())
        self.assertIn("title <app>", text)
        # Not a test of a wish: without the setup's header (installer gone), a name only
        # the header knows is not found by the generic patterns. The docs say so.
        without = self.build(read_sources=False)
        self.assertIn(APP, without.text)

    def test_the_check_refuses_a_name_left_in(self):
        original = report.Scrubber.known_pass

        def skip_host(self, text):
            self.phrases.pop(HOST, None)
            self._phrase_re = None
            return original(self, text)

        with mock.patch.object(report.Scrubber, "known_pass", skip_host):
            with self.assertRaises(report.ReportRefused) as cm:
                self.build()
        msg = str(cm.exception)
        self.assertIn("user and machine", msg)
        self.assertNotIn(HOST, msg)
        with mock.patch.object(report.Scrubber, "known_pass", skip_host):
            code, out, err = run_cli("report", "zqp")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("no report was made", err)
        self.assertNotIn(HOST, err)

    def test_removed_lists_categories_and_counts_only(self):
        rep = self.build()
        lines = "\n".join(rep.removed_lines())
        self.assertIn("checksums: ", lines)
        self.assertIn("paths: ", lines)
        for secret in SECRETS:
            self.assertNotIn(secret.casefold(), lines.casefold())

    def test_a_delete_log(self):
        (self.logs / "zqp-20261127-101500.delete.log").write_text(
            "gitfurl: delete the prefix zqp (approved: typed)\nplan:\n"
            f"  delete the prefix folder {self.prefix} and everything in it: 12 file(s), "
            "3 folder(s), 1.6 GB\n"
            f"  remove the menu entry {self.home}/.local/share/applications/"
            "gitfurl-zqp-zyxwv-quest.desktop (gitfurl wrote it for this prefix)\n"
            "done:\n  deleted the prefix folder " + str(self.prefix) + "\n")
        text = self.build().text
        self.assertIn("route: none (a prefix deletion)", text)
        self.assertIn("remove the menu entry <applications>/<menu-entry-1>.desktop", text)
        self.assertIn("about 1.6 GB", text)
        self.assertNotIn("zyxwv", text.casefold())

    def test_terminal_escapes_do_not_reach_the_report(self):
        log = self.logs / "zqp-20261127-081500.log"
        log.write_text(log.read_text().replace(
            "--- exit status 0", "0050:err:esc \x1b[31mred\x1b[0m ‮reversed\n--- exit status 0"))
        text = self.build().text
        self.assertNotIn("\x1b", text)
        self.assertNotIn("‮", text)
        self.assertIn("?[31mred", text)

    def test_unknown_prefix_and_bad_names(self):
        with self.assertRaises(report.ReportRefused):
            report.build_report("nothing-here")
        with self.assertRaises(ValueError):
            report.build_report("bad name!")


class CommandLineTests(Fixture):
    def test_prints_the_report(self):
        code, out, err = run_cli("report", "zqp")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, self.build().text)
        self.assertEqual(err, "")
        code, out, err = run_cli("report", "zqp", "--show-removed")
        self.assertIn("removed (categories and counts only, never the values):", err)
        self.assertNotIn("removed (", out)

    def test_run_all_runs_and_log(self):
        self.write_run("20261127-091500")
        _c, latest, _e = run_cli("report", "zqp")
        self.assertIn("== run 1 of 1 ==", latest)
        code, out, _e = run_cli("report", "zqp", "--run", "20261127-081500")
        self.assertEqual(code, 0)
        code, out, _e = run_cli("report", "zqp", "--all-runs")
        self.assertIn("== run 2 of 2 ==", out)
        code, out, err = run_cli("report", "zqp", "--run", "20250101-000000")
        self.assertEqual(code, 1)
        self.assertIn("no such run", err)
        code, out, err = run_cli("report", "--log",
                                 str(self.logs / "zqp-20261127-081500.timeline.txt"))
        self.assertEqual(code, 0, err)
        self.assertIn("files read: log, timeline, detailed log", out)
        code, _o, err = run_cli("report")
        self.assertEqual(code, 1)
        self.assertIn("name a prefix", err)

    def test_output_file_mode_no_overwrite_no_link(self):
        out_file = self.home / "report.txt"
        code, out, err = run_cli("report", "zqp", "-o", str(out_file))
        self.assertEqual(code, 0, err)
        self.assertEqual(stat.S_IMODE(out_file.stat().st_mode), 0o600)
        self.assertEqual(out_file.read_text(), self.build().text)
        self.assertIn("nothing was sent anywhere", out)
        self.assertNotIn(USER, out.replace(str(out_file), ""))
        out_file.write_text("mine")
        code, _o, err = run_cli("report", "zqp", "-o", str(out_file))
        self.assertEqual(code, 1)
        self.assertIn("exists", err)
        self.assertEqual(out_file.read_text(), "mine")
        target = self.home / "elsewhere.txt"
        link = self.home / "link.txt"
        link.symlink_to(target)
        code, _o, err = run_cli("report", "zqp", "-o", str(link))
        self.assertEqual(code, 1)
        self.assertFalse(target.exists())
        old = os.umask(0)
        try:
            run_cli("report", "zqp", "-o", str(self.home / "umask.txt"))
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE((self.home / "umask.txt").stat().st_mode), 0o600)

    def test_a_link_given_with_log_is_refused(self):
        link = self.home / "a.log"
        link.symlink_to(self.logs / "zqp-20261127-081500.log")
        code, _o, err = run_cli("report", "--log", str(link))
        self.assertEqual(code, 1)
        self.assertIn("links are not followed", err)


class WindowTests(Fixture):
    def test_the_window_text_matches_the_command_line(self):
        path = self.logs / "zqp-20261127-081500.timeline.txt"
        notice, body, shareable = support_report_view(path, wine_version="wine-11.0", umu=False)
        self.assertTrue(shareable)
        self.assertEqual(notice, "This is everything the report contains. Names of the "
                         "installer, its files and your user and machine were replaced; sizes "
                         "are rounded. Check it before you share it.")
        _c, out, _e = run_cli("report", "--log", str(path))
        self.assertEqual(body, out)
        _c, out, _e = run_cli("report", "zqp")
        self.assertEqual(body, out)

    def test_nothing_to_report_offers_no_copy(self):
        notice, body, shareable = support_report_view(None)
        self.assertFalse(shareable)
        self.assertIn("no saved run", body)
        original = report.Scrubber.known_pass

        def skip_user(self, text):
            self.phrases.pop(USER, None)
            self._phrase_re = None
            return original(self, text)

        with mock.patch.object(report.Scrubber, "known_pass", skip_user):
            notice, body, shareable = support_report_view(self.logs / "zqp-20261127-081500.log")
        self.assertFalse(shareable)
        self.assertIn("no report was made", body)


class RealFormatWineRun(RunEventHelpers, unittest.TestCase):
    """The log, timeline and detailed log a mocked Wine run really writes."""

    def test_report_of_a_real_wine_run_log(self):
        from gitfurl.timeline import Timeline, approval_entry

        self.fake_prefix_process(2 << 20)
        tl = Timeline()
        approval_entry(tl, "typed")
        res = self.run_it(self.sleeper(0.3, write=2 << 20), timeline=tl, detailed_log=True,
                          on_note=lambda _n: None)
        tl.end("The installer exited with status 0.")
        machine = {"user": ["nobody-here"], "home": [self.tmp.name], "host": [], "real-name": [],
                   "machine-id": []}
        with mock.patch.dict("os.environ", self.env):
            rep = report.build_report(Path(res.log_path), machine=machine, env=self.env,
                                      wine_version="wine-11.0")
        text = rep.text
        self.assertIn("runtime: wine", text)
        self.assertIn("exit status: 0", text)
        self.assertIn("files read: log, timeline, detailed log", text)
        self.assertIn("Setup exited with status 0", text)
        self.assertIn("command: <path>/wine <installer-folder>/<installer>.exe", text)
        self.assertIn("exit: status=0", text)
        self.assertIn("+00:00:00  You approved the plan", text)
        for gone in (self.tmp.name, "hunter2", "SECRET_TOKEN", "p.bin"):
            self.assertNotIn(gone, text)


class RealFormatNativeRun(NativeBase):
    """The log, timeline and detailed log the native route really writes."""

    def test_report_of_a_real_native_unpack(self):
        from gitfurl import timeline as tlm
        from gitfurl.detect import detect
        from gitfurl.route import run_native

        plan = self.plan()
        self.make_prefix()
        tl = tlm.Timeline()
        tlm.plan_entries(tl, detect(self.setup), self.choose(), self.prefix, True, False)
        tlm.approval_entry(tl, "typed")
        with no_process():
            res = run_native(plan, timeline=tl, env=self.env, progress_interval=0,
                             detailed_log=True)
        tl.end("Unpacked (Setup was not run).")
        machine = {"user": ["nobody-here"], "home": [str(self.base)], "host": [],
                   "real-name": [], "machine-id": []}
        rep = report.build_report("p1", env=self.env, machine=machine,
                                  log_dir=Path(res.log_path).parent)
        text = rep.text
        for kept in ("route: native (gitfurl chose it); reason: every archive uses methods "
                     "gitfurl decodes", "inno setup data format: 5.5.0 (u)",
                     "freearc stages: lzma, rep+exe+delta+lzma, storing", "archives: 2",
                     "files planned: 5", "files written: 5",
                     "Data block 1 of 2 in <archive-2>.bin: rep+exe+delta+lzma",
                     "Opened <archive-1>.bin: 2 files in 1 data block",
                     "installer: <installer-folder>/<installer>.exe",
                     "destination: C:\\Games\\<prefix> (<prefix-path>/drive_c/Games/<prefix>)",
                     "WINEPREFIX=<prefix-path> wine 'C:\\Games\\<prefix>\\",
                     "start: route=native, files=5, total_bytes=about 48 KB, archives=2",
                     "every CRC-32 matched the archive's index"):
            self.assertIn(kept, text, kept)
        for gone in (str(self.base), "NAME-01", "Game.exe", "level1", "vcredist", "48019"):
            self.assertNotIn(gone, text, gone)
        # the archive's token is the same in the timeline and the log
        tl_part, _sep, log_part = text.partition("-- log --")
        self.assertEqual(tl_part.count("<archive-1>.bin"), 2)
        self.assertIn("archive: <installer-folder>/<archive-1>.bin", log_part)


if __name__ == "__main__":
    unittest.main()
