#!/bin/sh
# Run a command with no network, an empty home and the installer folder mounted read-only.
# usage: tools/sandbox.sh INSTALLER_DIR WORKDIR command [args...]
# WORKDIR is the only writable place (prefix, logs). Delete it afterwards.
set -eu
if [ ! -d /nix ] || [ ! -d /run/current-system ]; then
  echo "tools/sandbox.sh only supports NixOS today (it binds /nix and /run/current-system)." >&2
  exit 1
fi
src=$1; work=$2; shift 2
mkdir -p "$work/home" "$work/data" "$work/state"
# SANDBOX_ETC=1 also shows a few read-only /etc files (needed by umu/Proton's container).
extra=""
if [ "${SANDBOX_ETC:-0}" = 1 ]; then
  extra="--ro-bind /etc/zoneinfo /etc/zoneinfo --symlink zoneinfo/UTC /etc/localtime \
    --ro-bind /etc/passwd /etc/passwd --ro-bind /etc/group /etc/group \
    --ro-bind /etc/machine-id /etc/machine-id --ro-bind /etc/hosts /etc/hosts --ro-bind /sys /sys"
fi
# shellcheck disable=SC2086
exec bwrap --unshare-all --die-with-parent --new-session \
  --ro-bind /nix /nix --ro-bind /run/current-system /run/current-system \
  --ro-bind /etc/fonts /etc/fonts --symlink /run/current-system/sw/bin/sh /bin/sh --proc /proc --dev /dev --tmpfs /tmp --tmpfs /var/tmp --tmpfs /var/lib/xkb \
  --ro-bind "$src" /installer --bind "$work" /work \
  --unsetenv DBUS_SESSION_BUS_ADDRESS --unsetenv XDG_RUNTIME_DIR --unsetenv DISPLAY --unsetenv WAYLAND_DISPLAY --setenv HOME /work/home --setenv XDG_DATA_HOME /work/data \
  --setenv XDG_STATE_HOME /work/state $extra --chdir /work "$@"
