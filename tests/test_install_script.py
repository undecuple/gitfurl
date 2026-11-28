"""install.sh, the optional download-and-install script. No network: the "release" is a
folder served through GITFURL_BASE_URL=file://..., and the "program" is a small shell
script that prints its arguments. Temporary HOME and TMPDIR; id and uname are shims."""

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "install.sh"
SH = shutil.which("sh")
VERSION = "v9.8.7"
ASSET = f"gitfurl-{VERSION}-linux-x86_64"

FAKE_PROGRAM = """#!/bin/sh
# A stand-in for the gitfurl program: prints its arguments and records the call.
if [ -n "${FAKE_LOG:-}" ]; then printf '%s\\n' "$*" >> "$FAKE_LOG"; fi
case "${1:-}" in
  --echo-stdin) cat; exit 0 ;;
  --exit) exit "$2" ;;
  --where) printf '%s\\n' "$0"; exit 0 ;;
esac
for a in "$@"; do printf 'arg:%s\\n' "$a"; done
echo "fake gitfurl NAME"
"""

ID_SHIM = """#!/bin/sh
if [ "${1:-}" = -u ]; then echo "${FAKE_UID:-1000}"; else echo "uid=${FAKE_UID:-1000}"; fi
"""

UNAME_SHIM = """#!/bin/sh
case "${1:-}" in
  -s) echo "${FAKE_OS:-Linux}" ;;
  -m) echo "${FAKE_ARCH:-x86_64}" ;;
  *) echo "${FAKE_OS:-Linux}" ;;
esac
"""

# A curl or wget that records its arguments and "downloads" from the release folder.
FETCH_SHIM = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_FETCH_LOG"
out=
url=
while [ $# -gt 0 ]; do
  case "$1" in
    -o|-O) out="$2"; shift 2 ;;
    *) url="$1"; shift ;;
  esac
done
cp "$FAKE_SRV${url#https://mirror.test/releases}" "$out"
"""


def _write_exec(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


@unittest.skipUnless(SH and shutil.which("sha256sum"), "needs sh and sha256sum")
class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.tmpdir = self.base / "tmpdir"
        self.tmpdir.mkdir()
        self.srv = self.base / "srv"
        self.shims = self.base / "shims"
        self.shims.mkdir()
        _write_exec(self.shims / "id", ID_SHIM)
        _write_exec(self.shims / "uname", UNAME_SHIM)
        self.log = self.base / "calls.log"
        self.bindir = self.home / ".local" / "bin"
        self.target = self.bindir / "gitfurl"
        self.publish("one")

    def publish(self, name: str, *, bad_sum: bool = False, folder: str = "latest/download"):
        """Put a fake program and its SHA256SUMS where the release would be."""
        d = self.srv / folder
        d.mkdir(parents=True, exist_ok=True)
        body = FAKE_PROGRAM.replace("NAME", name).encode()
        (d / ASSET).write_bytes(body)
        digest = hashlib.sha256(body + (b"x" if bad_sum else b"")).hexdigest()
        (d / "SHA256SUMS").write_text(f"{digest}  {ASSET}\n")
        return body

    def env(self, **extra):
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.shims}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "TMPDIR": str(self.tmpdir),
            "GITFURL_BASE_URL": f"file://{self.srv}",
            "FAKE_LOG": str(self.log),
            "LC_ALL": "C",
        }
        env.update(extra)
        return env

    def run_script(self, *args, env=None, stdin=None, **kw):
        return subprocess.run(
            [SH, str(SCRIPT), *args], env=env or self.env(), input=stdin, text=True,
            capture_output=True, timeout=60, **kw)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def assert_tmp_empty(self):
        self.assertEqual(list(self.tmpdir.iterdir()), [])

    # --- install -----------------------------------------------------------------------

    def test_install_then_replace(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"Installed {self.target} (mode 0755).", r.stdout)
        self.assertIn(f"Checked: {ASSET} matches SHA256SUMS", r.stdout)
        self.assertIn("is not on your PATH", r.stderr)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o755)
        self.assertIn(b"fake gitfurl one", self.target.read_bytes())
        self.assert_tmp_empty()

        second = self.publish("two")
        env = self.env(PATH=f"{self.bindir}:{self.env()['PATH']}")
        r = self.run_script(env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"Replaced {self.target} (mode 0755).", r.stdout)
        self.assertNotIn("is not on your PATH", r.stderr)
        self.assertEqual(self.target.read_bytes(), second)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o755)
        self.assertEqual(sorted(p.name for p in self.bindir.iterdir()), ["gitfurl"])
        self.assertEqual(self.calls(), [])  # installing runs nothing
        self.assert_tmp_empty()

    def test_xdg_bin_home_and_dir(self):
        xdg = self.base / "xdg bin"
        r = self.run_script(env=self.env(XDG_BIN_HOME=str(xdg)))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((xdg / "gitfurl").is_file())
        other = self.base / "other"
        r = self.run_script("--dir", str(other))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((other / "gitfurl").is_file())
        self.assertFalse(self.target.exists())

    def test_version_uses_that_release(self):
        self.publish("pinned", folder=f"download/{VERSION}")
        r = self.run_script("--version", VERSION)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"file://{self.srv}/download/{VERSION}/{ASSET}", r.stdout)
        self.assertIn(b"fake gitfurl pinned", self.target.read_bytes())
        r = self.run_script("--version", "v1.0.0")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cannot read", r.stderr)

    def test_checksum_mismatch_refused(self):
        self.publish("evil", bad_sum=True)
        r = self.run_script()
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not match its line in SHA256SUMS", r.stderr)
        self.assertIn("Nothing was installed or run.", r.stderr)
        self.assertFalse(self.bindir.exists())
        self.assert_tmp_empty()
        r = self.run_script("--run", "cli", "doctor")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.calls(), [])
        self.assert_tmp_empty()

    def test_sums_without_the_program_refused(self):
        d = self.srv / "latest" / "download"
        (d / "SHA256SUMS").write_text("0" * 64 + "  something-else\n")
        r = self.run_script()
        self.assertEqual(r.returncode, 1)
        self.assertIn("has no line for", r.stderr)
        self.assertFalse(self.bindir.exists())

    def test_link_in_place_refused(self):
        self.bindir.mkdir(parents=True)
        real = self.base / "elsewhere"
        real.write_text("keep")
        self.target.symlink_to(real)
        r = self.run_script()
        self.assertEqual(r.returncode, 1)
        self.assertIn("symbolic link", r.stderr)
        self.assertEqual(real.read_text(), "keep")
        r = self.run_script("--uninstall")
        self.assertEqual(r.returncode, 1)
        self.assertTrue(self.target.is_symlink())

    def test_wine_folder_refused(self):
        r = self.run_script("--dir", str(self.home / ".wine" / "bin"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("never touches ~/.wine", r.stderr)
        self.assertFalse((self.home / ".wine").exists())

    def test_desktop_entry(self):
        r = self.run_script("--desktop-entry")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls(), ["cli desktop-entry --yes"])

    # --- URLs and downloaders ----------------------------------------------------------

    def test_http_refused(self):
        for url in ("http://mirror.test/releases", "ftp://mirror.test/releases", "file:relative"):
            r = self.run_script(env=self.env(GITFURL_BASE_URL=url))
            self.assertEqual(r.returncode, 1, url)
            self.assertIn("refusing plain http://" if url.startswith("http:") else
                          "must start with https://", r.stderr)
            self.assertFalse(self.bindir.exists())
            self.assert_tmp_empty()

    def test_https_through_curl(self):
        _write_exec(self.shims / "curl", FETCH_SHIM)
        fetch_log = self.base / "fetch.log"
        env = self.env(GITFURL_BASE_URL="https://mirror.test/releases/",
                       FAKE_SRV=str(self.srv), FAKE_FETCH_LOG=str(fetch_log))
        r = self.run_script(env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = fetch_log.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertIn("--proto =https --proto-redir =https --tlsv1.2", line)
        self.assertTrue(lines[0].endswith("https://mirror.test/releases/latest/download/SHA256SUMS"))
        self.assertTrue(lines[1].endswith(f"https://mirror.test/releases/latest/download/{ASSET}"))
        self.assertTrue(self.target.is_file())

    def test_https_through_wget_when_no_curl(self):
        # A PATH with only the tools the script needs, a fake wget and no curl.
        tools = self.base / "tools"
        tools.mkdir()
        for name in ("cp", "mv", "rm", "mkdir", "chmod", "mktemp", "sha256sum", "sed", "cat"):
            found = shutil.which(name)
            if not found:
                self.skipTest(f"{name} not found")
            (tools / name).symlink_to(found)
        for name in ("id", "uname"):
            (tools / name).symlink_to(self.shims / name)
        _write_exec(tools / "wget", FETCH_SHIM)
        fetch_log = self.base / "fetch.log"
        env = self.env(PATH=str(tools), GITFURL_BASE_URL="https://mirror.test/releases",
                       FAKE_SRV=str(self.srv), FAKE_FETCH_LOG=str(fetch_log))
        r = self.run_script(env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = fetch_log.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertIn("--https-only --secure-protocol=TLSv1_2", line)
        self.assertTrue(self.target.is_file())
        (tools / "wget").unlink()
        r = self.run_script("--run", "x", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("neither curl nor wget was found", r.stderr)

    # --- run once ----------------------------------------------------------------------

    def test_run_passes_args_and_status(self):
        args = ["cli", "install", "/home/u/my setup.exe", "it's \"quoted\"", "", "*",
                "--dry-run", "--uninstall", "$HOME", "a\nb"]
        r = self.run_script("--run", *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        expect = "".join(f"arg:{a}\n" for a in args) + "fake gitfurl one\n"
        self.assertEqual(r.stdout, expect)  # the script's own lines go to stderr
        self.assertIn("Running: gitfurl cli install '/home/u/my setup.exe'", r.stderr)
        self.assertFalse(self.bindir.exists())
        self.assert_tmp_empty()

        r = self.run_script("--run", "--exit", "7")
        self.assertEqual(r.returncode, 7)
        self.assert_tmp_empty()

        r = self.run_script("--run", "--echo-stdin", stdin="hello\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "hello\n")

        r = self.run_script("--run", "--where")
        self.assertTrue(r.stdout.strip().startswith(str(self.tmpdir / "gitfurl-install.")))
        self.assertTrue(r.stdout.strip().endswith("/gitfurl"))
        self.assert_tmp_empty()

    def test_run_with_script_on_stdin(self):
        # As in: curl ... | sh -s -- --run ...; the pipe is the script, not gitfurl's input.
        r = subprocess.run(
            ["sh", "-s", "--", "--run", "--echo-stdin"], env=self.env(),
            input=SCRIPT.read_text(), text=True, capture_output=True, timeout=60,
            start_new_session=True)  # no controlling terminal, so no /dev/tty
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")
        r = subprocess.run(
            ["sh", "-s", "--", "--run", "cli", "install", "/x/my setup.exe", "--dry-run"],
            env=self.env(), input=SCRIPT.read_text(), text=True, capture_output=True,
            timeout=60, start_new_session=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[:4],
                         ["arg:cli", "arg:install", "arg:/x/my setup.exe", "arg:--dry-run"])
        self.assert_tmp_empty()

    def test_run_refuses_install_options(self):
        for args in (["--dir", "/x", "--run"], ["--desktop-entry", "--run"],
                     ["--uninstall", "--run"]):
            r = self.run_script(*args)
            self.assertEqual(r.returncode, 2, args)
            self.assertEqual(self.calls(), [])

    # --- uninstall ---------------------------------------------------------------------

    def test_uninstall(self):
        self.assertEqual(self.run_script().returncode, 0)
        self.bindir.joinpath("other").write_text("keep")
        r = self.run_script("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"Removed {self.target}.", r.stdout)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.bindir.joinpath("other").read_text(), "keep")
        self.assertEqual(self.calls(), ["cli desktop-entry --help",
                                        "cli desktop-entry --remove --yes"])
        r = self.run_script("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Nothing to remove", r.stdout)

    def test_uninstall_without_desktop_command(self):
        self.bindir.mkdir(parents=True)
        _write_exec(self.target, "#!/bin/sh\nexit 2\n")
        r = self.run_script("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no 'cli desktop-entry' command", r.stdout)
        self.assertFalse(self.target.exists())

    # --- dry run -----------------------------------------------------------------------

    def test_dry_run_downloads_nothing(self):
        _write_exec(self.shims / "curl", FETCH_SHIM)
        fetch_log = self.base / "fetch.log"
        env = self.env(GITFURL_BASE_URL="https://mirror.test/releases",
                       FAKE_SRV=str(self.srv), FAKE_FETCH_LOG=str(fetch_log))
        r = self.run_script("--dry-run", "--desktop-entry", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("https://mirror.test/releases/latest/download/SHA256SUMS", r.stdout)
        self.assertIn("https://mirror.test/releases/latest/download/gitfurl-vX.Y.Z-linux-x86_64",
                      r.stdout)
        self.assertIn(f"Install it as {self.target}", r.stdout)
        self.assertIn("cli desktop-entry --yes", r.stdout)
        r = self.run_script("--dry-run", "--version", VERSION, "--run", "cli", "a b", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"https://mirror.test/releases/download/{VERSION}/{ASSET}", r.stderr)
        self.assertIn("Run from there: gitfurl cli 'a b'", r.stderr)
        r = self.run_script("--dry-run", "--uninstall", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"Remove {self.target}", r.stdout)
        self.assertFalse(fetch_log.exists())
        self.assertFalse(self.bindir.exists())
        self.assertEqual(self.calls(), [])
        self.assert_tmp_empty()

    # --- refusals ----------------------------------------------------------------------

    def test_root_refused(self):
        r = self.run_script(env=self.env(FAKE_UID="0"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("refusing to run as root", r.stderr)
        self.assertFalse(self.bindir.exists())
        self.assert_tmp_empty()
        r = self.run_script("--uninstall", env=self.env(FAKE_UID="0"))
        self.assertEqual(r.returncode, 1)
        r = self.run_script("--allow-root", env=self.env(FAKE_UID="0"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.target.is_file())

    def test_other_platform_refused(self):
        for os_name, arch in (("Linux", "aarch64"), ("Darwin", "x86_64"), ("Linux", "i686")):
            r = self.run_script(env=self.env(FAKE_OS=os_name, FAKE_ARCH=arch))
            self.assertEqual(r.returncode, 1)
            self.assertIn(f"this is {os_name} {arch}", r.stderr)
            r = self.run_script("--run", "cli", env=self.env(FAKE_OS=os_name, FAKE_ARCH=arch))
            self.assertEqual(r.returncode, 1)
            self.assertFalse(self.bindir.exists())
            self.assertEqual(self.calls(), [])
            self.assert_tmp_empty()

    def test_bad_options(self):
        for args in (["--frobnicate"], ["extra"], ["--version"], ["--version", "latest"],
                     ["--version", "v1.2.3;rm"], ["--dir"], ["--dir="],
                     ["--uninstall", "--version", VERSION]):
            r = self.run_script(*args)
            self.assertEqual(r.returncode, 2, args)
            self.assertIn("error:", r.stderr)
        self.assertFalse(self.bindir.exists())
        self.assert_tmp_empty()

    def test_help(self):
        r = self.run_script("--help")
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.startswith("Usage: sh install.sh [OPTIONS]"))
        for word in ("--version", "--dir", "--desktop-entry", "--uninstall", "--run",
                     "--dry-run", "--allow-root", "GITFURL_BASE_URL"):
            self.assertIn(word, r.stdout)


if __name__ == "__main__":
    unittest.main()
