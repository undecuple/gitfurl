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

from gitfurl.safety import running_as_root

TITLE = "Portable install (no changes outside this prefix)"
MENU_OVERRIDE = {"WINEDLLOVERRIDES": "winemenubuilder.exe=d"}

LIMITS = (
    "This reduces changes outside the prefix as far as bwrap isolates them. It is not a "
    "guarantee and not a security boundary: a hostile installer is not contained by it."
)


def find_bwrap(env: dict[str, str] | None = None) -> str | None:
    """bwrap on PATH as an absolute path, else None. A relative PATH hit is refused."""
    env = os.environ if env is None else env
    found = shutil.which("bwrap", path=env.get("PATH"))
    return found if found and os.path.isabs(found) else None


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def _home(env: dict[str, str]) -> str:
    home = env.get("HOME", "")
    if not home or not os.path.isabs(home) or os.path.normpath(home) == "/":
        raise ValueError("portable install needs HOME to be set to a real folder (not '/')")
    return os.path.normpath(home)


def _wine_binary(argv0: str, home: str) -> str:
    """The wine program to run inside. The home folder is hidden there, so it may not live in it."""
    if not os.path.isabs(argv0) or not _inside(argv0, home):
        return argv0
    real = os.path.realpath(argv0)
    if _inside(real, home):
        raise ValueError(f"wine is inside your home folder ({argv0}), which the portable "
                         "sandbox hides; install wine system-wide or use a normal install")
    return real


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
        home = _home(env)
        _wine_binary(plan.argv[0], home)
        folder = str(installer_path.parent if installer_path else plan.cwd)
        if _inside(home, folder):
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


def build_portable_argv(plan, bwrap_path: str, installer_path: Path,
                        env: dict[str, str]) -> list[str]:
    """The argument list that wraps plan.argv in bwrap. Pure: reads `env` and the file system only.

    `env` is the environment gitfurl was started with (HOME, DISPLAY, ...). The plan's own
    overrides (WINEPREFIX, WINEDLLOVERRIDES) reach the program through the normal child
    environment and are not repeated here.
    """
    check_portable_supported(plan, bwrap_path, env, installer_path)
    home = _home(env)
    prefix = str(plan.prefix)
    folder = str(Path(installer_path).parent)
    argv: list[str] = [
        bwrap_path,
        "--die-with-parent", "--new-session",
        "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-net",
        "--ro-bind", "/", "/",
        "--dev", "/dev", "--dev-bind-try", "/dev/dri", "/dev/dri",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--tmpfs", home,
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
    cache, data, state, config = (f"{home}/.cache", f"{home}/.local/share",
                                  f"{home}/.local/state", f"{home}/.config")
    argv += ["--setenv", "HOME", home,
             "--setenv", "XDG_CACHE_HOME", cache, "--setenv", "XDG_DATA_HOME", data,
             "--setenv", "XDG_STATE_HOME", state, "--setenv", "XDG_CONFIG_HOME", config,
             "--setenv", "TMPDIR", "/tmp",
             "--unsetenv", "DBUS_SESSION_BUS_ADDRESS", "--unsetenv", "SSH_AUTH_SOCK"]
    if "XDG_RUNTIME_DIR" not in " ".join(display):
        argv += ["--unsetenv", "XDG_RUNTIME_DIR"]
    argv += ["--chdir", str(plan.cwd), "--", _wine_binary(plan.argv[0], home), *plan.argv[1:]]
    return argv


def describe_portable(plan, bwrap_path: str | None, installer_path: Path,
                      env: dict[str, str], full_argv: bool = False) -> list[str]:
    """Plain-text lines for the plan preview and the approval. Says what is and is not isolated."""
    home = _home(env) if env.get("HOME") else "your home folder"
    lines = [
        f"PORTABLE INSTALL: the installer runs inside bwrap ({bwrap_path or 'not found'}).",
        (f"  Writable: only the dedicated prefix {plan.prefix}, plus a temporary home and "
         "/tmp that vanish afterwards."),
        (f"  Hidden/temporary: {home} (so nothing lands in your home, ~/.cache/wine, desktop "
         "menus or file-type handlers); the session and system D-Bus sockets are hidden."),
        "  Network: off. An installer that needs the network will fail.",
        (f"  Read-only: the whole installer folder {Path(installer_path).parent}, and the "
         "rest of the file system, which the installer can still read."),
        ("  Display: the X11 or Wayland socket is allowed so the installer window can show; "
         "a hostile installer can still use that connection. Audio and most GPU access are "
         "not provided (only /dev/dri)."),
        ("  Wine's menu and file-association builder is disabled (WINEDLLOVERRIDES="
         "winemenubuilder.exe=d)."),
        "  If the prefix has to be created first, that step runs outside this wrapper.",
        ("  Stop signals bwrap's process group, not the installer's: the sandboxed programs "
         "run in a session of their own, and bwrap's --die-with-parent and private process "
         "namespace end them when bwrap ends. wineserver -k is not run, because the "
         "sandboxed Wine server lives in a private /tmp."),
        "  " + LIMITS,
    ]
    if bwrap_path:
        try:
            _args, notes = _display_args(env, home if env.get("HOME") else "/")
        except OSError:
            notes = []
        lines += [f"  Note: {n}" for n in notes]
    if full_argv and bwrap_path:
        from shlex import join

        lines.append("bwrap command: " + join(build_portable_argv(plan, bwrap_path,
                                                                    installer_path, env)))
    return lines
