"""Portable install: run the Wine installer inside a bubblewrap (bwrap) wrapper.

Pure functions. Nothing here starts a process or writes anything; it only builds argument
lists and text. This reduces what an installer can change outside its prefix, as far as
bwrap isolates. It is not a security boundary: a Wine prefix and bwrap do not contain a
hostile installer.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from gitfurl import prefixes
from gitfurl.safety import running_as_root

TITLE = "Portable install (reduces changes outside this prefix)"
MENU_OVERRIDE = prefixes.MENU_OVERRIDE  # every gitfurl run has it now, portable or not

# Host variables passed into the sandbox; everything else is cleared (--clearenv), so tokens,
# keys and agent sockets in your environment do not reach the installer.
PASS_ENV = ("PATH", "LANG", "LANGUAGE", "TZ")
PASS_ENV_PREFIXES = ("LC_",)

LIMITS = (
    "This reduces changes outside the prefix as far as bwrap isolates them. It is not a "
    "guarantee and not a security boundary: a hostile installer is not contained by it."
)
PERSIST_WARNING = (
    "The prefix is kept after the run, and whatever the installer put there (startup "
    "entries, replaced DLLs, links) runs again, outside this sandbox, whenever you use the "
    "prefix later. Delete the prefix if you do not trust the installer."
)


def find_bwrap(env: dict[str, str] | None = None) -> str | None:
    """bwrap on PATH as an absolute path, else None. A relative PATH hit is refused."""
    env = os.environ if env is None else env
    found = shutil.which("bwrap", path=env.get("PATH"))
    return found if found and os.path.isabs(found) else None


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def _home(env: dict[str, str]) -> tuple[str, str]:
    """(HOME as given, HOME with symlinks resolved). Only a clean absolute path is accepted."""
    home = env.get("HOME", "")
    bad = ValueError("portable install needs HOME to be a clean absolute path to a real "
                     f"folder (not '/'); it is {home!r}")
    if not home or not home.startswith("/") or home.startswith("//"):
        raise bad
    norm = os.path.normpath(home)
    if norm != home.rstrip("/") or norm == "/":
        raise bad
    real = os.path.realpath(norm)
    if real == "/":
        raise bad
    return norm, real


def _real(path) -> str:
    return os.path.realpath(str(path))


def _wine_binary(argv0: str, home: str, home_real: str) -> str:
    """The wine program to run inside. The home folder is hidden there, so it may not live in it.

    The path is kept as given (a program may depend on the name it was started by) unless it
    lies under HOME; then the resolved target is used, which must be outside HOME.
    """
    if not os.path.isabs(argv0):
        return argv0
    real = _real(argv0)
    if _inside(real, home_real):
        raise ValueError(f"wine is inside your home folder ({argv0}), which the portable "
                         "sandbox hides; install wine system-wide or use a normal install")
    return real if _inside(argv0, home) or _inside(argv0, home_real) else argv0


def check_portable_supported(plan, bwrap_path: str | None, env: dict[str, str] | None = None,
                             installer_path: Path | None = None) -> None:
    """Raise ValueError, with the reason, when a portable run cannot be done. No fallback."""
    if plan.runtime != "wine":
        raise ValueError("portable install is only supported with the wine runtime (umu-run "
                         "downloads a runtime, and its behaviour under bwrap is untested)")
    if not bwrap_path:
        raise ValueError("portable install needs bwrap (bubblewrap) on PATH, and it was not "
                         "found. gitfurl will not fall back to a normal install by itself")
    if running_as_root():
        raise ValueError("portable install is refused as root (gitfurl does not run "
                         "installers as root)")
    if env is not None:
        home, home_real = _home(env)
        _wine_binary(plan.argv[0], home, home_real)
        folder = _real(installer_path.parent if installer_path else plan.cwd)
        if _inside(home_real, folder):
            raise ValueError(f"the installer's folder ({folder}) contains your home folder; "
                             "it is mounted read-only as a whole, so move the installer into "
                             "a folder of its own")


def _display_args(env: dict[str, str], home: str) -> tuple[list[str], list[str]]:
    """(bwrap args, notes) for the installer window: X11 and Wayland sockets only."""
    args: list[str] = []
    notes: list[str] = []
    shown = False
    display = env.get("DISPLAY")
    if display and os.path.isdir("/tmp/.X11-unix"):
        args += ["--ro-bind", "/tmp/.X11-unix", "/tmp/.X11-unix", "--setenv", "DISPLAY", display]
        auth = env.get("XAUTHORITY") or str(Path(home) / ".Xauthority")
        if os.path.isfile(auth):
            args += ["--ro-bind", auth, "/tmp/.gitfurl-xauthority",
                     "--setenv", "XAUTHORITY", "/tmp/.gitfurl-xauthority"]
        shown = True
    wl, runtime_dir = env.get("WAYLAND_DISPLAY"), env.get("XDG_RUNTIME_DIR")
    if wl and (os.path.isabs(wl) or runtime_dir):
        sock = wl if os.path.isabs(wl) else os.path.join(runtime_dir, wl)
        if os.path.exists(sock):
            args += ["--ro-bind", sock, sock, "--setenv", "WAYLAND_DISPLAY", wl]
            if runtime_dir:
                args += ["--setenv", "XDG_RUNTIME_DIR", runtime_dir]
            shown = True
    if not shown:
        notes.append("no X11 or Wayland socket was found: an installer window cannot be shown")
    return args, notes


def passed_env(env: dict[str, str]) -> dict[str, str]:
    """The host variables that go into the sandbox. Everything else is cleared."""
    return {k: v for k, v in env.items()
            if k in PASS_ENV or k.startswith(PASS_ENV_PREFIXES)}


def build_portable_argv(plan, bwrap_path: str, installer_path: Path,
                        env: dict[str, str], overrides: dict[str, str] | None = None) -> list[str]:
    """The argument list that wraps plan.argv in bwrap. Pure: reads `env` and the file system only.

    `env` is the environment gitfurl was started with (HOME, DISPLAY, ...). The sandbox starts
    from an empty environment (--clearenv); only the variables named in PASS_ENV, the display
    variables, the home and XDG folders set here, and the plan's own overrides (`overrides`,
    by default plan.env_overrides: WINEPREFIX, WINEDLLOVERRIDES, WINEDEBUG...) are set.
    """
    check_portable_supported(plan, bwrap_path, env, installer_path)
    home, home_real = _home(env)
    prefix = _real(plan.prefix)
    folder = _real(Path(installer_path).parent)
    argv: list[str] = [
        bwrap_path,
        "--die-with-parent", "--new-session",
        "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-net",
        "--clearenv",
        "--ro-bind", "/", "/",
        "--dev", "/dev", "--dev-bind-try", "/dev/dri", "/dev/dri",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--tmpfs", home_real,
    ]
    # Hide the session and system buses (they are sockets under /run, visible through the
    # read-only root otherwise). Only where the folder exists, since a missing one cannot
    # be made on a read-only root.
    for hidden in ("/run/user", "/run/dbus"):
        if os.path.isdir(hidden):
            argv += ["--tmpfs", hidden]
    # Writable prefix and read-only installer folder, shallower path first so the deeper
    # mount wins when one lies inside the other. Both come after the tmpfs they may sit under.
    binds = [(prefix, "--bind"), (folder, "--ro-bind")]
    for path, flag in sorted(binds, key=lambda b: len(Path(b[0]).parts)):
        argv += [flag, path, path]
    display, _notes = _display_args(env, home)
    argv += display
    for key, value in sorted(passed_env(env).items()):
        argv += ["--setenv", key, value]
    argv += ["--setenv", "HOME", home,
             "--setenv", "XDG_CACHE_HOME", f"{home}/.cache",
             "--setenv", "XDG_DATA_HOME", f"{home}/.local/share",
             "--setenv", "XDG_STATE_HOME", f"{home}/.local/state",
             "--setenv", "XDG_CONFIG_HOME", f"{home}/.config",
             "--setenv", "TMPDIR", "/tmp"]
    for key, value in sorted((plan.env_overrides if overrides is None else overrides).items()):
        argv += ["--setenv", key, value]
    argv += ["--chdir", _real(plan.cwd), "--", _wine_binary(plan.argv[0], home, home_real),
             *plan.argv[1:]]
    return argv


def describe_portable(plan, bwrap_path: str | None, installer_path: Path,
                      env: dict[str, str], full_argv: bool = False) -> list[str]:
    """Plain-text lines for the plan preview and the approval. Says what is and is not isolated."""
    home = env.get("HOME") or "your home folder"
    lines = [
        f"PORTABLE INSTALL: the installer runs inside bwrap ({bwrap_path or 'not found'}).",
        (f"  Writable: the dedicated prefix {plan.prefix}, a temporary home and /tmp that "
         "vanish afterwards, the run's log file (it is the installer's output), and the GPU "
         "device /dev/dri."),
        (f"  Hidden/temporary: {home}, so the wrapped installer cannot write your home, "
         "~/.cache/wine, desktop menus or file-type handlers; the session and system D-Bus "
         "sockets are hidden. Other host sockets may stay reachable."),
        ("  Environment: cleared. Only PATH, LANG, LC_*, LANGUAGE, TZ, the display variables, "
         "the temporary home and XDG folders, and the plan's variables shown above are set; "
         "tokens and keys in your environment are not passed."),
        "  Network: off. An installer that needs the network will fail.",
        (f"  Read-only: the whole installer folder {Path(installer_path).parent}, and the "
         "rest of the file system, which the installer can still read."),
        ("  Display: the X11 or Wayland socket is allowed so the installer window can show, "
         "and the X authority cookie can be read inside; a hostile installer can use that "
         "connection. Audio is not provided."),
        f"  {prefixes.MENU_LINE} (as in every gitfurl run).",
        "  If the prefix has to be created first, that step runs outside this wrapper.",
        ("  Stop signals bwrap's process group, not the installer's: the sandboxed programs "
         "run in a session of their own, and bwrap's --die-with-parent and private process "
         "namespace end them when bwrap ends. wineserver -k is not run, because the "
         "sandboxed Wine server lives in a private /tmp."),
        "  " + PERSIST_WARNING,
        "  " + LIMITS,
    ]
    if bwrap_path and env.get("HOME"):
        try:
            _args, notes = _display_args(env, env["HOME"])
        except OSError:
            notes = []
        lines += [f"  Note: {n}" for n in notes]
    if full_argv and bwrap_path:
        from shlex import join

        lines.append("bwrap command: " + join(build_portable_argv(plan, bwrap_path,
                                                                    installer_path, env)))
    return lines
