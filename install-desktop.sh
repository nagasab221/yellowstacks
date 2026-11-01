#!/bin/bash
# Put Yellowstacks in the menu and make it open Android app files, so double-clicking
# an .apk / .xapk / .apks / .apkm in the file manager installs it.
#   install-desktop.sh            set it all up (safe to run again)
# Undo the file association: xdg-mime default org.gnome.FileRoller.desktop application/vnd.android.package-archive
set -eu
HERE="$(dirname "$(readlink -f "$0")")"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
MIME="$HOME/.local/share/mime"
mkdir -p "$BIN" "$APPS" "$MIME/packages"
ln -sf "$HERE/yellowstacks" "$BIN/yellowstacks"
ln -sf "$HERE/yellowstacks" "$BIN/mlbb"

cat > "$APPS/yellowstacks.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Yellowstacks
Comment=Android games and apps, with keyboard controls
Exec=$BIN/yellowstacks %F
Icon=$HERE/yellowstacks.png
Terminal=false
Categories=Game;
StartupWMClass=qemu-system-x86_64
MimeType=application/vnd.android.package-archive;application/x-android-app-bundle;
Actions=mlbb;install;settings;stop;

[Desktop Action mlbb]
Name=Mobile Legends
Exec=$BIN/mlbb

[Desktop Action install]
Name=Install an app (APK)…
Exec=$BIN/yellowstacks install

[Desktop Action settings]
Name=Settings
Exec=$BIN/yellowstacks settings

[Desktop Action stop]
Name=Shut down Android
Exec=$BIN/yellowstacks stop
EOF

# .apk is already known to the desktop; the bundle formats aren't (they're zip files)
cat > "$MIME/packages/yellowstacks.xml" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">
  <mime-type type="application/x-android-app-bundle">
    <comment>Android app bundle</comment>
    <sub-class-of type="application/zip"/>
    <generic-icon name="package-x-generic"/>
    <glob pattern="*.xapk"/>
    <glob pattern="*.apks"/>
    <glob pattern="*.apkm"/>
  </mime-type>
</mime-info>
EOF
update-mime-database "$MIME" >/dev/null 2>&1 || true
update-desktop-database "$APPS" >/dev/null 2>&1 || true
xdg-mime default yellowstacks.desktop application/vnd.android.package-archive application/x-android-app-bundle
echo "Yellowstacks is in the menu and opens .apk / .xapk / .apks / .apkm files."
