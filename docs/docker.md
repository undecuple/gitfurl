# Running gitfurl in a container

Status: **experimental, started 2026-11-16**. The image builds and starts. No installer has been run in it. See "What has been tested".

## What it is for
A container gives you a second layer around a Windows installer: its own filesystem, no network, your installer folder mounted read-only, and prefixes and logs kept in a volume instead of your home folder.

## What it does not do
- It is **not** a security guarantee. A container shares your kernel. The installer is a Windows program run through Wine as an ordinary user inside it; treat an untrusted installer as untrusted.
- Wine prefixes are still not sandboxes. The container is what limits the damage.
- Nothing in the image includes an installer, game files or keys. `.dockerignore` excludes them.

## Use
Docker or Podman. Set `INSTALLER_DIR` to the folder that holds your installer.

    INSTALLER_DIR=$HOME/Downloads/my-installer docker compose build
    INSTALLER_DIR=$HOME/Downloads/my-installer docker compose run --rm cli doctor
    INSTALLER_DIR=$HOME/Downloads/my-installer docker compose run --rm cli install \
        /installer/setup.exe --prefix demo --create-prefix --progress

The default service (`cli`) uses a virtual display, so the installer's window is not shown. It is useful for checking that an installer starts, with progress and logs. To use the real window:

    INSTALLER_DIR=... docker compose --profile gui run --rm gui

The `gui` service mounts your X11 socket. That lets a program in the container see and control your other windows. Use it only with software you are willing to run that way.

## Settings in the compose file
- `network_mode: none`: no network at all, including for the installer.
- `read_only: true`, `tmpfs: /tmp`: the container's filesystem cannot be changed.
- `cap_drop: ALL` and `no-new-privileges`: no extra privileges.
- `/installer` is read-only. `/work` is a volume holding prefixes, logs and the home folder.

## Limits
- The image has Wine only. Proton and umu-launcher are not in it.
- Wayland-only sessions need XWayland for the `gui` service.
- The image is large (Wine, 32-bit libraries).

## What has been tested
Checked on 2026-11-16 with Podman 6.1 (rootless), not Docker, using the same options as `compose.yaml` written out as `podman run` flags (network none, read-only root, all capabilities dropped, no-new-privileges):
- The image builds (Debian 13, Wine 10.0 from Debian, about 2.1 GB).
- `cli doctor` runs and finds Wine.
- The window opens under the virtual display and stays open for 5 seconds.

Not tested: `docker compose` itself (Docker is not installed here), the `gui` service with a real X11 socket, Wayland, and running any installer in the container.
