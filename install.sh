#!/usr/bin/env bash
# Yellowstacks installer - puts Yellowstacks in your menu and adds the
# `yellowstacks` and `mlbb` commands.  Safe to run again to update.
#   curl -fsSL https://raw.githubusercontent.com/nagasab221/yellowstacks/main/install.sh | bash
set -euo pipefail

REPO_URL="${YS_REPO:-https://github.com/nagasab221/yellowstacks.git}"
DEST="${YS_DIR:-$HOME/.local/share/yellowstacks}"
SDK="${ANDROID_SDK_ROOT:-$HOME/Android/Sdk}"

say()  { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;35m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- checks
command -v git     >/dev/null || die "git is needed:      sudo apt install git"
command -v python3 >/dev/null || die "python3 is needed:  sudo apt install python3"
[ "${XDG_SESSION_TYPE:-x11}" = "wayland" ] &&
  warn "You are on Wayland. Yellowstacks needs X11; log in with an Xorg session."

# ---------------------------------------------------------------- the code
if [ -d "$DEST/.git" ]; then
  say "Updating $DEST"
  git -C "$DEST" pull --ff-only --quiet
else
  say "Downloading Yellowstacks to $DEST"
  mkdir -p "$(dirname "$DEST")"
  git clone --depth 1 --quiet "$REPO_URL" "$DEST"
fi

# ---------------------------------------------------------------- python
if [ ! -x "$DEST/.venv/bin/python" ]; then
  say "Making a Python environment"
  python3 -m venv "$DEST/.venv" 2>/dev/null ||
    die "Could not make it. Install the tool first:  sudo apt install python3-venv"
fi
say "Installing the Python packages it needs"
"$DEST/.venv/bin/pip" install --quiet --upgrade pip
"$DEST/.venv/bin/pip" install --quiet grpcio protobuf pillow python-xlib

# ------------------------------------------------- menu entry + commands
say "Adding the menu entry, the commands and the APK file associations"
bash "$DEST/install-desktop.sh" >/dev/null
say "Installed."

# ---------------------------------------------------------------- after
echo
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) warn "$HOME/.local/bin is not in your PATH. Add this to ~/.bashrc:"
     echo '      export PATH="$HOME/.local/bin:$PATH"' ;;
esac

missing=0
[ -x "$SDK/emulator/emulator" ]      || { warn "The Android emulator was not found in $SDK"; missing=1; }
[ -x "$SDK/platform-tools/adb" ]     || { warn "adb was not found in $SDK"; missing=1; }
[ -d "$HOME/.android/avd/MLBB2.avd" ] || { warn "The MLBB2 virtual phone does not exist yet"; missing=1; }

if [ "$missing" = 1 ]; then
  cat <<EOF

Yellowstacks itself is installed, but it still needs a virtual phone to run.
That part needs the Android SDK and Intel's ARM translation, which cannot be
downloaded for you. See "Setting up the virtual phone" in the README:
  https://github.com/nagasab221/yellowstacks#setting-up-the-virtual-phone
EOF
else
  echo "Start it from your menu, or run:  yellowstacks"
fi
