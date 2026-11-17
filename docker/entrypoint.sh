#!/bin/sh
# Starts a virtual display when HEADLESS=1, then runs gitfurl with the given arguments.
set -eu
mkdir -p "$HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME"
if [ "${HEADLESS:-0}" = 1 ]; then
  Xvfb :99 -screen 0 1280x800x24 >/dev/null 2>&1 &
  export DISPLAY=:99
  sleep 1
fi
exec python3 -m gitfurl "$@"
