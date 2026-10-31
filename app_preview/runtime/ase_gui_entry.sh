#!/bin/sh
# Starts a virtual X screen with a small desktop (window manager), then runs the given command on it. Used by the App Preview panel for GUI apps.
# Needs Xvfb in the image (see app_preview/images/Dockerfile.gui); openbox gives the machine a real desktop: title bars, moving /
# resizing / closing windows, dialogs in front of their parent. Without them the program still runs, its window just sits at the top-left corner.
# Usage: ase_gui_entry.sh COMMAND [ARGS...]
SCREEN="${ASE_SCREEN:-800x600x24}"
DISPLAY="${DISPLAY:-:99}"
export DISPLAY
if ! command -v Xvfb >/dev/null 2>&1; then
  echo "[preview] Xvfb is not installed in this image. Build the preview GUI image (app_preview/images/Dockerfile.gui)." >&2
  exit 127
fi
Xvfb "$DISPLAY" -screen 0 "$SCREEN" -nolisten tcp >/tmp/xvfb.log 2>&1 &
XPID=$!
i=0
while [ ! -S "/tmp/.X11-unix/X${DISPLAY#:}" ] && [ $i -lt 100 ]; do
  i=$((i+1)); sleep 0.1
done
DESK=none
WMPID=""
if [ -z "$ASE_NO_DESKTOP" ] && command -v openbox >/dev/null 2>&1; then
  openbox >/tmp/openbox.log 2>&1 &
  WMPID=$!
  DESK=openbox
  sleep 0.5
fi
echo "$DESK" >/tmp/ase_desktop 2>/dev/null
"$@"
CODE=$?
[ -n "$WMPID" ] && kill "$WMPID" 2>/dev/null
kill "$XPID" 2>/dev/null
exit $CODE
