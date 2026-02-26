#!/bin/bash
set -e

# Start Xvfb for headless Chromium
Xvfb :99 -screen 0 1280x800x24 -ac &
sleep 1
export DISPLAY=:99

# Run the worker
exec python -u -m worker.main
