#!/bin/bash
# Run the UI tests in a throwaway nested X server (Xephyr), so they never touch
# the real desktop, mouse or keyboard.   usage: tests/run-ui-test.sh [game-screenshot.png]
#   tests/ui_test.py    side bar, controls, editor, settings, installing
#   tests/boot_test.py  the loading screen
cd "$(dirname "$0")/.."
Xephyr :9 -screen 1400x900 -ac -br -noreset >/dev/null 2>&1 &
XPID=$!
trap 'kill $XPID 2>/dev/null' EXIT
sleep 1.5
status=0
DISPLAY=:9 timeout 120 .venv/bin/python tests/ui_test.py "${YS_SHOTS:-}" "${1:-}" 2>&1 \
  | grep -vE '^controls |^Yellowstacks|^profile'
[ "${PIPESTATUS[0]}" = 0 ] || status=1
echo
DISPLAY=:9 timeout 60 .venv/bin/python tests/boot_test.py "${YS_SHOTS:-}" 2>&1
[ "${PIPESTATUS[0]}" = 0 ] || status=1
exit $status
