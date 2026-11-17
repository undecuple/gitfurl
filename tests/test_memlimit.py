"""Memory limit (systemd user scope). Fakes only: a fake systemd-run and systemctl on a temporary
PATH that record their arguments, fake /proc/meminfo data and a fake cgroup tree. No real
systemd unit is created, no Wine, no installer, no network."""

import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from gitfurl import cli, memlimit
from gitfurl.memlimit import (
    GIB,
    MIB,
    Availability,
    MemoryLimitError,
    MemoryScope,
    availability,
    compute_budget,
    next_high,
    read_meminfo,
)
from gitfurl.runner import RunControl, RunOptions, build_plan, describe_result, execute

UID = os.getuid()
MANAGER_CG = f"/user.slice/user-{UID}.slice/user@{UID}.service"
SLEEP = shutil.which("sleep")


def script(path: Path, body: str) -> str:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def meminfo(path: Path, total: int, available: int) -> str:
    path.write_text(f"MemTotal:       {total // 1024} kB\nMemFree:        1 kB\n"
                    f"MemAvailable:   {available // 1024} kB\n")
    return str(path)


class Fakes:
    """A temporary PATH with fake systemd-run and systemctl, a runtime dir and a cgroup tree.

    systemd-run records its argv, prints a version for --version, writes the scope's
    memory.events (from EVENTS_FILE, if present) and then execs the command after "--", so
    the command keeps its process id and process group exactly like the real --scope mode.
    systemctl records every argv line and answers show, set-property and reset-failed.
    """

    def __init__(self, root: Path, version: int = 262, controllers: str = "cpu memory pids"):
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        self.cg = root / "cgroup"
        (self.cg / MANAGER_CG.lstrip("/") / "app.slice").mkdir(parents=True)
        (self.cg / "cgroup.controllers").write_text("cpu io memory pids\n")
        (self.cg / MANAGER_CG.lstrip("/") / "cgroup.controllers").write_text(controllers + "\n")
        self.runtime = root / "run"
        (self.runtime / "systemd").mkdir(parents=True)
        (self.runtime / "systemd" / "private").write_text("")
        self.run_rec = root / "systemd-run.args"
        self.ctl_rec = root / "systemctl.args"
        self.events = root / "events.template"
        self.state = root / "unit.state"  # what `show -p ActiveState -p Result` prints
        self.state.write_text("ActiveState=inactive\nResult=success\n")
        self.fail_start = root / "fail.start"  # if present: systemd-run prints it and exits 1
        app = f"{self.cg}{MANAGER_CG}/app.slice"
        self.systemd_run = script(self.bin / "systemd-run", f'''
if [ "$1" = "--version" ]; then echo "systemd {version} ({version})"; exit 0; fi
printf "%s\\n" "$@" > "{self.run_rec}"
if [ -f "{self.fail_start}" ]; then cat "{self.fail_start}" >&2; exit 1; fi
unit=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --unit=*) unit="${{1#--unit=}}" ;;
    --) shift; break ;;
  esac
  shift
done
mkdir -p "{app}/$unit"
if [ -f "{self.events}" ]; then cat "{self.events}" > "{app}/$unit/memory.events"; fi
echo 123456789 > "{app}/$unit/memory.current"
exec "$@"
''')
        self.systemctl = script(self.bin / "systemctl", f'''
echo "$*" >> "{self.ctl_rec}"
case "$*" in
  "--user show -p ControlGroup --value") echo "{MANAGER_CG}" ;;
  "--user show -p ControlGroup "*) echo "ControlGroup={MANAGER_CG}/app.slice/${{5}}" ;;
  "--user show -p ActiveState -p Result "*) cat "{self.state}" ;;
  "--user set-property --runtime "*) ;;
  "--user reset-failed "*) ;;
  *) exit 1 ;;
esac
''')
        self.env = {"PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                    "XDG_RUNTIME_DIR": str(self.runtime), "HOME": str(root / "home"),
                    "XDG_STATE_HOME": str(root / "state"),
                    "XDG_DATA_HOME": str(root / "data")}
        (root / "home").mkdir()

    def avail(self, version=262) -> Availability:
        a = availability(self.env, str(self.cg))
        return replace(a, version=version, manager_cgroup=MANAGER_CG)

    def ctl_lines(self) -> list[str]:
        return self.ctl_rec.read_text().splitlines() if self.ctl_rec.exists() else []


class BudgetTests(unittest.TestCase):
    def test_reserve_is_the_larger_of_1_gib_and_10_percent(self):
        b = compute_budget(32 * GIB, 20 * GIB)
        self.assertEqual(b.reserve, int(3.2 * GIB))
        self.assertEqual(b.max_bytes, (20 * GIB - b.reserve) // MIB * MIB)
        self.assertEqual(b.high_bytes, int(b.max_bytes * 0.9) // MIB * MIB)
        small = compute_budget(4 * GIB, 3 * GIB)
        self.assertEqual(small.reserve, GIB)
        self.assertEqual(small.max_bytes, 2 * GIB)
        self.assertLess(small.high_bytes, small.max_bytes)

    def test_refuses_below_the_floor_with_the_numbers(self):
        with self.assertRaises(MemoryLimitError) as ctx:
            compute_budget(8 * GIB, int(1.5 * GIB))
        text = str(ctx.exception)
        self.assertIn("1.50 GiB", text)  # available
        self.assertIn("1.00 GiB", text)  # reserve and floor
        self.assertIn("Free at least 0.50 GiB", text)
        compute_budget(8 * GIB, 2 * GIB)  # exactly the floor is allowed

    def test_meminfo_parsing(self):
        with tempfile.TemporaryDirectory() as d:
            p = meminfo(Path(d) / "m", 16 * GIB, 9 * GIB)
            self.assertEqual(read_meminfo(p), (16 * GIB, 9 * GIB))
            Path(p).write_text("MemTotal: 5 kB\n")
            self.assertIsNone(read_meminfo(p))
        self.assertIsNone(read_meminfo("/nonexistent/meminfo"))

    def test_plan_lines_name_every_value_and_the_reason(self):
        with tempfile.TemporaryDirectory() as d:
            f = Fakes(Path(d))
            lines, problem = memlimit.describe(True, f.avail(), meminfo(Path(d) / "m", 16 * GIB,
                                                                          10 * GIB))
        self.assertIsNone(problem)
        text = "\n".join(lines)
        b = compute_budget(16 * GIB, 10 * GIB)
        for needle in (f"MemoryMax={b.max_bytes}", f"MemoryHigh={b.high_bytes}",
                       "kept free for the rest of the system", "10% of MemTotal",
                       "swap: not limited", "does not guarantee", "slowed down",
                       "stops processes in this run only", "--expand-environment=no",
                       "--scope", "never raised"):
            self.assertIn(needle, text)
        self.assertNotIn("cannot", text.lower().replace("cannot be", ""))


class AdjustRuleTests(unittest.TestCase):
    b = compute_budget(16 * GIB, 12 * GIB)  # reserve 1.6 GiB, high about 9.36 GiB

    def test_lowered_by_the_shortfall_from_current_usage(self):
        usage = 6 * GIB
        new = next_high(self.b.high_bytes, self.b, self.b.reserve - 512 * MIB, usage)
        self.assertEqual(new, (usage - 512 * MIB) // MIB * MIB)

    def test_never_below_the_floor(self):
        new = next_high(self.b.high_bytes, self.b, 100 * MIB, 2 * GIB)
        self.assertEqual(new, self.b.high_floor)
        self.assertEqual(self.b.high_floor, GIB)
        self.assertIsNone(next_high(self.b.high_floor, self.b, 100 * MIB, 2 * GIB))

    def test_raised_by_the_surplus_and_never_above_the_start(self):
        cur = 3 * GIB
        new = next_high(cur, self.b, self.b.reserve + 256 * MIB + GIB, None)
        self.assertEqual(new, cur + GIB)
        self.assertEqual(next_high(cur, self.b, 15 * GIB, None), self.b.high_bytes)
        self.assertIsNone(next_high(self.b.high_bytes, self.b, 15 * GIB, None))

    def test_small_changes_and_the_dead_band_do_nothing(self):
        self.assertIsNone(next_high(3 * GIB, self.b, self.b.reserve + 100 * MIB, None))
        self.assertIsNone(next_high(3 * GIB, self.b, self.b.reserve - 10 * MIB, 3 * GIB))


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.t = Path(self._t.name)
        self.f = Fakes(self.t)

    def tearDown(self):
        self._t.cleanup()

    def check(self, env=None):
        return availability(env or self.f.env, str(self.f.cg))

    def test_available_with_everything_in_place(self):
        a = self.check()
        self.assertTrue(a.ok, a.reason)
        self.assertEqual(a.systemd_run, self.f.systemd_run)

    def test_each_missing_piece_has_a_reason(self):
        empty = self.t / "empty"
        empty.mkdir()
        self.assertIn("systemd-run was not found",
                      self.check(dict(self.f.env, PATH=str(empty))).reason)
        self.assertIn("XDG_RUNTIME_DIR", self.check({**self.f.env, "XDG_RUNTIME_DIR": ""}).reason)
        (self.f.runtime / "systemd" / "private").unlink()
        self.assertIn("no user systemd manager", self.check().reason)

    def test_memory_controller_not_delegated(self):
        (self.f.cg / MANAGER_CG.lstrip("/") / "cgroup.controllers").write_text("cpu pids\n")
        a = self.check()
        self.assertFalse(a.ok)
        self.assertIn("memory controller is not delegated", a.reason)

    def test_no_cgroup_v2(self):
        (self.f.cg / "cgroup.controllers").unlink()
        self.assertIn("cgroup v2", self.check().reason)

    def test_verify_asks_systemd_and_reads_the_version(self):
        a = memlimit.verify(self.check(), self.f.env, cgroup_root=str(self.f.cg))
        self.assertEqual(a.version, 262)
        self.assertEqual(a.manager_cgroup, MANAGER_CG)

    def test_doctor_reports_availability(self):
        from gitfurl.doctor import format_report, memory_limit_report

        with mock.patch("gitfurl.memlimit.availability",
                        return_value=Availability(False, "systemd-run was not found on PATH")):
            text = memory_limit_report()
        self.assertIn("unavailable (systemd-run was not found on PATH)", text)
        self.assertIn("--no-memory-limit", text)
        r = mock.Mock(problem=None, version="v", path="p")
        r.name = "x"
        out = format_report({"platform": "p", "wine": r, "bwrap": r, "umu": r, "innoextract": r,
                             "tkinter": r, "memory_limit": text})
        self.assertIn("memory limit: unavailable", out)


class WrapTests(unittest.TestCase):
    def scope(self, version=262):
        b = compute_budget(16 * GIB, 10 * GIB)
        a = Availability(True, "ok", "/usr/bin/systemd-run", "/usr/bin/systemctl", version, "/x")
        return MemoryScope(a, b, "gitfurl-p1-20261117-101010-42.scope"), b

    def test_exact_argv(self):
        s, b = self.scope()
        self.assertEqual(s.wrap(["/usr/bin/wine", "/i/setup.exe"]), [
            "/usr/bin/systemd-run", "--user", "--scope", "--quiet",
            "--unit=gitfurl-p1-20261117-101010-42.scope", "--expand-environment=no",
            "-p", f"MemoryHigh={b.high_bytes}", "-p", f"MemoryMax={b.max_bytes}",
            "--", "/usr/bin/wine", "/i/setup.exe"])

    def test_old_systemd_refuses_a_dollar_and_otherwise_omits_the_flag(self):
        s, _b = self.scope(version=249)
        self.assertNotIn("--expand-environment=no", s.wrap(["/usr/bin/wine", "/i/a.exe"]))
        with self.assertRaises(MemoryLimitError):
            s.wrap(["/usr/bin/wine", "/i/$HOME.exe"])

    def test_unit_name(self):
        name = memlimit.unit_name("my-prefix_1", "wineboot")
        self.assertTrue(name.startswith("gitfurl-my-prefix_1-wineboot-"))
        self.assertTrue(name.endswith(f"-{os.getpid()}.scope"))


class ExecuteTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.t = Path(self._t.name)
        self.f = Fakes(self.t)
        self.meminfo = meminfo(self.t / "meminfo", 16 * GIB, 10 * GIB)
        self.wine_rec = self.t / "wine.args"
        self.wine = script(self.f.bin / "wine",
                           f'printf "%s\\n" "$@" > "{self.wine_rec}"\nexit 0\n')
        installer = self.t / "inst" / "setup.exe"
        installer.parent.mkdir()
        installer.write_bytes(b"")
        self.installer = installer
        self.plan = replace(build_plan(installer, "p1", env=self.f.env, memory_limit=True),
                            argv=[self.wine, str(installer)])
        self.plan.prefix.mkdir(parents=True)

    def tearDown(self):
        self._t.cleanup()

    def options(self, **kw):
        return RunOptions(cgroup_root=str(self.f.cg), meminfo=self.meminfo, **kw)

    def test_wine_argv_is_wrapped_and_the_result_reported(self):
        self.f.events.write_text("low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n")
        res = execute(self.plan, self.wine, self.f.env, options=self.options(detailed_log=True))
        self.assertEqual(res.status, 0)
        got = self.f.run_rec.read_text().splitlines()
        self.assertEqual(got[:3], ["--user", "--scope", "--quiet"])
        self.assertTrue(got[3].startswith("--unit=gitfurl-p1-"))
        self.assertEqual(got[4], "--expand-environment=no")
        b = compute_budget(16 * GIB, 10 * GIB)
        self.assertEqual(got[5:9], ["-p", f"MemoryHigh={b.high_bytes}", "-p",
                                    f"MemoryMax={b.max_bytes}"])
        self.assertEqual(got[9:], ["--", self.wine, str(self.installer)])
        self.assertEqual(self.wine_rec.read_text().splitlines(), [str(self.installer)])
        self.assertIsNotNone(res.memory)
        self.assertFalse(res.memory.hit_limit)
        self.assertIn("no out-of-memory stop was seen", describe_result(res))
        log = Path(res.log_path).read_text()
        self.assertIn("systemd-run", log.splitlines()[0])
        kinds = [json.loads(x)["event"] for x in
                 Path(res.log_path).with_suffix(".events.jsonl").read_text().splitlines()]
        self.assertIn("memory_limit", kinds)
        self.assertIn("memory_limit_result", kinds)
        unit = got[3].removeprefix("--unit=")
        for line in self.f.ctl_lines():  # only this unit was asked about or touched
            self.assertTrue(line.endswith(unit) or line == "--user show -p ControlGroup --value",
                            line)

    def test_oom_kill_from_memory_events_is_reported_in_plain_words(self):
        self.f.events.write_text("low 0\nhigh 7\nmax 3\noom 1\noom_kill 2\noom_group_kill 0\n")
        self.f.state.write_text("ActiveState=failed\nResult=oom-kill\n")
        res = execute(self.plan, self.wine, self.f.env, options=self.options())
        rep = res.memory
        self.assertTrue(rep.hit_limit)
        self.assertEqual((rep.oom_kill, rep.high), (2, 7))
        text = describe_result(res)
        self.assertIn("hit its hard memory limit", text)
        self.assertIn("the kernel stopped one or more processes inside this run's scope "
                      "(2 process(es)", text)
        self.assertNotIn("protected", text)
        self.assertIn("retry without it, use --no-memory-limit, or turn the option off in the "
                      "window", text)
        self.assertIn("slowed down at the soft limit", text)
        self.assertNotIn("cannot", text)
        unit = next(x for x in self.f.run_rec.read_text().splitlines()
                    if x.startswith("--unit="))
        self.assertIn(f"--user reset-failed {unit.removeprefix('--unit=')}", self.f.ctl_lines())

    def test_systemd_result_alone_is_enough_to_report(self):
        self.f.state.write_text("ActiveState=failed\nResult=oom-kill\n")
        res = execute(self.plan, self.wine, self.f.env, options=self.options())
        self.assertTrue(res.memory.hit_limit)
        self.assertNotIn("process(es) by", describe_result(res))

    def test_unreadable_memory_events_is_not_reported_as_no_stop(self):
        # the fake writes no memory.events: the scope's result can not be checked
        res = execute(self.plan, self.wine, self.f.env, options=self.options())
        self.assertFalse(res.memory.final_read)
        text = describe_result(res)
        self.assertIn("memory.events could not be read after the run", text)
        self.assertIn("an out-of-memory stop could not be checked", text)
        self.assertNotIn("no out-of-memory stop was seen", text)

    def test_a_failed_systemd_run_start_is_named(self):
        self.f.fail_start.write_text("Failed to start transient scope unit: Access denied\n")
        res = execute(self.plan, self.wine, self.f.env, options=self.options())
        self.assertEqual(res.status, 1)
        self.assertFalse(self.wine_rec.exists())  # the installer never ran
        self.assertEqual(res.memory.start_failure,
                         "Failed to start transient scope unit: Access denied")
        text = describe_result(res)
        self.assertIn("systemd-run may have failed, so the installer may not have run", text)
        self.assertIn("'Failed to start transient scope unit: Access denied'", text)
        self.assertNotIn("no out-of-memory stop was seen", text)

    def test_a_plain_non_zero_exit_is_not_called_a_systemd_run_failure(self):
        self.f.events.write_text("high 0\noom_kill 0\n")
        script(self.f.bin / "wine", 'echo "wine: some installer output"\nexit 3\n')
        res = execute(self.plan, self.wine, self.f.env, options=self.options())
        self.assertEqual(res.status, 3)
        self.assertIsNone(res.memory.start_failure)
        self.assertNotIn("systemd-run may have failed", describe_result(res))

    def test_the_scope_is_finished_even_when_progress_stop_raises(self):
        self.f.state.write_text("ActiveState=failed\nResult=oom-kill\n")
        with mock.patch("gitfurl.runner.ProgressMonitor.stop", side_effect=RuntimeError("boom")), \
                self.assertRaises(RuntimeError):
            execute(self.plan, self.wine, self.f.env,
                    options=self.options(on_progress=lambda _s: None))
        self.assertTrue(any(x.startswith("--user reset-failed gitfurl-p1-")
                            for x in self.f.ctl_lines()), self.f.ctl_lines())

    def test_unavailable_refuses_before_anything_runs(self):
        (self.f.cg / MANAGER_CG.lstrip("/") / "cgroup.controllers").write_text("cpu\n")
        with self.assertRaises(MemoryLimitError) as ctx:
            execute(self.plan, self.wine, self.f.env, options=self.options())
        self.assertIn("NOT available", str(ctx.exception))
        self.assertFalse(self.f.run_rec.exists())
        self.assertFalse(self.wine_rec.exists())
        self.assertEqual(list((self.t / "state").glob("**/*.log")), [])

    def test_too_little_memory_refuses_before_anything_runs(self):
        low = meminfo(self.t / "low", 8 * GIB, int(1.2 * GIB))
        with self.assertRaises(MemoryLimitError) as ctx:
            execute(self.plan, self.wine, self.f.env,
                    options=RunOptions(cgroup_root=str(self.f.cg), meminfo=low))
        self.assertIn("Free at least", str(ctx.exception))
        self.assertFalse(self.wine_rec.exists())

    def test_off_leaves_the_argv_unchanged(self):
        seen = {}

        def fake(argv, **kw):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0)

        res = execute(replace(self.plan, memory_limit=False), self.wine, self.f.env, run=fake,
                      options=self.options())
        self.assertEqual(seen["argv"], [self.wine, str(self.installer)])
        self.assertIsNone(res.memory)
        self.assertFalse(self.f.run_rec.exists())
        self.assertEqual(self.f.ctl_lines(), [])

    def test_portable_wraps_the_bwrap_argv(self):
        bwrap_rec = self.t / "bwrap.args"
        bwrap = script(self.f.bin / "bwrap", f'printf "%s\\n" "$@" > "{bwrap_rec}"\nexit 0\n')
        plan = replace(build_plan(self.installer, "p1", env=self.f.env, portable=True,
                                  memory_limit=True), argv=[self.wine, str(self.installer)])
        env = dict(self.f.env, DISPLAY="")
        res = execute(plan, self.wine, env, options=self.options())
        self.assertEqual(res.status, 0)
        got = self.f.run_rec.read_text().splitlines()
        self.assertEqual(got[got.index("--") + 1], bwrap)
        inner = bwrap_rec.read_text().splitlines()
        self.assertEqual(inner[inner.index("--") + 1:], [self.wine, str(self.installer)])

    def test_proton_wraps_umu_run(self):
        proton = self.t / "proton-dir"
        proton.mkdir()
        (proton / "proton").write_text("")
        umu = script(self.f.bin / "umu-run", f'printf "%s\\n" "$@" > "{self.wine_rec}"\n')
        plan = build_plan(self.installer, "p1", env=self.f.env, runtime="proton",
                          proton_path=str(proton), memory_limit=True)
        res = execute(plan, umu, self.f.env, options=self.options())
        self.assertEqual(res.status, 0)
        got = self.f.run_rec.read_text().splitlines()
        self.assertEqual(got[got.index("--") + 1:], [umu, str(self.installer)])

    def _sleeping_wine(self):
        return script(self.f.bin / "wine", f'echo $$ > "{self.wine_rec}"\nexec {SLEEP} 60\n')

    def _wait_started(self):
        deadline = time.monotonic() + 20
        while not self.wine_rec.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.wine_rec.exists())
        time.sleep(0.1)

    def test_stop_is_unchanged_the_command_keeps_the_group(self):
        wine = self._sleeping_wine()
        plan = replace(self.plan, argv=[wine, str(self.installer)])
        control = RunControl(grace_s=2)
        box = {}
        seen_pids = []
        real_popen = subprocess.Popen

        def popen(*a, **kw):
            p = real_popen(*a, **kw)
            if kw.get("start_new_session"):  # the run's leader (not the systemd probes)
                seen_pids.append(p.pid)
            return p

        def go():
            with mock.patch("subprocess.Popen", side_effect=popen):
                box["r"] = execute(plan, wine, self.f.env, options=self.options(control=control))

        th = threading.Thread(target=go)
        th.start()
        self._wait_started()
        # the fake systemd-run exec'd the command: same process, so the same group gitfurl owns
        self.assertEqual(int(self.wine_rec.read_text()), seen_pids[0])
        t0 = time.monotonic()
        control.request_stop()
        th.join(30)
        self.assertFalse(th.is_alive())
        self.assertLess(time.monotonic() - t0, 10)
        self.assertTrue(control.cancelled)
        self.assertEqual(box["r"].status, 130)
        self.assertIsNotNone(box["r"].memory)

    def test_ctrl_c_is_unchanged(self):
        if threading.current_thread() is not threading.main_thread():
            self.skipTest("needs the main thread")
        wine = self._sleeping_wine()
        plan = replace(self.plan, argv=[wine, str(self.installer)])
        control = RunControl(grace_s=2)

        def interrupt():
            deadline = time.monotonic() + 20
            while not self.wine_rec.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            time.sleep(0.2)
            os.kill(os.getpid(), signal.SIGINT)  # our own process only

        threading.Thread(target=interrupt, daemon=True).start()
        res = execute(plan, wine, self.f.env, options=self.options(control=control))
        self.assertEqual(res.status, 130)
        self.assertTrue(control.cancelled)
        self.assertIn("cancelled by user", Path(res.log_path).read_text())


class FinishTests(unittest.TestCase):
    def _scope(self):
        avail = Availability(True, "", "/x/systemd-run", "/x/systemctl", 262, MANAGER_CG)
        return MemoryScope(avail, compute_budget(16 * GIB, 10 * GIB), "gitfurl-p1-t-1.scope",
                           env={}, settle_s=5.0)

    def test_ctrl_c_while_waiting_still_reads_the_result_and_resets_the_unit(self):
        scope = self._scope()
        calls = []
        shows = iter([KeyboardInterrupt(), {"ActiveState": "failed", "Result": "oom-kill"}])

        def show(*_props):
            item = next(shows)
            if isinstance(item, BaseException):
                raise item
            return item

        with mock.patch.object(scope, "_find_cgroup", return_value=None), \
                mock.patch.object(scope, "_show", side_effect=show), \
                mock.patch.object(scope, "_systemctl", side_effect=lambda *a: calls.append(a)), \
                self.assertRaises(KeyboardInterrupt):
            scope.finish()
        self.assertTrue(scope.report.oom_result)
        self.assertEqual(calls, [("reset-failed", "gitfurl-p1-t-1.scope")])

    def test_a_still_active_scope_names_the_reset_command(self):
        scope = replace_settle(self._scope())
        with mock.patch.object(scope, "_find_cgroup", return_value=None), \
                mock.patch.object(scope, "_show", return_value={"ActiveState": "active"}):
            rep = scope.finish()
        self.assertIn("systemctl --user reset-failed gitfurl-p1-t-1.scope", rep.notes[-1])

    def test_find_start_failure(self):
        self.assertIsNone(memlimit.find_start_failure("wine: hello\nFailed: no\n"))
        self.assertEqual(memlimit.find_start_failure("x\nFailed to find executable /a: No such "
                                                     "file or directory\n"),
                         "Failed to find executable /a: No such file or directory")


def replace_settle(scope):
    scope.settle_s = 0.0
    return scope


class DynamicTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.t = Path(self._t.name)
        self.f = Fakes(self.t)
        self.unit = "gitfurl-p1-20261117-101010-42.scope"
        scope_dir = self.f.cg / MANAGER_CG.lstrip("/") / "app.slice" / self.unit
        scope_dir.mkdir()
        (scope_dir / "memory.current").write_text(str(6 * GIB))
        (scope_dir / "memory.events").write_text("high 0\noom_kill 0\n")
        self.mem = self.t / "meminfo"
        self.budget = compute_budget(20 * GIB, 12 * GIB)  # reserve 2 GiB, soft limit 9 GiB
        self.notes, self.changes = [], []
        self.scope = MemoryScope(self.f.avail(), self.budget, self.unit, self.f.env,
                                 cgroup_root=str(self.f.cg), meminfo=str(self.mem),
                                 on_note=self.notes.append,
                                 on_change=lambda *a: self.changes.append(a), settle_s=0)

    def tearDown(self):
        self._t.cleanup()

    def tick(self, available):
        meminfo(self.mem, 20 * GIB, available)
        self.scope.tick()

    def set_calls(self):
        return [x for x in self.f.ctl_lines() if " set-property " in x]

    def test_lowered_then_raised_with_clamping_on_one_unit_only(self):
        b = self.budget
        self.tick(b.reserve - GIB)  # the rest of the system grew: 1 GiB short
        self.assertEqual(self.scope.high, 5 * GIB)
        scope_dir = self.f.cg / MANAGER_CG.lstrip("/") / "app.slice" / self.unit
        (scope_dir / "memory.current").write_text(str(2 * GIB))
        self.tick(100 * MIB)  # far short: clamped at the floor
        self.assertEqual(self.scope.high, b.high_floor)
        self.tick(b.reserve + 256 * MIB + 2 * GIB)  # memory free again: raised by the surplus
        self.assertEqual(self.scope.high, b.high_floor + 2 * GIB)
        self.tick(15 * GIB)  # plenty: back to the start value, never above
        self.assertEqual(self.scope.high, b.high_bytes)
        self.tick(15 * GIB)
        self.assertEqual(self.scope.high, b.high_bytes)
        calls = self.set_calls()
        self.assertEqual(calls, [
            f"--user set-property --runtime {self.unit} MemoryHigh={5 * GIB}",
            f"--user set-property --runtime {self.unit} MemoryHigh={b.high_floor}",
            f"--user set-property --runtime {self.unit} MemoryHigh={b.high_floor + 2 * GIB}",
            f"--user set-property --runtime {self.unit} MemoryHigh={b.high_bytes}"])
        for line in self.f.ctl_lines():  # every call names this one unit, and no other
            self.assertEqual([w for w in line.split() if w.endswith(".scope")], [self.unit])
            self.assertNotIn("MemoryMax", line)
        self.assertEqual(len(self.changes), 4)
        self.assertIn("lowered", self.notes[0])
        self.assertIn("nothing is stopped", self.notes[0])
        self.assertIn("raised", self.notes[2])
        self.assertEqual(len(self.scope.report.changes), 4)

    def test_no_change_while_enough_is_free(self):
        self.tick(self.budget.reserve + 100 * MIB)
        self.assertEqual(self.set_calls(), [])

    def test_a_failing_set_property_is_noted_once_and_the_value_kept(self):
        script(self.f.bin / "systemctl", f'echo "$*" >> "{self.f.ctl_rec}"\nexit 1\n')
        self.tick(100 * MIB)
        self.tick(100 * MIB)
        self.assertEqual(self.scope.high, self.budget.high_bytes)
        self.assertEqual(len(self.scope.report.notes), 1)

    def test_the_thread_uses_a_timed_wait_and_stops(self):
        self.scope.interval = 0.05
        meminfo(self.mem, 20 * GIB, self.budget.reserve - GIB)
        self.scope.start()
        deadline = time.monotonic() + 10
        while not self.set_calls() and time.monotonic() < deadline:
            time.sleep(0.02)
        report = self.scope.finish()
        self.assertTrue(self.set_calls())
        self.assertFalse(self.scope._thread.is_alive())
        self.assertFalse(report.hit_limit)


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.t = Path(self._t.name)
        self.f = self.t / "setup.exe"
        self.f.write_bytes(b"")
        self.data = self.t / "data"
        (self.data / "gitfurl" / "prefixes" / "p1").mkdir(parents=True)
        self.env = {"XDG_DATA_HOME": str(self.data), "XDG_STATE_HOME": str(self.t / "state")}

    def tearDown(self):
        self._t.cleanup()

    def test_unavailable_refuses_with_exit_2_and_names_the_flag(self):
        none = Availability(False, "systemd-run was not found on PATH")
        with mock.patch.dict("os.environ", self.env), \
                mock.patch("shutil.which", return_value="/x/wine"), \
                mock.patch("gitfurl.memlimit.availability", return_value=none), \
                mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen:
            code, out, err = run_cli("install", str(self.f), "--prefix", "p1", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("memory limit: NOT available: systemd-run was not found on PATH", out)
        self.assertIn("--no-memory-limit", err)
        self.assertIn("Nothing was run", err)
        run.assert_not_called()
        popen.assert_not_called()
        self.assertFalse((self.t / "state").exists())

    def test_no_memory_limit_runs_the_plain_argv(self):
        none = Availability(False, "systemd-run was not found on PATH")
        done = subprocess.CompletedProcess([], 0)
        with mock.patch.dict("os.environ", self.env), \
                mock.patch("shutil.which", return_value="/x/wine"), \
                mock.patch("gitfurl.memlimit.availability", return_value=none), \
                mock.patch("gitfurl.runner._run_in_group", return_value=done) as run:
            code, out, _ = run_cli("install", str(self.f), "--prefix", "p1", "--yes",
                                   "--no-memory-limit")
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0], ["/x/wine", str(self.f.resolve())])
        self.assertIn("memory limit: off", out)

    def test_dry_run_shows_the_values_and_starts_nothing(self):
        ok = Availability(True, "ok", "/usr/bin/systemd-run", "/usr/bin/systemctl")
        mem = meminfo(self.t / "m", 16 * GIB, 10 * GIB)
        real = memlimit.describe
        with mock.patch.dict("os.environ", self.env), \
                mock.patch("gitfurl.memlimit.availability", return_value=ok), \
                mock.patch("gitfurl.memlimit.describe",
                           side_effect=lambda e, a, m="": real(e, a, mem)), \
                mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen:
            code, out, _ = run_cli("install", str(self.f), "--dry-run", "--prefix", "p1")
        self.assertEqual(code, 0)
        run.assert_not_called()
        popen.assert_not_called()
        b = compute_budget(16 * GIB, 10 * GIB)
        self.assertIn("memory limit: on", out)
        self.assertIn(f"MemoryMax={b.max_bytes}", out)
        self.assertIn(f"MemoryHigh={b.high_bytes}", out)
        self.assertIn("/usr/bin/systemd-run --user --scope", out)

    def test_dry_run_says_not_available(self):
        none = Availability(False, "no user systemd manager is running")
        with mock.patch.dict("os.environ", self.env), \
                mock.patch("gitfurl.memlimit.availability", return_value=none):
            code, out, _ = run_cli("install", str(self.f), "--dry-run", "--prefix", "p1")
        self.assertEqual(code, 0)
        self.assertIn("memory limit: NOT available: no user systemd manager is running", out)

    def test_approval_text_shows_the_values(self):
        ok = Availability(True, "ok", "/usr/bin/systemd-run", "/usr/bin/systemctl")
        mem = meminfo(self.t / "m", 16 * GIB, 10 * GIB)
        real = memlimit.describe
        with mock.patch.dict("os.environ", self.env), \
                mock.patch("shutil.which", return_value="/x/wine"), \
                mock.patch("gitfurl.memlimit.availability", return_value=ok), \
                mock.patch("gitfurl.memlimit.describe",
                           side_effect=lambda e, a, m="": real(e, a, mem)), \
                mock.patch("sys.stdin.isatty", return_value=False), \
                mock.patch("subprocess.Popen") as popen:
            code, out, _ = run_cli("install", str(self.f), "--prefix", "p1")
        self.assertEqual(code, 2)  # not approved
        popen.assert_not_called()
        b = compute_budget(16 * GIB, 10 * GIB)
        self.assertLess(out.index(f"MemoryMax={b.max_bytes}"), out.index("Not approved"))
        self.assertIn("stops processes in this run only", out)


class PrefixCreationTests(unittest.TestCase):
    def test_wineboot_is_wrapped_too(self):
        from gitfurl.runner import run_with_prefix

        with tempfile.TemporaryDirectory() as d:
            t = Path(d)
            f = Fakes(t)
            mem = meminfo(t / "m", 16 * GIB, 10 * GIB)
            inst = t / "setup.exe"
            inst.write_bytes(b"")
            plan = build_plan(inst, "fresh", env=f.env, memory_limit=True)
            argvs = []

            def fake(argv, **kw):
                argvs.append(argv)
                return subprocess.CompletedProcess(argv, 0)

            res = run_with_prefix(plan, "/x/wine", True, f.env, run=fake,
                                  options=RunOptions(cgroup_root=str(f.cg), meminfo=mem))
        self.assertEqual(res.status, 0)
        self.assertTrue(argvs[0][4].startswith("--unit=gitfurl-fresh-wineboot-"))
        self.assertEqual(argvs[0][argvs[0].index("--") + 1:], ["/x/wine", "wineboot", "--init"])
        self.assertEqual(argvs[1][argvs[1].index("--") + 1:], ["/x/wine", str(inst)])
        self.assertNotEqual(argvs[0][4], argvs[1][4])


class GuiTests(unittest.TestCase):
    def test_memory_problem_and_checklist(self):
        from gitfurl.gui import MEMORY_TITLE, memory_problem, plan_checklist, run_reason

        none = Availability(False, "systemd-run was not found on PATH")
        self.assertIsNone(memory_problem(False, none))
        why = memory_problem(True, none)
        self.assertIn("NOT available: systemd-run was not found on PATH", why)
        self.assertIn(f"Turn off '{MEMORY_TITLE}'", why)
        rows = plan_checklist(None, None, True, True, "wine", True, None, why)
        self.assertEqual(rows[-1], ("memory", False, why))
        self.assertTrue(run_reason(rows, busy=False).startswith("Run is off: memory limit"))
        with tempfile.TemporaryDirectory() as d:
            ok = Availability(True, "ok", "/usr/bin/systemd-run", "/usr/bin/systemctl")
            self.assertIsNone(memory_problem(True, ok, meminfo(Path(d) / "m", 16 * GIB, 9 * GIB)))
            low = memory_problem(True, ok, meminfo(Path(d) / "m", 16 * GIB, GIB))
            self.assertIn("Free at least", low)


class WindowTests(unittest.TestCase):
    def test_default_on_and_run_off_with_reason_when_unavailable(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            from test_gui_polish import run_window
        finally:
            sys.path.pop(0)
        r = run_window('''
            import gitfurl.gui as g
            from gitfurl.memlimit import Availability
            root.focus_force()
            ui["show"]("details")
            ui["installer_var"].set(os.environ["TEST_INSTALLER"])
            ui["wine_path"]["value"] = "/fake/wine"
            ui["set_banners"](False)
            result["default_on"] = ui["memory_var"].get()
            g.memory_availability = lambda *a, **k: Availability(False, "test: no systemd-run")
            ui["refresh_plan"](); settle(root)
            result["run_off"] = ui["run_btn"]._state
            result["reason"] = ui["reason_var"].get()
            result["row"] = ui["check_rows"]["memory"][2].cget("text")
            result["plan"] = ui["plan_box"].get("1.0", "end")
            ui["memory_var"].set(False); ui["refresh_plan"](); settle(root)
            result["run_on"] = ui["run_btn"]._state
            result["reason_on"] = ui["reason_var"].get()
            result["plan_off"] = ui["plan_box"].get("1.0", "end")
            finish(root)
        ''', start="details")
        self.assertTrue(r["default_on"])
        self.assertEqual(r["run_off"], "disabled")
        self.assertIn("memory limit: NOT available: test: no systemd-run", r["reason"])
        self.assertIn("Turn off 'Limit memory (reduces the chance of running out)'", r["row"])
        self.assertIn("memory limit: NOT available", r["plan"])
        self.assertEqual((r["run_on"], r["reason_on"]), ("normal", ""))
        self.assertIn("memory limit: off", r["plan_off"])


if __name__ == "__main__":
    unittest.main()
