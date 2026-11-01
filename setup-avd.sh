#!/bin/bash
# Build an Android 13 emulator (Google APIs image, rootable) with Intel's libhoudini
# ARM translation, so arm64-only games can run.
#   setup-avd.sh <houdini-prebuilts-dir> [apk-dir]
# houdini-prebuilts-dir: the "prebuilts" folder from
#   github.com/supremegamers/vendor_intel_proprietary_houdini (Android 13 commit 2f8f088)
# apk-dir: optional folder of split APKs to install afterwards (adb install-multiple)
set -euo pipefail
HOUDINI=${1:?houdini prebuilts dir}
APKS=${2:-}
AVD=${AVD:-MLBB2}
SDK="$HOME/Android/Sdk"
ADB="$SDK/platform-tools/adb"
IMAGE="system-images;android-33;google_apis;x86_64"
LOG="$HOME/.cache/yellowstacks/setup-$AVD.log"
mkdir -p "$(dirname "$LOG")"

wait_boot() {
  timeout 240 "$ADB" wait-for-device
  for _ in $(seq 1 100); do
    [ "$("$ADB" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = 1 ] && return 0
    sleep 3
  done
  echo "Android didn't finish booting" >&2; return 1
}
root_remount() { "$ADB" root >/dev/null; sleep 3; "$ADB" wait-for-device; "$ADB" remount; }

echo "== creating AVD $AVD"
echo no | "$SDK/cmdline-tools/latest/bin/avdmanager" create avd -n "$AVD" -k "$IMAGE" -d pixel_6 --force >/dev/null
python3 - "$HOME/.android/avd/$AVD.avd/config.ini" "$AVD" <<'EOF'
import sys
p, name = sys.argv[1], sys.argv[2]
want = {"PlayStore.enabled": "no", "avd.id": name, "avd.name": name, "hw.ramSize": "4096",
        "hw.cpu.ncore": "6", "disk.dataPartition.size": "24G", "hw.gpu.enabled": "yes",
        "hw.gpu.mode": "host", "hw.keyboard": "yes", "hw.lcd.width": "1920", "hw.lcd.height": "1080",
        "hw.lcd.density": "400", "hw.initialOrientation": "landscape", "showDeviceFrame": "no",
        "vm.heapSize": "512", "hw.camera.back": "none", "hw.audioInput": "no",
        "hw.mainKeys": "yes",   # hardware keys: Android shows no navigation bar
        "hw.device.name": "yellowstacks"}  # not pixel_6: no camera-hole overlay
out, seen = [], set()
for line in open(p).read().splitlines():
    k = line.split("=", 1)[0].strip()
    if k == "disk.dataPartition.path":
        continue
    if k in want:
        out.append(f"{k}={want[k]}"); seen.add(k)
    else:
        out.append(line)
out += [f"{k}={v}" for k, v in want.items() if k not in seen]
open(p, "w").write("\n".join(out) + "\n")
EOF

echo "== first boot (headless, writable system)"
"$SDK/emulator/emulator" -avd "$AVD" -writable-system -no-window -gpu swiftshader_indirect \
  -no-audio -no-boot-anim -no-snapshot >"$LOG" 2>&1 &
EMU=$!
wait_boot
root_remount
"$ADB" reboot; sleep 5; wait_boot
root_remount

echo "== installing libhoudini"
"$ADB" push "$HOUDINI/lib64/libhoudini.so" /system/lib64/ >/dev/null
"$ADB" push "$HOUDINI/lib64/arm64" /system/lib64/ >/dev/null
"$ADB" push "$HOUDINI/bin/houdini64" /system/bin/ >/dev/null
"$ADB" push "$HOUDINI/etc/binfmt_misc" /system/etc/ >/dev/null
# native bridge bind-mounts this over /proc/cpuinfo for ARM apps
"$ADB" push "$HOUDINI/lib64/arm64/cpuinfo" /system/etc/cpuinfo.arm64.txt >/dev/null
"$ADB" shell "cat > /system/etc/init/houdini.rc" <<'EOF'
# Intel libhoudini ARM64 translation (files in /system)
on early-init
    mount binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc

on property:ro.enable.native.bridge.exec64=1
    copy /system/etc/binfmt_misc/arm64_exe /proc/sys/fs/binfmt_misc/register
    copy /system/etc/binfmt_misc/arm64_dyn /proc/sys/fs/binfmt_misc/register
EOF
"$ADB" shell 'chmod 755 /system/bin/houdini64; chmod 644 /system/etc/init/houdini.rc /system/lib64/libhoudini.so /system/etc/cpuinfo.arm64.txt;
  restorecon -R /system/lib64/arm64 /system/lib64/libhoudini.so /system/bin/houdini64 /system/etc/binfmt_misc /system/etc/init/houdini.rc /system/etc/cpuinfo.arm64.txt'
# This image is 64-bit only: leave abilist32 empty (a non-empty value triggers a
# 32-bit boringssl self-test that doesn't exist and reboot-loops the device).
"$ADB" shell "sed -i 's/^ro.dalvik.vm.native.bridge=0\$/ro.dalvik.vm.native.bridge=libhoudini.so/' /system/build.prop"
"$ADB" shell "cat >> /system/build.prop" <<'EOF'
# ARM64 translation (libhoudini)
ro.product.cpu.abilist=x86_64,arm64-v8a
ro.product.cpu.abilist64=x86_64,arm64-v8a
ro.enable.native.bridge.exec64=1
ro.dalvik.vm.isa.arm64=x86_64
EOF

echo "== reboot with translation"
"$ADB" reboot; sleep 5; wait_boot
root_remount >/dev/null

# libhoudini dlopens /system/lib64/arm64/libtcb.so, but the default linker namespace
# on this image only permits a few /system/lib64 subfolders, so ARM apps abort.
# The linker config is regenerated every boot, so keep a patched copy in /system
# and copy it over the generated one before zygote starts.
echo "== allowing libhoudini's helper folder in the linker config"
TMPCFG=$(mktemp)
"$ADB" pull /linkerconfig/ld.config.txt "$TMPCFG" >/dev/null
sed -i 's|^namespace.default.permitted.paths = /system/${LIB}/drm$|&\nnamespace.default.permitted.paths += /system/${LIB}/arm64|' "$TMPCFG"
grep -qF 'permitted.paths += /system/${LIB}/arm64' "$TMPCFG" || { echo "couldn't patch linker config" >&2; exit 1; }
"$ADB" push "$TMPCFG" /system/etc/ld.config.houdini.txt >/dev/null
rm -f "$TMPCFG"
"$ADB" shell "cat >> /system/etc/init/houdini.rc" <<'EOF'

# libhoudini dlopens helpers from /system/lib64/arm64, which the default linker
# namespace doesn't permit on this image. Swap in a linker config that allows it,
# after perform_apex_config (init.rc, post-fs-data) and before zygote starts.
on post-fs-data
    copy /system/etc/ld.config.houdini.txt /linkerconfig/ld.config.txt
    chmod 0644 /linkerconfig/ld.config.txt
EOF
"$ADB" shell 'chmod 644 /system/etc/ld.config.houdini.txt; restorecon /system/etc/ld.config.houdini.txt; sync'
"$ADB" reboot; sleep 5; wait_boot
# the pixel_6 profile adds a camera-hole overlay that letterboxes landscape games
"$ADB" shell cmd overlay disable com.android.internal.emulation.pixel_6 || true
echo "abilist:       $("$ADB" shell getprop ro.product.cpu.abilist | tr -d '\r')"
echo "native bridge: $("$ADB" shell getprop ro.dalvik.vm.native.bridge | tr -d '\r')"
echo "linker fix:    $("$ADB" shell grep -c 'lib64/arm64' /linkerconfig/ld.config.txt | tr -d '\r') line(s)"

if [ -n "$APKS" ]; then
  echo "== installing APKs from $APKS"
  "$ADB" install-multiple "$APKS"/*.apk
fi

echo "== shutting down (start it later with -writable-system to keep the changes)"
# flush and power off properly; killing the emulator straight away loses fresh installs
"$ADB" shell sync
"$ADB" shell reboot -p >/dev/null 2>&1 || true
for _ in $(seq 1 60); do kill -0 "$EMU" 2>/dev/null || break; sleep 2; done
kill -0 "$EMU" 2>/dev/null && "$ADB" emu kill >/dev/null 2>&1 || true
wait "$EMU" 2>/dev/null || true
echo done

# strip the new phone back to what games need (removable apps, dark mode, wallpaper)
"$(dirname "$(readlink -f "$0")")/.venv/bin/python" \
  "$(dirname "$(readlink -f "$0")")/yellowstacks.py" customize || true
