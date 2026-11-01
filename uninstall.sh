#!/usr/bin/env bash
# Yellowstacks uninstaller - removes the app, the commands, the menu entry and
# the file associations.  Your virtual phone, your apps inside it and the
# Android SDK are left alone.
#   curl -fsSL https://raw.githubusercontent.com/nagasab221/yellowstacks/main/uninstall.sh | bash
# Add --purge to also delete your settings and keyboard controls.
set -euo pipefail

DEST="${YS_DIR:-$HOME/.local/share/yellowstacks}"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
MIME="$HOME/.local/share/mime"
PURGE=0
for a in "$@"; do [ "$a" = "--purge" ] && PURGE=1; done

say() { printf '\033[1;33m==>\033[0m %s\n' "$*"; }

# shut Android down if Yellowstacks is running
if command -v yellowstacks >/dev/null 2>&1; then
  say "Shutting Android down if it is running"
  yellowstacks stop >/dev/null 2>&1 || true
  sleep 2
fi

say "Removing the yellowstacks and mlbb commands"
rm -f "$BIN/yellowstacks" "$BIN/mlbb"

say "Removing the menu entry and the file associations"
rm -f "$APPS/yellowstacks.desktop" "$MIME/packages/yellowstacks.xml"
update-mime-database "$MIME" >/dev/null 2>&1 || true
update-desktop-database "$APPS" >/dev/null 2>&1 || true
# hand .apk files back to the archive manager, if it is installed
if [ -f /usr/share/applications/org.gnome.FileRoller.desktop ]; then
  xdg-mime default org.gnome.FileRoller.desktop \
    application/vnd.android.package-archive >/dev/null 2>&1 || true
fi

if [ -d "$DEST" ]; then
  say "Removing $DEST"
  rm -rf "$DEST"
fi

if [ "$PURGE" = 1 ]; then
  say "Removing your settings and keyboard controls"
  rm -rf "$HOME/.config/yellowstacks" "$HOME/.cache/yellowstacks"
else
  echo
  echo "Your settings and keyboard controls were kept in ~/.config/yellowstacks"
  echo "Run with --purge to delete those too."
fi

echo
echo "Left alone on purpose: the MLBB2 virtual phone (~/.android/avd),"
echo "everything installed inside it, and the Android SDK (~/Android/Sdk)."
say "Uninstalled."
