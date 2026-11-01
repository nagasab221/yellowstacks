#!/bin/bash
# Fetch the dexer the build needs. The d8 that ships with SDK build tools 34 (the
# newest sdkmanager offers) predates Java 21: it can't read the class files this
# JDK writes ("Unsupported class file major version 65") and dies with a
# NullPointerException on inner classes even when told to target Java 8.
set -euo pipefail
cd "$(dirname "$0")"
VERSION=${1:-9.5.28}
mkdir -p tools
if [ -f tools/r8.jar ]; then
  echo "tools/r8.jar is already here (delete it to fetch again)"
  exit 0
fi
URL="https://dl.google.com/dl/android/maven2/com/android/tools/r8/$VERSION/r8-$VERSION.jar"
echo "fetching r8 $VERSION"
curl -fSL --retry 3 -o tools/r8.jar "$URL"
java -cp tools/r8.jar com.android.tools.r8.D8 --version
