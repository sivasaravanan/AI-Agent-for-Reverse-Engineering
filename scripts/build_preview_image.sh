#!/bin/sh
# Builds the GUI preview image (Xvfb + Tk) used by the App Preview panel. Needs a container runtime and internet.
# Same as the panel's "Prepare GUI preview image" button. Asks you to approve the exact command first.
cd "$(dirname "$0")/.." || exit 1
exec python3 -m app_preview --build-gui-image "$@"
