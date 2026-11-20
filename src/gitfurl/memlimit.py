"""Memory limit for a run: a systemd user scope with a soft and a hard memory limit.

This reduces the chance that the system runs out of memory; it does not guarantee that the
installer succeeds. Above the soft limit the installer is slowed down; at the hard limit the
kernel stops a process of this run and systemd then ends the whole run (OOMPolicy=stop);
programs outside the run are not touched. It needs no root, no network and no Python package:
it uses the user's own systemd manager and its cgroup v2 memory controls.

What this relies on, checked against the official documentation on 2026-11-17:

- systemd-run(1), --scope: "If a command is run as transient scope unit, it will be executed
  by systemd-run itself as parent process and will thus inherit the execution environment of
  the caller." ... "Execution in this case is synchronous, and will return only when the
  command finishes." Seen on this machine (systemd 262): the command keeps systemd-run's
  process id and process group, so gitfurl's own process group, Stop and Ctrl-C are unchanged.
- systemd-run(1), --expand-environment=: "If enabled (the default), environment variables
  specified as "${VARIABLE}" will be expanded" ... "With --scope, this expansion is performed
  by systemd-run itself" ... "Disabling variable expansion is useful if the specified command
  includes or may include a "$" sign." (added in version 254). An installer path can hold a
  "$", so gitfurl passes --expand-environment=no; with an older systemd-run it refuses a
  command that holds a "$" rather than guess.
- systemd-run(1), --property: "Sets a property on the scope or service unit that is created.
  This option takes an assignment in the same format as systemctl(1)'s set-property command."
- systemd.resource-control(5), MemoryHigh=: "Specify the throttling limit on memory usage of
  the executed processes in this unit. Memory usage may go above the limit if unavoidable,
  but the processes are heavily slowed down and memory is taken away aggressively in such
  cases." MemoryMax=: "If memory usage cannot be contained under the limit, out-of-memory
  killer is invoked inside the unit." Both take a size in bytes.
- systemctl(1), set-property: "Set the specified unit properties at runtime where this is
  supported." --runtime: "make changes only temporarily, so that they are lost on the next
  reboot."
- Linux, Documentation/admin-guide/cgroup-v2: a new process "is born into the cgroup that the
  forking process belongs to" (so Wine's server, which starts its own session, stays in the
  scope); memory.high: "Going over the high limit never invokes the OOM killer";
  memory.events oom_kill: "The number of processes belonging to this cgroup killed by any
  kind of OOM killer."

Swap is left alone (no MemorySwapMax): MemoryMax counts memory, not swap, so a run that goes
over the soft limit can move pages to swap and slow down instead of being stopped. Seen on
this machine: with swap capped at 0 a test program held at its soft limit stalled for minutes
instead of finishing.

--collect is not used: with it, a scope whose last process was stopped by the out-of-memory
killer is unloaded at once, with its memory.events, so the result could not be read
(seen on this machine). Without it systemd keeps that one failed unit with Result=oom-kill;
gitfurl reads that and then clears it with `systemctl --user reset-failed <that unit>`.

OOMPolicy=stop is passed explicitly. systemd.scope(5) lists OOMPolicy= for scope units; with
"stop" "the event is logged and the unit's processes are terminated cleanly by the service
manager" and the unit "ultimately ends up in the oom-kill failed state". The default comes from
DefaultOOMPolicy=, which can be changed, so gitfurl does not rely on it. Seen on this machine
(systemd 262, 2026-11-17, test programs in scopes with MemoryMax=200M and MemorySwapMax=0):
a normal end left no unit at all (LoadState=not-found) at once; an out-of-memory kill of the
main process, or of a child while the main process would have exited 0 or 3, left the unit
loaded as failed with Result=oom-kill (and in the child cases systemd ended the main process
with SIGTERM). So when this run's unit is gone after the run and was never seen failed,
gitfurl reports that no out-of-memory stop was recorded, and says why. A scope ends normally
before gitfurl can read its memory.events, so that read alone can not tell.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field

MIB = 1024**2
GIB = 1024**3
RESERVE_MIN = GIB  # always keep at least this much for the rest of the system
RESERVE_FRACTION = 0.10  # ... or this share of MemTotal, whichever is larger
MAX_FLOOR = GIB  # refuse to start when the hard limit would be below this
HIGH_FRACTION = 0.90  # soft limit = this share of the hard limit
HIGH_FLOOR = GIB  # the soft limit is never lowered below this (or its start value if smaller)
ADJUST_INTERVAL_S = 5.0
MIN_CHANGE = 64 * MIB  # smaller changes are not worth a call
RAISE_MARGIN = 256 * MIB  # raise only when this much more than the reserve is free
EXPAND_FLAG_SINCE = 254  # systemd-run --expand-environment= was added in this version
PROBE_TIMEOUT_S = 5

WORDING = ("This reduces the chance that the system runs out of memory; it does not guarantee "
           "that the installer succeeds. Above the soft limit the installer is slowed down; at "
           "the hard limit the kernel stops a process of this run and systemd then ends the whole run; "
           "programs outside the run are not touched.")


class MemoryLimitError(ValueError):
    """The memory limit cannot be set up. Nothing has been run."""


def gib(n: int) -> str:
    return f"{n / GIB:.2f} GiB"


# ---- the budget -------------------------------------------------------------------------

def read_meminfo(path: str = "/proc/meminfo") -> tuple[int, int] | None:
    """(MemTotal, MemAvailable) in bytes, or None."""
    found: dict[str, int] = {}
    try:
        with open(path, encoding="ascii") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    found[key] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    if "MemTotal" not in found or "MemAvailable" not in found:
        return None
    return found["MemTotal"], found["MemAvailable"]


@dataclass(frozen=True)
class Budget:
    total: int
    available: int
    reserve: int
    max_bytes: int
    high_bytes: int

    @property
    def high_floor(self) -> int:
        return min(self.high_bytes, HIGH_FLOOR)

    def lines(self) -> list[str]:
        return [
            f"  measured now: MemTotal {gib(self.total)}, MemAvailable {gib(self.available)}",
            (f"  kept free for the rest of the system: {gib(self.reserve)} (the larger of 1 GiB "
             "and 10% of MemTotal)"),
            (f"  hard limit MemoryMax={self.max_bytes} ({gib(self.max_bytes)}) = MemAvailable "
             "minus that reserve; at this limit a process of this run is stopped and systemd then ends "
             "the whole run (OOMPolicy=stop)"),
            (f"  soft limit MemoryHigh={self.high_bytes} ({gib(self.high_bytes)}) = 90% of the "
             "hard limit; above it the installer is slowed down, nothing is stopped"),
            ("  swap: not limited (no MemorySwapMax), so the run can use swap and slow down "
             "rather than be stopped"),
            (f"  while it runs: every {ADJUST_INTERVAL_S:g} s, if MemAvailable falls below the "
             "reserve, the soft limit is lowered by the shortfall (never below "
             f"{gib(self.high_floor)}); when memory is free again it is raised back, never "
             "above its start value. The hard limit is never raised."),
            "  the values are measured again when the run starts",
        ]


def compute_budget(total: int, available: int) -> Budget:
    """The limits for a run. Raises MemoryLimitError, with the numbers, if too little is free."""
    reserve = max(RESERVE_MIN, int(total * RESERVE_FRACTION))
    hard = available - reserve
    if hard < MAX_FLOOR:
        need = MAX_FLOOR - hard
        raise MemoryLimitError(
            f"memory limit: too little memory is free to start safely. Available now: "
            f"{gib(available)}; kept free for the rest of the system: {gib(reserve)}; that "
            f"leaves {gib(max(0, hard))}, below the {gib(MAX_FLOOR)} minimum for a run. Free at "
            f"least {gib(need)} (close other programs) and try again")
    hard = hard // MIB * MIB
    high = int(hard * HIGH_FRACTION) // MIB * MIB
    return Budget(total, available, reserve, hard, high)


def budget_from(meminfo: str = "/proc/meminfo") -> Budget:
    mem = read_meminfo(meminfo)
    if mem is None:
        raise MemoryLimitError(f"memory limit: could not read MemTotal and MemAvailable from "
                               f"{meminfo}")
    return compute_budget(*mem)


def next_high(current: int, budget: Budget, available: int, usage: int | None) -> int | None:
    """The new soft limit for one adjustment step, or None for no change.

    If MemAvailable is below the reserve, the run's soft limit is lowered by the shortfall,
    counted from what the run uses now (from the current soft limit if that is unknown). If
    more than the reserve plus a margin is free, it is raised by the surplus. The result is
    kept between budget.high_floor and the start value budget.high_bytes, and changes smaller
    than MIN_CHANGE are skipped unless they reach one of those bounds.
    """
    if available < budget.reserve:
        base = current if usage is None else min(usage, current)
        target = base - (budget.reserve - available)
    elif available > budget.reserve + RAISE_MARGIN and current < budget.high_bytes:
        target = current + (available - budget.reserve - RAISE_MARGIN)
    else:
        return None
    target = max(budget.high_floor, min(budget.high_bytes, target // MIB * MIB))
    if target == current:
        return None
    if abs(target - current) < MIN_CHANGE and target not in (budget.high_floor,
                                                             budget.high_bytes):
        return None
    return target


# ---- availability -----------------------------------------------------------------------

@dataclass(frozen=True)
class Availability:
    ok: bool
    reason: str
    systemd_run: str | None = None
    systemctl: str | None = None
    version: int | None = None  # set by verify() (it runs systemd-run --version)
    manager_cgroup: str | None = None


def _which(name: str, env) -> str | None:
    found = shutil.which(name, path=env.get("PATH"))
    return found if found and os.path.isabs(found) else None


def _controllers(path: str) -> list[str] | None:
    try:
        with open(path, encoding="ascii") as fh:
            return fh.read().split()
    except OSError:
        return None


def availability(env=None, cgroup_root: str = "/sys/fs/cgroup", uid: int | None = None
                 ) -> Availability:
    """Whether a memory-limited scope can be used. Reads files only; starts no process.

    The checks: systemd-run and systemctl on PATH, a user systemd manager (its private socket
    in XDG_RUNTIME_DIR), the unified cgroup v2 hierarchy, and the memory controller in the
    user manager's cgroup (the usual place, user.slice/user-UID.slice/user@UID.service).
    verify() checks again with the manager itself when the run starts.
    """
    env = os.environ if env is None else env
    uid = os.getuid() if uid is None else uid
    sr, sc = _which("systemd-run", env), _which("systemctl", env)
    if not sr:
        return Availability(False, "systemd-run was not found on PATH")
    if not sc:
        return Availability(False, "systemctl was not found on PATH", sr)
    rt = env.get("XDG_RUNTIME_DIR", "")
    if not rt or not os.path.isabs(rt):
        return Availability(False, "XDG_RUNTIME_DIR is not set, so no user systemd manager "
                            "can be reached", sr, sc)
    if not os.path.exists(os.path.join(rt, "systemd", "private")):
        return Availability(False, f"no user systemd manager is running (no "
                            f"{rt}/systemd/private)", sr, sc)
    if _controllers(os.path.join(cgroup_root, "cgroup.controllers")) is None:
        return Availability(False, f"the unified cgroup v2 hierarchy is not mounted at "
                            f"{cgroup_root}", sr, sc)
    cg = f"/user.slice/user-{uid}.slice/user@{uid}.service"
    have = _controllers(cgroup_root + cg + "/cgroup.controllers")
    if have is None:
        return Availability(False, f"your user manager's cgroup was not found at "
                            f"{cgroup_root}{cg}", sr, sc)
    if "memory" not in have:
        return Availability(False, "the memory controller is not delegated to your user "
                            f"manager (its cgroup.controllers lists: {' '.join(have) or 'none'})",
                            sr, sc)
    return Availability(True, "systemd-run and the memory controller are available to your "
                        "user manager", sr, sc, None, cg)


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _probe(argv, env, run) -> subprocess.CompletedProcess:
    return run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S, check=False,
               env=env)


def verify(avail: Availability, env=None, run=None, cgroup_root: str = "/sys/fs/cgroup"
           ) -> Availability:
    """Ask systemd itself (systemd-run --version, systemctl --user show). Raises if unusable.

    Read-only probes with fixed arguments and a timeout, run only when a real run starts.
    """
    if not avail.ok:
        raise MemoryLimitError(f"memory limit: NOT available: {avail.reason}")
    env = dict(os.environ if env is None else env)
    run = run or subprocess.run
    try:
        out = _probe([avail.systemd_run, "--version"], env, run)
        m = re.match(r"systemd (\d+)", _text(out.stdout).strip())
        if out.returncode != 0 or not m:
            raise MemoryLimitError("memory limit: NOT available: could not read systemd-run's "
                                   "version")
        version = int(m.group(1))
        show = _probe([avail.systemctl, "--user", "show", "-p", "ControlGroup", "--value"],
                      env, run)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MemoryLimitError(f"memory limit: NOT available: {exc}") from exc
    cg = _text(show.stdout).strip()
    if show.returncode != 0 or not _clean_cgroup(cg):
        first = (_text(show.stderr).strip().splitlines() or ["no answer"])[0][:200]
        raise MemoryLimitError(f"memory limit: NOT available: the user systemd manager did not "
                               f"answer ({first})")
    have = _controllers(cgroup_root + cg + "/cgroup.controllers") or []
    if "memory" not in have:
        raise MemoryLimitError("memory limit: NOT available: the memory controller is not "
                               "delegated to your user manager")
    return Availability(True, avail.reason, avail.systemd_run, avail.systemctl, version, cg)


def _clean_cgroup(cg: str) -> bool:
    return cg.startswith("/") and ".." not in cg.split("/") and "\n" not in cg


def describe(enabled: bool, avail: Availability | None, meminfo: str = "/proc/meminfo"
             ) -> tuple[list[str], str | None]:
    """(plan lines, problem). problem is None when a run may go ahead. Reads files only."""
    if not enabled:
        return (["memory limit: off (turned off by you; the run is not limited and can use "
                 + "all free memory)"], None)
    if avail is None or not avail.ok:
        reason = avail.reason if avail else "not checked"
        return ([f"memory limit: NOT available: {reason}"],
                f"memory limit: NOT available: {reason}")
    try:
        budget = budget_from(meminfo)
    except MemoryLimitError as exc:
        return [str(exc)], str(exc)
    lines = ["memory limit: on (a systemd user scope only for this run). " + WORDING]
    lines += budget.lines()
    lines.append("  command: " + " ".join(
        [avail.systemd_run or "systemd-run", "--user", "--scope", "--quiet",
         "--unit=gitfurl-<prefix>-<time>.scope", "--expand-environment=no",
         "-p", f"MemoryHigh={budget.high_bytes}", "-p", f"MemoryMax={budget.max_bytes}",
         "-p", "OOMPolicy=stop", "--", "<the command above>"]))
    return lines, None


# ---- the scope of one run -----------------------------------------------------------------

def unit_name(prefix_name: str, label: str = "", now: float | None = None) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", prefix_name)[:64]
    middle = f"{label}-" if label else ""
    return f"gitfurl-{safe}-{middle}{stamp}-{os.getpid()}.scope"


@dataclass
class MemoryReport:
    unit: str
    budget: Budget
    oom_kill: int | None = None  # from the scope's own memory.events (None: never read)
    oom_result: bool = False  # systemd reported Result=oom-kill for the unit
    high: int | None = None  # memory.events "high", as last read
    changes: list = field(default_factory=list)  # (old, new) soft limit changes
    notes: list = field(default_factory=list)
    final_read: bool = False  # memory.events was read after the run ended
    unit_gone: bool = False  # after the run systemd no longer had the unit (LoadState=not-found)
    start_failure: str | None = None  # a "Failed to ..." line systemd-run left in the log

    @property
    def hit_limit(self) -> bool:
        return self.oom_result or bool(self.oom_kill)

    def lines(self) -> list[str]:
        if self.hit_limit:
            count = f" ({self.oom_kill} process(es) by this run's memory.events)" \
                if self.oom_kill else ""
            out = [(f"Memory limit: the run hit its hard memory limit "
                    f"({gib(self.budget.max_bytes)}): the kernel stopped a process of this run"
                    f"{count} and systemd then ended the whole run. The install may be incomplete; read the "
                    "log. The hard limit was fixed from the memory available when the run "
                    "started; to retry without it, use --no-memory-limit, or turn the option off "
                    "in the window.")]
        elif self.start_failure:
            out = [("Memory limit: systemd-run may have failed, so the installer may not have "
                    f"run at all; the exit status may be systemd-run's. The log has: "
                    f"'{self.start_failure}'. See the log.")]
        elif self.unit_gone and not self.oom_kill:
            out = [(f"Memory limit: no out-of-memory stop was recorded in this run (hard limit "
                    f"{gib(self.budget.max_bytes)}). systemd had already removed the run's scope, "
                    "which it does only when the run ended without one: a stop by the "
                    "out-of-memory killer leaves the scope as failed (OOMPolicy=stop).")]
        elif not self.final_read:
            seen = (f"; while the run lasted it showed {self.oom_kill} out-of-memory stop(s)"
                    if self.oom_kill is not None else "")
            out = [(f"Memory limit: this run's memory.events could not be read after the run "
                    f"(the scope may already be gone){seen}, so an out-of-memory stop could not "
                    f"be checked (hard limit {gib(self.budget.max_bytes)}).")]
        else:
            out = [(f"Memory limit: no out-of-memory stop was seen in this run (hard limit "
                    f"{gib(self.budget.max_bytes)}).")]
        if self.high:
            out.append(f"The installer was slowed down at the soft limit (memory.events "
                       f"'high': {self.high}, as last read).")
        if self.changes:
            low = min(new for _old, new in self.changes)
            out.append(f"The soft limit was changed {len(self.changes)} time(s) because other "
                       f"programs needed memory (lowest {gib(low)}).")
        out += [f"Memory limit note: {n}" for n in self.notes]
        return out


EVENT_KEYS = ("high", "max", "oom", "oom_kill")
ACTIVE_STATES = ("active", "activating", "deactivating")
START_FAILURE_PREFIX = "Failed to "  # systemd-run's own error lines (seen with systemd 262)


def find_start_failure(log_text: str) -> str | None:
    """The first line of the run's output that looks like a systemd-run error, or None.

    Seen with systemd 262 on 2026-11-17: "Failed to parse MemoryMax= value ...", "Failed to find
    executable ...", "Failed to connect to user scope bus ...". Only a hint: a program run inside
    the scope could print such a line too, so callers say "may have failed".
    """
    for line in log_text.splitlines():
        if line.startswith(START_FAILURE_PREFIX):
            return line.strip()[:200]
    return None


class MemoryScope:
    """The systemd user scope of one run: wraps the argv, adjusts the soft limit, reports.

    Every systemctl call names this one unit, which gitfurl created; no other unit is read or
    changed, nothing is stopped and nothing is found by name.
    """

    def __init__(self, avail: Availability, budget: Budget, unit: str, env=None, run=None,
                 cgroup_root: str = "/sys/fs/cgroup", meminfo: str = "/proc/meminfo",
                 interval: float = ADJUST_INTERVAL_S, on_change=None, on_note=None,
                 settle_s: float = 5.0):
        self.avail, self.budget, self.unit = avail, budget, unit
        self.env = dict(os.environ if env is None else env)
        self.run = run or subprocess.run
        self.cgroup_root, self.meminfo, self.interval = cgroup_root, meminfo, interval
        self.on_change, self.on_note, self.settle_s = on_change, on_note, settle_s
        self.high = budget.high_bytes
        self.report = MemoryReport(unit, budget)
        self._events: dict[str, int] = {}
        self._cgroup: str | None = None
        self._stop = threading.Event()
        # Held while a tick runs, so no tick (and no on_note) starts after finish() took it.
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._failed_set = False

    def wrap(self, argv: list[str]) -> list[str]:
        out = [self.avail.systemd_run, "--user", "--scope", "--quiet", f"--unit={self.unit}"]
        if self.avail.version is not None and self.avail.version >= EXPAND_FLAG_SINCE:
            out.append("--expand-environment=no")
        elif any("$" in a for a in argv):
            raise MemoryLimitError(
                f"memory limit: systemd-run {self.avail.version} is older than "
                f"{EXPAND_FLAG_SINCE} and cannot be told not to expand '$', and the command "
                "holds a '$'. Rename the file or folder, or turn the memory limit off")
        return [*out, "-p", f"MemoryHigh={self.budget.high_bytes}",
                "-p", f"MemoryMax={self.budget.max_bytes}", "-p", "OOMPolicy=stop", "--", *argv]

    # -- systemctl, only for self.unit --
    def _systemctl(self, *args: str) -> subprocess.CompletedProcess | None:
        try:
            return self.run([self.avail.systemctl, "--user", *args], capture_output=True,
                            text=True, timeout=10, check=False, env=self.env)
        except (OSError, subprocess.TimeoutExpired):
            return None

    def _show(self, *props: str) -> dict[str, str]:
        args = ["show"]
        for p in props:
            args += ["-p", p]
        done = self._systemctl(*args, self.unit)
        if done is None or done.returncode != 0:
            return {}
        out = {}
        for line in _text(done.stdout).splitlines():
            k, _, v = line.partition("=")
            out[k] = v
        return out

    def _find_cgroup(self) -> str | None:
        cg = self._show("ControlGroup").get("ControlGroup", "")
        if cg and _clean_cgroup(cg) and cg.endswith("/" + self.unit):
            return cg
        return None

    def _read(self, name: str) -> str | None:
        if self._cgroup is None:
            return None
        try:
            with open(self.cgroup_root + self._cgroup + "/" + name, encoding="ascii") as fh:
                return fh.read()
        except OSError:
            return None

    def read_events(self) -> bool:
        """Read this scope's memory.events; False if it could not be read."""
        text = self._read("memory.events")
        if text is None:
            return False
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] in EVENT_KEYS and parts[1].isdigit():
                self._events[parts[0]] = max(self._events.get(parts[0], 0), int(parts[1]))
        self.report.oom_kill = self._events.get("oom_kill", 0)
        self.report.high = self._events.get("high", 0)
        return True

    def _usage(self) -> int | None:
        text = self._read("memory.current")
        try:
            return int(text) if text is not None else None
        except ValueError:
            return None

    def set_high(self, value: int) -> bool:
        done = self._systemctl("set-property", "--runtime", self.unit, f"MemoryHigh={value}")
        if done is None or done.returncode != 0:
            if not self._failed_set:
                self._failed_set = True
                self.report.notes.append("could not change the soft limit while the run lasted "
                                         "(systemctl set-property failed)")
            return False
        return True

    def tick(self) -> None:
        """One adjustment step (called every `interval` seconds while the run lasts)."""
        if self._cgroup is None:
            self._cgroup = self._find_cgroup()
        self.read_events()
        mem = read_meminfo(self.meminfo)
        if mem is None:
            return
        available = mem[1]
        usage = self._usage()
        new = next_high(self.high, self.budget, available, usage)
        if new is None or not self.set_high(new):
            return
        old, self.high = self.high, new
        self.report.changes.append((old, new))
        if self.on_change:
            self.on_change(old, new, available, usage)
        if self.on_note:
            if new < old:
                self.on_note(f"memory: other programs need memory (available {gib(available)}, "
                             f"below the {gib(self.budget.reserve)} kept free), so the "
                             f"installer's soft limit was lowered from {gib(old)} to {gib(new)}; "
                             "above it the installer is slowed down, nothing is stopped.")
            else:
                self.on_note(f"memory: memory is free again (available {gib(available)}), so "
                             f"the installer's soft limit was raised from {gib(old)} to "
                             f"{gib(new)}.")

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            with self._lock:
                if self._stop.is_set():
                    return
                try:
                    self.tick()
                except Exception:  # noqa: BLE001, S110 - the limit itself stays; the run must go on
                    pass

    def start(self, adjust: bool = True) -> None:
        if adjust:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def finish(self) -> MemoryReport:
        """After the run: read the result of this unit only, then clear it if it failed."""
        self._stop.set()
        with self._lock:  # wait for a running tick; none starts after this
            pass
        if self._thread:
            self._thread.join(timeout=15)
        state: dict[str, str] | None = None
        try:
            if self._cgroup is None:
                self._cgroup = self._find_cgroup()
            if self.read_events():
                self.report.final_read = True
            deadline = time.monotonic() + self.settle_s
            while True:  # a process of the run (e.g. Wine's server) may keep it a few seconds
                state = self._show("ActiveState", "Result", "LoadState")
                if state.get("ActiveState") not in ACTIVE_STATES \
                        or time.monotonic() >= deadline:
                    break
                time.sleep(0.2)
                if self.read_events():
                    self.report.final_read = True
        finally:  # also on Ctrl-C or an error above: settle this unit's result
            if state is None:
                state = self._show("ActiveState", "Result", "LoadState")
            self.report.unit_gone = state.get("LoadState") == "not-found"
            if state.get("Result") == "oom-kill":
                self.report.oom_result = True
            if state.get("ActiveState") == "failed":
                self._systemctl("reset-failed", self.unit)
            elif state.get("ActiveState") in ACTIVE_STATES:
                self.report.notes.append(
                    f"the run's scope {self.unit} is still active (a process of the run, for "
                    "example Wine's server, is still running in it); its limits stay until it "
                    "ends. If it then stays listed as failed, clear it with: systemctl --user "
                    f"reset-failed {self.unit}")
        return self.report


def prepare(prefix_name: str, env=None, run=None, cgroup_root: str = "/sys/fs/cgroup",
            meminfo: str = "/proc/meminfo", label: str = "", **kw) -> MemoryScope:
    """Check availability (files, then systemd itself) and the budget. Raises MemoryLimitError."""
    avail = verify(availability(env, cgroup_root), env, run, cgroup_root)
    budget = budget_from(meminfo)
    return MemoryScope(avail, budget, unit_name(prefix_name, label), env, run, cgroup_root,
                       meminfo, **kw)
