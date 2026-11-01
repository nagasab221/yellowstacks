#!/bin/bash
# Build the Yellowstacks home screen APK, straight from the SDK tools (no Gradle,
# so there is nothing to download and nothing to keep up to date).
#   launcher/build.sh [--install]
# --install also installs it into the running emulator and makes it the home app.
set -euo pipefail
cd "$(dirname "$0")"

SDK="${ANDROID_HOME:-$HOME/Android/Sdk}"
API=33
BT=$(ls "$SDK/build-tools" 2>/dev/null | sort -V | tail -1)
JAR="$SDK/platforms/android-$API/android.jar"
TOOLS="$SDK/build-tools/$BT"
# The d8 that ships with build tools 34 predates Java 21 and dies on the class
# files this JDK writes, so use a current one from Google's Maven (fetch.sh).
D8JAR=tools/r8.jar
OUT=build
APK=$OUT/yellowstacks-home.apk
KEYSTORE=$OUT/debug.keystore

[ -f "$D8JAR" ] || ./fetch.sh

for need in "$JAR" "$TOOLS/aapt2" "$TOOLS/zipalign" "$TOOLS/apksigner" "$D8JAR"; do
  [ -e "$need" ] || { echo "missing $need - install the SDK build tools and platform $API" >&2; exit 1; }
done
command -v javac >/dev/null || { echo "javac not found - install a JDK" >&2; exit 1; }

rm -rf $OUT
mkdir -p $OUT/res $OUT/gen $OUT/classes

echo "== resources"
"$TOOLS/aapt2" compile --dir res -o $OUT/res.zip
"$TOOLS/aapt2" link -o $OUT/base.apk -I "$JAR" \
  --manifest AndroidManifest.xml $OUT/res.zip --java $OUT/gen \
  --min-sdk-version 29 --target-sdk-version $API

echo "== java"
javac -nowarn -source 11 -target 11 -classpath "$JAR" -d $OUT/classes \
  $(find src $OUT/gen -name '*.java') 2>&1 | grep -v 'bootstrap class path' || true

echo "== dex"
java -cp "$D8JAR" com.android.tools.r8.D8 --lib "$JAR" --min-api 29 \
  --output $OUT $(find $OUT/classes -name '*.class')

echo "== package"
cp $OUT/base.apk $OUT/unsigned.apk
(cd $OUT && zip -q -u unsigned.apk classes.dex)
"$TOOLS/zipalign" -f 4 $OUT/unsigned.apk $OUT/aligned.apk

if [ ! -f "$KEYSTORE" ]; then
  keytool -genkeypair -keystore "$KEYSTORE" -alias ys -storepass yellowstacks \
    -keypass yellowstacks -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=Yellowstacks, OU=Home, O=Yellowstacks, C=US" >/dev/null 2>&1
fi
"$TOOLS/apksigner" sign --ks "$KEYSTORE" --ks-pass pass:yellowstacks \
  --key-pass pass:yellowstacks --out "$APK" $OUT/aligned.apk
rm -f $OUT/unsigned.apk $OUT/aligned.apk $OUT/base.apk $OUT/res.zip
echo "built $APK"

if [ "${1:-}" = "--install" ]; then
  ADB="$SDK/platform-tools/adb"
  echo "== install"
  "$ADB" install -r -g "$APK"
  "$ADB" shell cmd package set-home-activity \
    com.yellowstacks.launcher/com.yellowstacks.launcher.HomeActivity
  echo "installed and set as the home app"
fi
