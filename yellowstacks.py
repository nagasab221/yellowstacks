#!/usr/bin/env python3
"""Yellowstacks - Android games and apps in Google's Android emulator on Linux (X11).

Starting: a loading screen shows where the game window will be while Android
starts (the emulator's own window stays minimised until Android is ready), then
the home screen, or an app chosen in Settings → Startup (`mlbb` opens Mobile
Legends). The home screen is Yellowstacks' own (launcher/, installed by
`customize`): a real Android home app that lists everything you can open, so
Home and Recent apps behave exactly as Android means them to.

Replaces the emulator's side bar with its own, docked to the game window:
  minimise / close (shuts Android down cleanly), power, volume, rotate, back,
  home, recent apps, screenshot, zoom (window sizes, full screen), install an
  app (APK), edit controls, controls on/off, settings, and a "more" menu.

Settings: CPU cores, memory, frame rate, VSync, resolution and DPI (used when the
emulator starts; Android restarts for them), the keyboard controls, apps (install / open / remove),
what opens at start, and Android options.

Keyboard controls: while the game window is focused and the game is in front,
mapped keys become touches through the emulator's gRPC API. Two kinds of control:
  - joystick: four keys push a virtual stick (e.g. WASD -> movement)
  - tap:      one key holds a finger on a spot on the screen while it's down
Mouse clicks still go to the game. While Android has a text box open (login,
chat) the keys are left alone so typing works, and Ctrl+V pastes the PC
clipboard. ` turns the controls on/off, F11 toggles full screen.

  yellowstacks.py run      [--profile mlbb] [--open PACKAGE]   start everything
  yellowstacks.py install  [FILES]     install .apk / .xapk / .apks / .apkm files
  yellowstacks.py settings [--page P]  the settings, without Android running
  yellowstacks.py open     PACKAGE     open an app in the running Android
  yellowstacks.py setup    [--profile mlbb]   just the controls editor
  yellowstacks.py customize [--restore]   strip Android back to what games need:
                           remove the apps it doesn't use, dark mode, Yellowstacks
                           wallpaper (--restore puts the removed apps back)
(Use the `yellowstacks` launcher; it hands requests to a running Yellowstacks.)
"""
import argparse
import copy
import datetime
import io
import json
import math
import os
import random
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "emu_proto"))

import grpc  # noqa: E402
import emulator_controller_pb2 as pb  # noqa: E402
import emulator_controller_pb2_grpc as pb_grpc  # noqa: E402

APP = "Yellowstacks"
VERSION = "2026.11"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "yellowstacks"
LOG_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "yellowstacks"
ICON = HERE / "yellowstacks.png"
GRPC_ADDR = os.environ.get("EMU_GRPC", "localhost:8554")
GRPC_PORT = GRPC_ADDR.rsplit(":", 1)[-1]
SDK = Path(os.environ.get("ANDROID_SDK_ROOT", Path.home() / "Android/Sdk"))
ADB = os.environ.get("ADB", str(SDK / "platform-tools/adb"))
EMULATOR = str(SDK / "emulator/emulator")
AVD = os.environ.get("YS_AVD", "MLBB2")
TICK_MS = 8  # how often keys/fingers are processed
GAME = "com.mobile.legends"
GAME_NAME = "Mobile Legends"

DIRS = {"up": (0, -1), "left": (-1, 0), "down": (0, 1), "right": (1, 0)}

# Starting layout for Mobile Legends at 1920x1080 (device pixels). Edit it with the
# side bar's keyboard button.
DEFAULT_PROFILE = {
    "version": 2,
    "package": GAME,        # the controls only act while this app is in front
    "device_size": [1920, 1080],
    "toggle_key": "grave",  # ` turns the controls on/off
    "controls": [
        {"type": "joystick", "keys": {"up": "w", "left": "a", "down": "s", "right": "d"},
         "pos": [300, 820], "radius": 130},
        {"type": "tap", "key": "space", "pos": [1720, 900]},
        {"type": "tap", "key": "q", "pos": [1460, 950]},
        {"type": "tap", "key": "e", "pos": [1530, 770]},
        {"type": "tap", "key": "r", "pos": [1700, 660]},
        {"type": "tap", "key": "f", "pos": [1300, 970]},
        {"type": "tap", "key": "b", "pos": [880, 1010]},
        {"type": "tap", "key": "c", "pos": [1000, 1010]},
    ],
}


# ---------------------------------------------------------------- profiles

def profile_path(name):
    return CONFIG_DIR / f"{name}.json"


def migrate(prof):
    """Turn the first profile format (one joystick + "buttons") into a controls list."""
    if "controls" in prof:
        return prof
    controls = []
    joy = prof.get("joystick")
    if joy:
        controls.append({"type": "joystick", "keys": joy["keys"], "pos": joy["pos"],
                         "radius": joy.get("radius", 130)})
    for b in prof.get("buttons", []):
        controls.append({"type": "tap", "key": b["key"], "pos": b["pos"]})
    return {"version": 2, "device_size": prof.get("device_size", [1920, 1080]),
            "toggle_key": prof.get("toggle_key", "grave"), "controls": controls}


def load_profile(name):
    path = profile_path(name)
    if not path.exists():
        save_profile(name, DEFAULT_PROFILE)
    prof = json.loads(path.read_text())
    if "controls" not in prof:
        prof = migrate(prof)
        save_profile(name, prof)
    if "package" not in prof and name == "mlbb":  # made before controls knew their game
        prof["package"] = GAME
        save_profile(name, prof)
    return prof


def save_profile(name, prof):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = profile_path(name).with_suffix(".tmp")
    tmp.write_text(json.dumps(prof, indent=2))
    tmp.replace(profile_path(name))


def used_keys(prof, skip=None):
    """key name -> description, for every key the profile uses (except control `skip`)."""
    out = {prof.get("toggle_key", "grave"): "the on/off key", "F11": "full screen"}
    for c in prof["controls"]:
        if c is skip:
            continue
        if c["type"] == "joystick":
            for d, k in c["keys"].items():
                out[k] = "a joystick"
        else:
            out[c["key"]] = "another tap"
    return out


def key_label(name):
    special = {"space": "SPC", "grave": "`", "Tab": "TAB", "Return": "ENT", "BackSpace": "BKSP",
               "Up": "↑", "Down": "↓", "Left": "←", "Right": "→", "Shift_L": "SHIFT",
               "Shift_R": "SHIFT", "Control_L": "CTRL", "Control_R": "CTRL", "Alt_L": "ALT",
               "Caps_Lock": "CAPS", "minus": "-", "equal": "=", "comma": ",", "period": ".",
               "slash": "/", "semicolon": ";", "apostrophe": "'", "bracketleft": "[",
               "bracketright": "]", "backslash": "\\"}
    if name in special:
        return special[name]
    if name.startswith("KP_"):
        return "N" + name[3:]
    return name.upper() if len(name) == 1 else name[:5]


# ---------------------------------------------------------------- settings

SETTINGS_PATH = CONFIG_DIR / "settings.json"
RESOLUTIONS = [(1280, 720), (1600, 900), (1920, 1080), (2560, 1440)]
DPIS = [240, 320, 400, 480]
AUTO_DPI = {720: 280, 900: 320, 1080: 400, 1440: 560}  # Android ~430 dp tall at each size
MAX_CORES = 6               # the emulator ignores more: "does not support more than 6 cores"
FPS_RANGE = (30, 120, 5)    # lowest, highest, step of the frame-rate slider
DEFAULT_SETTINGS = {
    "cpu_cores": 6,
    "ram_mb": 4096,
    "fps": 120,                 # Android's screen refresh rate = the most a game can draw
    "vsync": False,             # sync the emulator's window to the PC monitor
    "resolution": [1920, 1080],
    "dpi": 0,                   # 0 = automatic for the resolution
    "start_app": "",            # package to open once Android has started; "" = home screen
    "controls_on_start": True,
    "remember_window": True,
    "show_emulator_bar": False,
}
# the emulator reads these when it starts: changing them needs it to start again
HARDWARE_SETTINGS = ("cpu_cores", "ram_mb", "fps", "vsync", "resolution", "dpi")


def _clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def load_settings():
    try:
        saved = json.loads(SETTINGS_PATH.read_text())
    except (OSError, ValueError):
        saved = {}
    s = {**DEFAULT_SETTINGS, **{k: v for k, v in saved.items() if k in DEFAULT_SETTINGS}}
    s["cpu_cores"] = _clamp(s["cpu_cores"], 1, MAX_CORES, DEFAULT_SETTINGS["cpu_cores"])
    s["fps"] = _clamp(s["fps"], FPS_RANGE[0], FPS_RANGE[1], DEFAULT_SETTINGS["fps"])
    return s


def save_settings(s):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2))
    tmp.replace(SETTINGS_PATH)


def dpi_for(s):
    return s["dpi"] or AUTO_DPI.get(s["resolution"][1], 400)


def host_ram_mb():
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return 8192


def avd_dir():
    home = Path(os.environ.get("ANDROID_AVD_HOME", Path.home() / ".android" / "avd"))
    try:
        conf = dict(line.split("=", 1) for line in (home / f"{AVD}.ini").read_text().splitlines() if "=" in line)
        return Path(conf["path"].strip())
    except (OSError, KeyError):
        return home / f"{AVD}.avd"


def apply_avd_config(s):
    """Write the hardware settings into the AVD's config.ini, which the emulator reads
    when it starts. Returns True if anything changed."""
    w, h = s["resolution"]
    want = {"hw.cpu.ncore": s["cpu_cores"], "hw.ramSize": s["ram_mb"], "hw.lcd.width": w,
            "hw.lcd.height": h, "hw.lcd.density": dpi_for(s), "hw.lcd.vsync": s["fps"]}
    path = avd_dir() / "config.ini"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return False
    out, seen = [], set()
    for line in lines:
        k = line.split("=", 1)[0].strip()
        if k in want:
            out.append(f"{k}={want[k]}")
            seen.add(k)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in want.items() if k not in seen]
    if out == lines:
        return False
    path.write_text("\n".join(out) + "\n")
    return True


# ---------------------------------------------------------------- starting the emulator

# -writable-system keeps the ARM translator that setup-avd.sh installed.
# -feature -Vulkan,...: the game draws with OpenGL ES instead of Vulkan, and the
# emulator composes the screen the plain way. With those on, the NVIDIA driver
# crashed the emulator (SIGSEGV in libgfxstream_backend) whenever a game rebuilt
# its screen (restarts, settings changes).
EMULATOR_ARGS = ["-writable-system", "-no-snapshot", "-gpu", "host",
                 "-feature", "-Vulkan,-GLAsyncSwap,-HostComposition",
                 "-grpc", GRPC_PORT, "-no-boot-anim"]


def emulator_complaint():
    """The emulator's last FATAL/ERROR line from its log, for the loading screen."""
    try:
        lines = (LOG_DIR / "emulator.log").read_text(errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        if line.startswith(("FATAL", "ERROR")):
            return "\n\n" + line.split("|", 1)[-1].strip()
    return ""


def emulator_command(s):
    """The emulator's command line and environment for settings `s`.

    Frame rate: Android's screen refreshes `fps` times a second (60 by default),
    which is also the most frames a game can show.
    VSync: the emulator never sets a swap interval for its window, so the graphics
    driver's default decides, and NVIDIA's default is to wait for the monitor.
    The driver's environment variables switch that on or off for the emulator only."""
    env = dict(os.environ)
    if s["vsync"]:
        env["__GL_SYNC_TO_VBLANK"] = "1"   # NVIDIA
        env.pop("vblank_mode", None)       # Mesa (Intel/AMD): its default syncs
    else:
        env["__GL_SYNC_TO_VBLANK"] = "0"
        env["vblank_mode"] = "0"
    fps = _clamp(s["fps"], FPS_RANGE[0], FPS_RANGE[1], DEFAULT_SETTINGS["fps"])
    return [EMULATOR, "-avd", AVD, *EMULATOR_ARGS, "-vsync-rate", str(fps)], env


def start_emulator(s):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    cmd, env = emulator_command(s)
    with open(LOG_DIR / "emulator.log", "wb") as log:
        return subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)


def running_emulator():
    """(qemu pid, adb serial) of the running emulator we control, found by its gRPC
    port, or None. The emulator can leave its files behind after it exits, so the
    process must still be alive."""
    run_dir = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "avd" / "running"
    for ini in run_dir.glob("pid_*.ini"):
        try:
            conf = dict(line.split("=", 1) for line in ini.read_text().splitlines() if "=" in line)
            pid = int(ini.stem.split("_", 1)[1])
            alive = b"qemu-system" in Path(f"/proc/{pid}/cmdline").read_bytes()
        except (OSError, ValueError):
            continue
        if alive and conf.get("grpc.port") == GRPC_PORT and conf.get("port.serial"):
            return pid, f"emulator-{conf['port.serial']}"
    return None


BOOTING_FILE = LOG_DIR / "booting"  # holds the pid of a Yellowstacks that's starting Android


def still_booting():
    """Is Yellowstacks still starting Android (and setting it up)? Installs wait for
    that: one of the set-up steps restarts Android's adb connection."""
    try:
        return pid_alive(int(BOOTING_FILE.read_text().strip()))
    except (OSError, ValueError):
        return False


def adb_shell(cmd, timeout=10):
    """Run a shell command in Android and return what it printed ("" if it failed)."""
    try:
        return subprocess.run([ADB, "shell", cmd], capture_output=True, text=True,
                              timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def adb_ready():
    try:
        out = subprocess.run([ADB, "get-state"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.strip() == "device"


def android_booted():
    return adb_shell("getprop sys.boot_completed", 5).strip() == "1"


def foreground_package():
    """The package of the app in front (the launcher on the home screen), or None."""
    out = adb_shell("dumpsys activity activities 2>/dev/null | grep -m1 topResumedActivity", 4)
    m = re.search(r" u\d+ ([\w.]+)/", out)
    return m.group(1) if m else None


def open_app(package):
    subprocess.run([ADB, "shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"],
                   capture_output=True, timeout=20)


def installed_packages():
    """Apps the user installed (not the ones that came with Android)."""
    return [line.split(":", 1)[1].strip() for line in adb_shell("pm list packages -3").splitlines()
            if line.startswith("package:")]


LAUNCHER_PKG = "com.google.android.apps.nexuslauncher"   # Android's own home screen
LAUNCHER_ICONS = f"/data/data/{LAUNCHER_PKG}/databases/app_icons.db"

# Yellowstacks' own home screen: a real Android home app, so pressing Home really
# does put the game in the background and Recent apps really does list it
HOME_PKG = "com.yellowstacks.launcher"
HOME_ACTIVITY = f"{HOME_PKG}/.HomeActivity"
HOME_APK = HERE / "launcher" / "yellowstacks-home.apk"


def list_apps():
    """Installed apps as [{"package", "label", "icon" (PNG bytes or None)}], by name.
    Names and icons come from the home screen's own icon cache (needs root, which
    this userdebug image has); apps it hasn't seen yet are shown by package name."""
    packages = installed_packages()
    if not packages:
        return []
    launchable = {}
    out = adb_shell("cmd package query-activities --brief -a android.intent.action.MAIN "
                    "-c android.intent.category.LAUNCHER")
    for line in out.splitlines():
        line = line.strip()
        if "/" in line and not line.startswith(("priority", "Activity")):
            launchable.setdefault(line.split("/", 1)[0], line)
    names, icons = {}, {}
    quoted = ",".join(f"'{p}'" for p in packages)
    sql = (f"select substr(componentName, 1, instr(componentName, '/') - 1), label, hex(icon) "
           f"from icons where substr(componentName, 1, instr(componentName, '/') - 1) in ({quoted}) "
           f"and profileId = 0 order by length(icon) desc")
    out = adb_shell(f'su 0 sqlite3 -separator "|" {LAUNCHER_ICONS} "{sql}"', 20)
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) == 3 and parts[0] in packages and parts[0] not in names:
            names[parts[0]] = parts[1]
            try:
                icons[parts[0]] = bytes.fromhex(parts[2]) if parts[2] else None
            except ValueError:
                icons[parts[0]] = None
    apps = [{"package": p, "label": names.get(p) or p, "icon": icons.get(p),
             "launchable": p in launchable} for p in packages]
    return sorted(apps, key=lambda a: a["label"].lower())


# ---------------------------------------------------------------- emulator

class Emulator:
    """Thin wrapper over the emulator's gRPC controller."""

    def __init__(self, addr=GRPC_ADDR):
        self.stub = pb_grpc.EmulatorControllerStub(grpc.insecure_channel(addr))

    def touches(self, fingers):
        """fingers: list of (finger_id, x, y, down)."""
        if not fingers:
            return
        self.stub.sendTouch(pb.TouchEvent(touches=[
            pb.Touch(x=int(x), y=int(y), identifier=fid, pressure=100 if down else 0,
                     touch_major=8, expiration=pb.Touch.NEVER_EXPIRE)
            for fid, x, y, down in fingers]))

    def screenshot_png(self):
        return self.stub.getScreenshot(pb.ImageFormat(format=pb.ImageFormat.PNG), timeout=5).image

    def paste(self, text):
        """Put text on Android's clipboard and press Android's Paste key."""
        self.stub.setClipboard(pb.ClipData(text=text), timeout=3)
        # evdev KEY_PASTE (135) -> Android KEYCODE_PASTE, which text boxes handle
        self.stub.sendKey(pb.KeyboardEvent(codeType=pb.KeyboardEvent.Evdev, keyCode=135,
                                           eventType=pb.KeyboardEvent.keypress), timeout=3)

    def booted(self):
        from google.protobuf import empty_pb2
        return self.stub.getStatus(empty_pb2.Empty(), timeout=3).booted

    def screen_size(self):
        """The Android screen size (landscape) the emulator was started with."""
        from google.protobuf import empty_pb2
        st = self.stub.getStatus(empty_pb2.Empty(), timeout=3)
        cfg = {e.key: e.value for e in st.hardwareConfig.entry}
        w, h = int(cfg["hw.lcd.width"]), int(cfg["hw.lcd.height"])
        return max(w, h), min(w, h)

    # Linux key codes as the emulator's keyboard (qwerty.kl) maps them
    POWER, VOLUME_DOWN, VOLUME_UP, BACK, HOME, RECENTS = 116, 114, 115, 158, 102, 580

    def key(self, code, down):
        kind = pb.KeyboardEvent.keydown if down else pb.KeyboardEvent.keyup
        self.stub.sendKey(pb.KeyboardEvent(codeType=pb.KeyboardEvent.Evdev, keyCode=code,
                                           eventType=kind), timeout=3)

    def press(self, code):
        self.stub.sendKey(pb.KeyboardEvent(codeType=pb.KeyboardEvent.Evdev, keyCode=code,
                                           eventType=pb.KeyboardEvent.keypress), timeout=3)

    def rotate(self, degrees):
        """Turn the device like the emulator's own rotate buttons (+90 = a quarter turn
        to the left). Only the console's `rotate` turns the window as well as the
        sensors, and it only goes clockwise, so a left turn is three right turns."""
        for _ in range((-degrees // 90) % 4):
            subprocess.run([ADB, "emu", "rotate"], timeout=5, capture_output=True)


def notify(text):
    """Small desktop notification; ignored if notify-send isn't available."""
    try:
        subprocess.Popen(["notify-send", "-a", APP, "-t", "2500", "-i", str(ICON), text],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


class TextFieldWatcher:
    """Polls Android for a focused text box (login fields, chat) in a background thread.

    `active` is True while the focused editor has a non-zero inputType.
    """

    POLL = 0.3  # seconds
    CMD = "dumpsys input_method | grep -A1 mCurrentTextBoxAttribute"

    def __init__(self):
        self.active = False
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self._stop.is_set():
            self.active = self._check()
            self._stop.wait(self.POLL)

    def _check(self):
        try:
            out = subprocess.run([ADB, "shell", self.CMD], capture_output=True,
                                 text=True, timeout=3).stdout
        except (OSError, subprocess.TimeoutExpired):
            return False
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("inputType="):
                try:
                    return int(line.split()[0].split("=", 1)[1], 16) != 0
                except ValueError:
                    return False
        return False

    def stop(self):
        self._stop.set()


class ForegroundWatcher:
    """Polls which Android app is in front, in a background thread. `package` is
    None until the first answer (and stays at the last answer if adb hiccups)."""

    POLL = 1.0  # seconds

    def __init__(self):
        self.package = None
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self._stop.is_set():
            self.package = foreground_package() or self.package
            self._stop.wait(self.POLL)

    def stop(self):
        self._stop.set()


def typing_bar_ok_point():
    """Where the OK button of the game's typing bar is, or None if it isn't open.

    Unity games type into a separate Android window: a full-width white bar with a
    text box and an OK button at its right end. Enter doesn't press OK (the game
    only listens for the button), so we find the bar and tap the button.
    """
    try:
        out = subprocess.run([ADB, "shell", "dumpsys window | grep -m1 mCurrentFocus; dumpsys window windows"],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    focus = re.search(r"mCurrentFocus=Window\{(\w+) ", out)
    if not focus:
        return None
    entry = re.search(r"Window #\d+ Window\{%s " % focus.group(1), out)  # the window's own entry
    if not entry:
        return None
    frame = re.compile(r" frame=\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]").search(out, entry.end())
    if not frame:
        return None
    left, top, right, bottom = map(int, frame.groups())
    if right - left < 1000 or not 60 < bottom - top < 400:  # not a typing bar
        return None
    return right - round(126 * (right - left) / 1920), (top + bottom) // 2  # 126 px at 1920 wide


# ---------------------------------------------------------------- X11 helpers

class XWindows:
    """Finds the emulator's windows and reads their on-screen geometry."""

    def __init__(self, d):
        from Xlib import X
        self.X, self.d = X, d
        self.root = d.screen().root
        self.NET_CLIENT_LIST = d.intern_atom("_NET_CLIENT_LIST")
        self.NET_NAME = d.intern_atom("_NET_WM_NAME")
        self.NET_PID = d.intern_atom("_NET_WM_PID")

    def name(self, win):
        prop = win.get_full_property(self.NET_NAME, 0)
        return prop.value.decode(errors="replace") if prop else (win.get_wm_name() or "")

    def is_emulator(self, win):
        return "qemu-system-x86_64" in (win.get_wm_class() or ())

    def is_main(self, win):
        """The game window (we retitle it, so accept both names)."""
        name = self.name(win)
        return self.is_emulator(win) and (name.startswith("Android Emulator") or name == APP)

    def pid(self, win):
        prop = win.get_full_property(self.NET_PID, 0)
        return prop.value[0] if prop else None

    def net_state(self, win):
        prop = win.get_full_property(self.d.intern_atom("_NET_WM_STATE"), self.X.AnyPropertyType)
        return {self.d.get_atom_name(a) for a in prop.value} if prop else set()

    def client_message(self, win, atom, data):
        from Xlib import protocol
        ev = protocol.event.ClientMessage(window=win, client_type=self.d.intern_atom(atom),
                                          data=(32, (data + [0] * 5)[:5]))
        mask = self.X.SubstructureRedirectMask | self.X.SubstructureNotifyMask
        self.root.send_event(ev, event_mask=mask)
        self.d.flush()

    def minimise(self, win):
        self.client_message(win, "WM_CHANGE_STATE", [3])  # IconicState

    def skip_taskbar(self, win, on):
        a = self.d.intern_atom
        self.client_message(win, "_NET_WM_STATE", [1 if on else 0, a("_NET_WM_STATE_SKIP_TASKBAR"),
                                                   a("_NET_WM_STATE_SKIP_PAGER"), 1])

    def activate(self, win):
        self.client_message(win, "_NET_ACTIVE_WINDOW", [2, 0, 0])

    def set_fullscreen(self, win, on):
        self.client_message(win, "_NET_WM_STATE",
                            [1 if on else 0, self.d.intern_atom("_NET_WM_STATE_FULLSCREEN"), 0, 1])

    def move_resize(self, win, x, y, w, h):
        # _NET_MOVERESIZE_WINDOW: gravity static(10) + x|y|w|h flags, source = pager (2)
        self.client_message(win, "_NET_MOVERESIZE_WINDOW",
                            [10 | (0xF << 8) | (2 << 12), int(x), int(y), int(w), int(h)])

    def brand(self, win, icon_argb):
        """Show the game window as Yellowstacks (title + taskbar icon)."""
        from Xlib import Xatom
        utf8 = self.d.intern_atom("UTF8_STRING")
        if self.name(win) != APP:
            win.change_property(self.NET_NAME, utf8, 8, APP.encode())
            win.change_property(Xatom.WM_NAME, Xatom.STRING, 8, APP.encode())
        # the emulator marks its window as not minimisable (it had its own button);
        # allow it so our minimise button and the title bar can do it
        motif = self.d.intern_atom("_MOTIF_WM_HINTS")
        prop = win.get_full_property(motif, motif)
        if prop and len(prop.value) >= 2 and prop.value[0] & 1 and not prop.value[1] & 8:
            hints = list(prop.value)
            hints[1] |= 8
            win.change_property(motif, motif, 32, hints)
        if icon_argb:
            win.change_property(self.d.intern_atom("_NET_WM_ICON"), Xatom.CARDINAL, 32, icon_argb)
        self.d.flush()

    def workarea_for(self, x, y):
        """(x, y, w, h) of the usable desktop area on the monitor containing x,y."""
        from Xlib.ext import randr
        mons = []
        try:
            for m in randr.get_monitors(self.root).monitors:
                mons.append((m.x, m.y, m.width_in_pixels, m.height_in_pixels))
        except Exception:
            g = self.root.get_geometry()
            mons = [(0, 0, g.width, g.height)]
        mon = next((m for m in mons if m[0] <= x < m[0] + m[2] and m[1] <= y < m[1] + m[3]), mons[0])
        prop = self.root.get_full_property(self.d.intern_atom("_NET_WORKAREA"), self.X.AnyPropertyType)
        if prop and len(prop.value) >= 4:  # one area for all monitors: clip it to this one
            wx, wy, ww, wh = prop.value[:4]
            x1, y1 = max(mon[0], wx), max(mon[1], wy)
            x2, y2 = min(mon[0] + mon[2], wx + ww), min(mon[1] + mon[3], wy + wh)
            if x2 > x1 and y2 > y1:
                return x1, y1, x2 - x1, y2 - y1
        return mon

    def toplevels(self):
        prop = self.root.get_full_property(self.NET_CLIENT_LIST, self.X.AnyPropertyType)
        if prop and len(prop.value):
            return [self.d.create_resource_object("window", w) for w in prop.value]
        return self.root.query_tree().children  # no window manager (tests)

    # ids of emulator windows we've seen; hidden (unmapped) windows drop out of the
    # window manager's list, and we still need to find them
    SEEN = set()

    def emulator(self):
        """(main window, side bar window); either can be None."""
        main = bar = None
        wins = []
        try:
            ids = [w.id for w in self.toplevels()]
        except Exception:
            return None, None
        for wid in dict.fromkeys(ids + sorted(XWindows.SEEN)):
            w = self.d.create_resource_object("window", wid)
            try:
                if self.is_emulator(w):
                    wins.append(w)
                    XWindows.SEEN.add(wid)
                else:
                    XWindows.SEEN.discard(wid)
            except Exception:  # a window that closed while we looked at it
                XWindows.SEEN.discard(wid)
        try:
            for w in wins:
                if self.is_main(w):
                    main = w
            if main is not None:
                pid = main.get_full_property(self.NET_PID, 0)
                for w in wins:
                    # the side bar is the tall narrow window of the same emulator process
                    # (its title is "" or "Emulator"; extended controls etc. are wider)
                    if w.id == main.id:
                        continue
                    wpid = w.get_full_property(self.NET_PID, 0)
                    if pid and wpid and wpid.value[0] != pid.value[0]:
                        continue
                    geo = w.get_geometry()
                    if geo.width < 160 and geo.height > 200:
                        bar = w
        except Exception:  # windows can vanish while we look at them
            return None, None
        return main, bar

    def geometry(self, win):
        """(x, y, width, height) on screen, or None if not visible (unmapped or minimised;
        a compositing window manager keeps minimised windows mapped)."""
        try:
            if win.get_attributes().map_state != self.X.IsViewable:
                return None
            if "_NET_WM_STATE_HIDDEN" in self.net_state(win):
                return None
            geo = win.get_geometry()
            pos = win.translate_coords(self.root, 0, 0)
            return -pos.x, -pos.y, geo.width, geo.height
        except Exception:
            return None


# ---------------------------------------------------------------- the controls

class Controls:
    def __init__(self, prof, emu, tk_root=None):
        from Xlib import X, XK, display
        self.X, self.XK = X, XK
        self.emu, self.tk_root = emu, tk_root
        self.d = display.Display()
        self.root = self.d.screen().root
        self.root.change_attributes(event_mask=X.PropertyChangeMask)
        self.NET_ACTIVE = self.d.intern_atom("_NET_ACTIVE_WINDOW")
        self.xwin = XWindows(self.d)

        self.enabled = True       # toggled by the ` key or the side bar
        self.window = None        # focused emulator window, or None
        self.grabbed = set()      # (keycode, modifiers) currently grabbed
        self.held = {}            # keycode -> action, for keys currently held
        self.fingers = {}         # finger_id -> (x, y) currently down
        self.typing = TextFieldWatcher()
        self.was_typing = False
        self.foreground = ForegroundWatcher()
        self.was_in_app = True
        self.state = None
        self.on_change = None     # called when on/off or focus state changes
        self.hotkeys = {}         # keycode -> callback (e.g. F11 full screen)
        self.on_configure = None  # called when the game window moves/resizes/maps
        self.stopped = False      # set while shutting down: take no keys at all
        self.mouse_grab = None    # game window whose left clicks we turn into touches
        self.mouse_pos = None     # device position of the mouse "finger" while held
        self.v_code = self.keycode("v")
        self.enter_codes = {self.keycode("Return"), self.keycode("KP_Enter")}
        try:  # the profile's positions are scaled to the screen size Android runs at
            self.screen = emu.screen_size()
        except Exception:
            self.screen = None
        self.set_profile(prof)

    def keycode(self, name, strict=True):
        # X keysym names are case-sensitive ("Tab", "Escape"); accept "tab" too
        sym = self.XK.string_to_keysym(name) or self.XK.string_to_keysym(name.capitalize())
        code = self.d.keysym_to_keycode(sym) if sym else 0
        if not code:
            if strict:
                raise SystemExit(f"Unknown key name in profile: {name!r}")
            print(f"ignoring unknown key {name!r}", flush=True)
            return None
        return code

    def set_profile(self, prof):
        """Load (or reload) the controls; safe while running."""
        self.held.clear()
        self.release_all()
        self.prof = prof
        pw, ph = prof["device_size"]
        self.dev_w, self.dev_h = self.screen or (pw, ph)
        k = self.dev_w / pw
        self.controls = [{**c, "pos": [c["pos"][0] * k, c["pos"][1] * k],
                          **({"radius": c["radius"] * k} if "radius" in c else {})}
                         for c in prof["controls"]]
        self.actions = {}  # keycode -> ("joy", index, direction) | ("tap", index)
        for i, c in enumerate(self.controls):
            if c["type"] == "joystick":
                for direction, name in c["keys"].items():
                    code = self.keycode(name, strict=False)
                    if code:
                        self.actions[code] = ("joy", i, direction)
            elif c["type"] == "tap":
                code = self.keycode(c["key"], strict=False)
                if code:
                    self.actions[code] = ("tap", i)
        self.toggle_code = self.keycode(prof.get("toggle_key", "grave"))
        self.update_grabs()

    def in_app(self):
        """Is the app these controls are for in front? (Not known yet counts as yes.)"""
        pkg = self.prof.get("package")
        front = self.foreground.package
        return not pkg or front is None or front == pkg

    def set_enabled(self, on):
        self.enabled = on
        self.update_grabs()
        notify("Game controls ON" if on else
               "Game controls OFF - keys type normally. Press ` to turn back on.")

    # -- which keys we take from the emulator

    def update_grabs(self):
        """Grab keys only while they should go to the game.

        Emulator not focused: nothing. Text box open: only Ctrl+V (our paste).
        Controls off: only the ` key. Otherwise: all mapped keys plus `.
        """
        X = self.X
        locks = (0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask)
        want = set()
        if self.window is not None and not self.stopped:
            if self.typing.active:
                for extra in (0, X.ShiftMask):
                    want |= {(self.v_code, X.ControlMask | extra | m) for m in locks}
                want |= {(c, m) for c in self.enter_codes for m in locks}  # Enter = OK
            else:
                playing = self.enabled and self.in_app()
                codes = [self.toggle_code, *self.hotkeys] + (list(self.actions) if playing else [])
                want |= {(c, m) for c in codes for m in locks}
        self.update_mouse_grab()
        if want != self.grabbed:
            for code, mods in self.grabbed - want:
                self.root.ungrab_key(code, mods)
            for code, mods in want - self.grabbed:
                self.root.grab_key(code, mods, True, X.GrabModeAsync, X.GrabModeAsync)
            self.d.flush()
            self.grabbed = want
        if self.window is None or self.typing.active or not self.enabled or not self.in_app():
            self.held.clear()
            self.release_all()
        state = ("paused (emulator not focused)" if self.window is None else
                 "paused (text box open - type freely)" if self.typing.active else
                 "OFF (press ` to turn on)" if not self.enabled else
                 f"paused ({self.foreground.package} is in front)" if not self.in_app() else "active")
        if state != self.state:
            self.state = state
            print("controls", state, flush=True)
        if self.on_change:
            self.on_change()

    def update_mouse_grab(self):
        """While the controls are active, take left clicks on the game window and send
        them as their own finger. The emulator's own mouse touches always use finger
        slot 0, the same slot as the first of our fingers, so a click while walking
        would drag the joystick finger away (walking stops, the skill never fires)."""
        X = self.X
        active = (self.window is not None and not self.stopped and self.enabled
                  and not self.typing.active and self.in_app())
        want = self.window if active else None
        if (want and want.id) == (self.mouse_grab and self.mouse_grab.id):
            return
        if self.mouse_grab is not None:
            try:
                self.mouse_grab.ungrab_button(1, X.AnyModifier)
            except Exception:
                pass
            self.mouse_pos = None
        if want is not None:
            want.grab_button(1, X.AnyModifier, False,
                             X.ButtonPressMask | X.ButtonReleaseMask | X.PointerMotionMask,
                             X.GrabModeAsync, X.GrabModeAsync, X.NONE, X.NONE)
        self.d.flush()
        self.mouse_grab = want

    def to_device(self, wx, wy):
        """Game-window pixel -> Android screen pixel."""
        try:
            geo = self.mouse_grab.get_geometry()
            sx, sy = self.dev_w / geo.width, self.dev_h / geo.height
        except Exception:
            sx = sy = 1
        return (min(max(wx * sx, 0), self.dev_w - 1), min(max(wy * sy, 0), self.dev_h - 1))

    def refresh_focus(self):
        prop = self.root.get_full_property(self.NET_ACTIVE, self.X.AnyPropertyType)
        win = None
        if prop and len(prop.value) and prop.value[0]:
            w = self.d.create_resource_object("window", prop.value[0])
            try:
                if self.xwin.is_main(w):
                    win = w
            except Exception:
                pass
        self.window = win
        self.update_grabs()

    # -- fingers

    def desired_fingers(self):
        want = {}
        pushes = {}
        for act in self.held.values():
            if act[0] == "joy":
                _, i, d = act
                dx, dy = pushes.get(i, (0, 0))
                pushes[i] = (dx + DIRS[d][0], dy + DIRS[d][1])
            elif act[0] == "tap":
                want[2000 + act[1]] = tuple(self.controls[act[1]]["pos"])
        if self.mouse_pos is not None:
            want[3000] = self.mouse_pos
        for i, (dx, dy) in pushes.items():
            if dx or dy:
                c = self.controls[i]
                n = math.hypot(dx, dy)
                want[1000 + i] = (c["pos"][0] + dx / n * c["radius"],
                                  c["pos"][1] + dy / n * c["radius"])
        return want

    def sync_fingers(self):
        want = self.desired_fingers()
        out = []
        for fid, (x, y) in want.items():
            if fid not in self.fingers:
                # joystick: touch the stick's centre first, then push it
                start = (x, y) if fid == 3000 else tuple(self.controls[fid % 1000]["pos"])
                out.append((fid, start[0], start[1], True))
                self.fingers[fid] = start
            elif self.fingers[fid] != (x, y):
                out.append((fid, x, y, True))
                self.fingers[fid] = (x, y)
        for fid in list(self.fingers):
            if fid not in want:
                x, y = self.fingers.pop(fid)
                out.append((fid, x, y, False))
        if out:
            try:
                self.emu.touches(out)
            except grpc.RpcError as e:
                print("emulator not reachable:", e.code().name, flush=True)

    def release_all(self):
        if self.fingers:
            try:
                self.emu.touches([(fid, x, y, False) for fid, (x, y) in self.fingers.items()])
            except grpc.RpcError:
                pass
            self.fingers.clear()

    # -- events

    def handle(self, ev):
        X = self.X
        if ev.type == X.PropertyNotify and ev.atom == self.NET_ACTIVE:
            self.refresh_focus()
        elif ev.type == X.ButtonPress and ev.detail == 1 and self.mouse_grab is not None:
            self.mouse_pos = self.to_device(ev.event_x, ev.event_y)
            self.sync_fingers()  # right away, so a quick click isn't lost between ticks
        elif ev.type == X.MotionNotify and self.mouse_pos is not None:
            self.mouse_pos = self.to_device(ev.event_x, ev.event_y)
        elif ev.type == X.ButtonRelease and ev.detail == 1 and self.mouse_pos is not None:
            self.mouse_pos = None
            self.sync_fingers()
        elif ev.type in (X.ConfigureNotify, X.MapNotify, X.UnmapNotify):
            if self.on_configure:
                self.on_configure()
        elif ev.type == X.KeyPress:
            if ev.detail == self.v_code and ev.state & X.ControlMask:
                if self.typing.active:
                    self.paste_host_clipboard()
                return
            if ev.detail in self.enter_codes and self.typing.active:
                threading.Thread(target=self.press_ok, daemon=True).start()
                return
            if ev.detail == self.toggle_code:
                self.set_enabled(not self.enabled)
                return
            if ev.detail in self.hotkeys:
                self.hotkeys[ev.detail]()
                return
            act = self.actions.get(ev.detail)
            if act and self.enabled:
                self.held[ev.detail] = act
        elif ev.type == X.KeyRelease:
            if ev.detail in self.held and not self.key_physically_down(ev.detail):  # ignore auto-repeat
                del self.held[ev.detail]

    def drop_released_keys(self):
        """Let go of keys that are no longer down.

        X only sends a key's release to us if that key started its own grab. If W is
        held when D is pressed, D's release goes elsewhere once W is let go, so we
        check the real keyboard state instead of trusting release events alone.
        """
        if not self.held:
            return
        keymap = self.d.query_keymap()
        for code in [c for c in self.held if not keymap[c // 8] & (1 << (c % 8))]:
            del self.held[code]

    def key_physically_down(self, code):
        keymap = self.d.query_keymap()
        return bool(keymap[code // 8] & (1 << (code % 8)))

    def press_ok(self):
        """Enter while typing: press the typing bar's OK button (or a plain Enter)."""
        point = typing_bar_ok_point()
        try:
            if point:
                x, y = point
                self.emu.touches([(9, x, y, True)])
                time.sleep(0.06)
                self.emu.touches([(9, x, y, False)])
                print("Enter -> OK", flush=True)
            else:
                self.emu.press(28)  # KEY_ENTER
        except grpc.RpcError as e:
            print("Enter failed:", e.code().name, flush=True)

    def paste_host_clipboard(self):
        text = ""
        if self.tk_root is not None:
            try:
                text = self.tk_root.clipboard_get()
            except Exception:
                text = ""
        if not text:
            notify("Nothing to paste - the clipboard is empty (or holds a picture)")
            return
        try:
            self.emu.paste(text)
            print(f"pasted {len(text)} characters", flush=True)
        except grpc.RpcError as e:
            print("paste failed:", e.code().name, flush=True)

    def tick(self):
        while self.d.pending_events():
            self.handle(self.d.next_event())
        self.drop_released_keys()
        in_app = self.in_app()
        if self.typing.active != self.was_typing or in_app != self.was_in_app:
            self.was_typing, self.was_in_app = self.typing.active, in_app
            self.update_grabs()
        self.sync_fingers()

    def shutdown(self):
        self.typing.stop()
        self.foreground.stop()
        self.window = None
        self.update_grabs()


# ---------------------------------------------------------------- side bar

# Yellowstacks look: dark, warm yellow accents
BG = "#141317"         # bar / dialog background
PANEL = "#1f1e24"      # raised surfaces (menus, toolbars, tooltips)
RAISED = "#2a2930"     # buttons on panels
LINE = "#2f2d35"
YELLOW = "#FFD23F"
YELLOW_DOWN = "#E0B000"
TEXT = "#F3EBD3"
MUTED = "#8a8578"
DANGER = "#e5484d"


def round_rect(c, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


def theme_tk(root):
    """Dark/yellow defaults for Tk menus."""
    for k, v in {"background": PANEL, "foreground": TEXT, "activeBackground": YELLOW,
                 "activeForeground": BG, "selectColor": YELLOW, "disabledForeground": MUTED,
                 "relief": "flat", "borderWidth": 0, "activeBorderWidth": 0,
                 "font": "Sans 10"}.items():
        root.option_add(f"*Menu.{k}", v)


def ask(parent, title, text, yes="Yes", no="Cancel", danger=False):
    """Themed yes/no dialog; returns True for yes."""
    import tkinter as tk
    top = tk.Toplevel(parent)
    top.title(title)
    top.configure(bg=BG, padx=22, pady=18)
    top.resizable(False, False)
    tk.Label(top, text=title, bg=BG, fg=YELLOW, font=("Sans", 13, "bold")).pack(anchor="w")
    tk.Label(top, text=text, bg=BG, fg=TEXT, font=("Sans", 10), justify="left",
             wraplength=360).pack(anchor="w", pady=(8, 16))
    row = tk.Frame(top, bg=BG)
    row.pack(fill="x")
    result = {"ok": False}

    def done(ok):
        result["ok"] = ok
        top.destroy()

    style = dict(relief="flat", bd=0, padx=16, pady=6, font=("Sans", 10, "bold"), cursor="hand2")
    if no:
        tk.Button(row, text=no, command=lambda: done(False), bg=RAISED, fg=TEXT,
                  activebackground=LINE, activeforeground=TEXT, **style).pack(side="right", padx=(8, 0))
    tk.Button(row, text=yes, command=lambda: done(True), bg=DANGER if danger else YELLOW,
              fg="white" if danger else BG, activebackground=YELLOW_DOWN, activeforeground=BG,
              **style).pack(side="right")
    top.bind("<Escape>", lambda e: done(False))
    top.bind("<Return>", lambda e: done(True))
    top.update_idletasks()
    sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
    px, py = parent.winfo_pointerxy()
    top.geometry(f"+{min(max(px - top.winfo_reqwidth() // 2, 0), sw - top.winfo_reqwidth())}"
                 f"+{min(max(py - 40, 0), sh - top.winfo_reqheight())}")
    top.transient(parent)
    top.wait_visibility()
    top.grab_set()
    top.focus_force()
    parent.wait_window(top)
    return result["ok"]


def info(parent, title, text):
    ask(parent, title, text, yes="OK", no=None)


class Painter:
    """Draws at 4x size with Pillow, then scales down: smooth, anti-aliased edges.
    (Tk's canvas draws lines, arcs and polygons without anti-aliasing on X11, which
    makes small icons look jagged.) Coordinates are in final-size pixels."""

    S = 4

    def __init__(self, w, h, bg=None):
        """bg=None makes a see-through image, for drawing over other things."""
        from PIL import Image, ImageDraw
        self.w, self.h = w, h
        mode, bg = ("RGB", bg) if bg else ("RGBA", (0, 0, 0, 0))
        self.img = Image.new(mode, (w * self.S, h * self.S), bg)
        self.d = ImageDraw.Draw(self.img)

    def _pts(self, pts):
        S = self.S
        return [(pts[i] * S, pts[i + 1] * S) for i in range(0, len(pts), 2)]

    def dot(self, x, y, r, col):
        S = self.S
        self.d.ellipse(((x - r) * S, (y - r) * S, (x + r) * S, (y + r) * S), fill=col)

    def line(self, pts, col, width=2.2):
        """Polyline with round caps and joins."""
        P = self._pts(pts)
        self.d.line(P, fill=col, width=round(width * self.S), joint="curve")
        for i in range(0, len(pts), 2):
            self.dot(pts[i], pts[i + 1], width / 2, col)

    def arc(self, cx, cy, r, start, extent, col, width=2.2):
        """Arc around (cx, cy); angles counter-clockwise from 3 o'clock, like Tk."""
        pts = []
        steps = max(8, int(abs(extent) / 6))
        for i in range(steps + 1):
            a = math.radians(start + extent * i / steps)
            pts += [cx + r * math.cos(a), cy - r * math.sin(a)]
        self.line(pts, col, width)
        return pts

    def poly(self, pts, col):
        self.d.polygon(self._pts(pts), fill=col)

    def rrect(self, x1, y1, x2, y2, r, fill=None, outline=None, width=2.0):
        S = self.S
        self.d.rounded_rectangle((x1 * S, y1 * S, x2 * S, y2 * S), radius=r * S, fill=fill,
                                 outline=outline, width=round(width * S) if outline else 0)

    def ring(self, x, y, r, col, width=2.0):
        S, h = self.S, width / 2
        self.d.ellipse(((x - r - h) * S, (y - r - h) * S, (x + r + h) * S, (y + r + h) * S),
                       outline=col, width=round(width * S))

    def photo(self):
        from PIL import Image, ImageTk
        return ImageTk.PhotoImage(self.img.resize((self.w, self.h), Image.LANCZOS))


def paint_button(name, w, h, hot, pressed, on=None):
    """One side-bar button as a smooth image: optional lit tile + icon."""
    p = Painter(w, h, BG)
    x, y = w / 2, h / 2
    col = YELLOW
    if hot:
        fill = DANGER if name == "close" else (YELLOW_DOWN if pressed else YELLOW)
        m = 7 if w > 40 else 4
        p.rrect(m, 2, w - m, h - 2, 9, fill=fill)
        col = "white" if name == "close" else BG
    elif name in ("min", "close"):
        col = MUTED
    if name == "toggle":
        y -= 6
    if name == "min":
        p.line([x - 5, y, x + 5, y], col)
    elif name == "close":
        p.line([x - 5, y - 5, x + 5, y + 5], col)
        p.line([x - 5, y + 5, x + 5, y - 5], col)
    elif name == "power":
        p.arc(x, y + 1, 8, 125, 290, col)
        p.line([x, y - 9, x, y - 1], col)
    elif name in ("vol_up", "vol_down"):  # speaker with a plus / minus
        p.poly([x - 11, y - 3.5, x - 7, y - 3.5, x - 2, y - 8.5, x - 2, y + 8.5, x - 7, y + 3.5,
                x - 11, y + 3.5], col)
        p.line([x + 3, y, x + 11, y], col)
        if name == "vol_up":
            p.line([x + 7, y - 4, x + 7, y + 4], col)
    elif name in ("rot_left", "rot_right"):  # circular arrow, mirrored for right
        s = 1 if name == "rot_left" else -1
        start, extent, r = -40, 270, 7.5
        pts = []
        for i in range(25):
            a = math.radians(start + extent * i / 24)
            pts += [x + s * r * math.cos(a), y - r * math.sin(a)]
        p.line(pts, col)
        a = math.radians(start + extent)  # arrowhead at the end, along the direction of travel
        ex, ey = x + s * r * math.cos(a), y - r * math.sin(a)
        dx, dy = -s * math.sin(a), -math.cos(a)
        nx, ny = -dy, dx
        p.poly([ex + dx * 4, ey + dy * 4, ex - dx * 1.5 + nx * 4, ey - dy * 1.5 + ny * 4,
                ex - dx * 1.5 - nx * 4, ey - dy * 1.5 - ny * 4], col)
    elif name == "back":  # chevron
        p.line([x + 4, y - 8, x - 4, y, x + 4, y + 8], col)
    elif name == "home":  # house
        p.line([x - 9, y - 1, x, y - 9, x + 9, y - 1], col)
        p.line([x - 6, y - 3, x - 6, y + 8, x + 6, y + 8, x + 6, y - 3], col)
    elif name == "recents":  # two stacked cards
        tile = (YELLOW_DOWN if pressed else YELLOW) if hot else BG
        p.rrect(x - 4, y - 9, x + 8, y + 3, 3, outline=col, width=2)
        p.rrect(x - 8, y - 4, x + 4, y + 8, 3, fill=tile, outline=col, width=2)
    elif name == "screenshot":  # viewfinder corners + dot
        for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            cx, cy = x + 9 * sx, y + 7 * sy
            p.line([cx, cy - 4 * sy, cx, cy, cx - 4 * sx, cy], col)
        p.dot(x, y, 2.6, col)
    elif name == "zoom":  # expand arrows
        p.line([x - 7, y + 7, x + 7, y - 7], col)
        p.line([x + 1, y - 8, x + 8, y - 8, x + 8, y - 1], col)
        p.line([x - 8, y + 1, x - 8, y + 8, x - 1, y + 8], col)
    elif name == "keys":
        p.rrect(x - 12, y - 8, x + 12, y + 8, 3.5, outline=col, width=2)
        for yy in (y - 3, y + 1):
            for i in range(5):
                p.dot(x - 8 + i * 4, yy, 1.2, col)
        p.line([x - 5, y + 4.5, x + 5, y + 4.5], col, width=2)
    elif name == "toggle":
        pill = (BG if hot else YELLOW) if on else (BG if hot else LINE)
        knob = (YELLOW if hot else BG) if on else MUTED
        p.rrect(x - 14, y - 7.5, x + 14, y + 7.5, 7.5, fill=pill)
        p.dot(x + 6.5 if on else x - 6.5, y, 5, knob)
    elif name == "apk":  # arrow down into a tray
        p.line([x, y - 9, x, y + 3], col)
        p.line([x - 5, y - 2, x, y + 3, x + 5, y - 2], col)
        p.line([x - 9, y + 2, x - 9, y + 8, x + 9, y + 8, x + 9, y + 2], col)
    elif name == "settings":  # gear: 8 teeth around a hole
        pts = []
        for i in range(32):
            a = math.radians(i * 360 / 32 - 5.6)
            r = 10 if i % 4 in (1, 2) else 7.4
            pts += [x + r * math.cos(a), y - r * math.sin(a)]
        p.poly(pts, col)
        p.dot(x, y, 3.2, (YELLOW_DOWN if pressed else YELLOW) if hot else BG)
    elif name == "more":  # vertical dots
        for dy in (-7, 0, 7):
            p.dot(x, y + dy, 2.1, col)
    return p.photo()


def paint_tap(r, selected):
    """Editor marker for a tap control: yellow disc with a dark rim (white when selected).
    See-through around it; the key label is drawn on top by Tk."""
    rim = 3 if selected else 2
    half = math.ceil(r + rim) + 1
    p = Painter(2 * half, 2 * half)
    p.dot(half, half, r + rim / 2, "white" if selected else BG)
    p.dot(half, half, r - rim / 2, YELLOW)
    return p.photo()


def paint_joystick(R, selected):
    """Editor marker for a joystick: faint yellow disc with a dashed rim (white when selected),
    a centre dot and a dark bubble per direction key. Labels are drawn on top by Tk."""
    rim = 3 if selected else 2
    half = math.ceil(max(R + rim, 0.6 * R + 17)) + 1
    p = Painter(2 * half, 2 * half)
    p.dot(half, half, R, (0xFF, 0xD2, 0x3F, 40))
    n = max(8, round(2 * math.pi * R / 10))  # a dash every ~10 px, like Tk's dash=(6, 4)
    for i in range(n):
        p.arc(half, half, R, i * 360 / n, 360 / n * 0.4, "white" if selected else YELLOW, rim)
    p.dot(half, half, 6, YELLOW)
    for dx, dy in DIRS.values():
        kx, ky = half + dx * R * 0.6, half + dy * R * 0.6
        p.dot(kx, ky, 16, YELLOW)
        p.dot(kx, ky, 14, BG)
    return p.photo()


def paint_control_glyph(kind, size):
    """A small mark of a joystick or a tap, for the editor's Add buttons. Drawn the
    same way as the side bar's icons, because Tk's canvas has no anti-aliasing."""
    p = Painter(size, size)
    c = size / 2
    if kind == "joystick":
        r = size * 0.36
        arm = size * 0.19
        p.ring(c, c, r, YELLOW, max(1.5, size * 0.085))
        p.line([c, c - arm, c, c + arm], YELLOW, max(1.4, size * 0.08))
        p.line([c - arm, c, c + arm, c], YELLOW, max(1.4, size * 0.08))
    else:
        p.ring(c, c, size * 0.42, YELLOW, max(1.2, size * 0.07))
        p.dot(c, c, size * 0.2, YELLOW)
    return p.photo()


class SideBar:
    """Yellowstacks' side bar, floating just right of the game window.

    It's a borderless utility window attached to the game window, so it stacks and
    minimises with it, and it never takes keyboard focus, so clicking it doesn't pull
    you out of the game.
    """

    W = 58
    GAP = 6       # space between the game window and the bar
    RADIUS = 12   # rounded window corners

    # name, tooltip, kind ("hold" = Android key down/up with the mouse button)
    GROUPS = [
        [("power", "Power  (hold for the power menu)", "hold"),
         ("vol_up", "Volume up", "hold"),
         ("vol_down", "Volume down", "hold"),
         ("rot_left", "Rotate left", "click"),
         ("rot_right", "Rotate right", "click")],
        [("back", "Back", "click"),
         ("home", "Home", "click"),
         ("recents", "Recent apps", "click")],
        [("screenshot", "Take a screenshot", "click"),
         ("zoom", "Window size / full screen", "click"),
         ("apk", "Install an app (APK)", "click"),
         ("keys", "Edit controls", "click"),
         ("toggle", "Controls on/off  ( ` )", "click"),
         ("settings", "Settings", "click"),
         ("more", "More", "click")],
    ]

    def __init__(self, root, app):
        import tkinter as tk
        self.tk, self.root, self.app = tk, root, app
        self.d = app.xwin.d
        self.shown = False
        self.pos = None
        self.shaped = None
        self.transient_for = None
        self.tip = None
        self.tip_job = None
        self.canvases = {}
        self.images = {}   # rendered button pictures, by (name, size, state)

        root.title(APP)
        root.configure(bg=BG)
        theme_tk(root)
        try:
            root.wm_attributes("-type", "utility")
        except tk.TclError:
            pass
        self.logo(root)
        top = tk.Frame(root, bg=BG)
        top.pack(fill="x", pady=(0, 2))
        for name, tip in (("min", "Minimise"), ("close", "Close Yellowstacks (shuts Android down)")):
            self.button(top, name, tip, "click", self.W // 2, 26).pack(side="left")
        for group in self.GROUPS:
            tk.Frame(root, bg=LINE, height=1).pack(fill="x", padx=12, pady=5)
            for name, tip, kind in group:
                self.button(root, name, tip, kind, self.W, 46 if name == "toggle" else 38).pack()
        tk.Frame(root, bg=BG, height=8).pack()
        root.withdraw()
        root.update_idletasks()
        # Tk's managed X window is the parent of the widget window
        inner = self.d.create_resource_object("window", root.winfo_id())
        self.wrapper = inner.query_tree().parent
        self.set_hints()

    def logo(self, parent):
        """The Yellowstacks mark at the top; click it for About."""
        c = self.tk.Canvas(parent, width=self.W, height=46, bg=BG, highlightthickness=0, bd=0,
                           cursor="hand2")
        try:
            from PIL import Image, ImageTk
            logo = Image.open(ICON).convert("RGBA").resize((30, 30), Image.LANCZOS)
            base = Image.new("RGBA", logo.size, BG)
            self.logo_img = ImageTk.PhotoImage(Image.alpha_composite(base, logo).convert("RGB"))
            c.create_image(self.W / 2, 25, image=self.logo_img)
        except Exception:
            c.create_text(self.W / 2, 25, text="Y", fill=YELLOW, font=("Sans", 16, "bold"))
        c.bind("<Enter>", lambda e: self.tip_schedule(c, APP))
        c.bind("<Leave>", lambda e: self.tip_hide())
        c.bind("<ButtonRelease-1>", lambda e: self.app.about())
        c.pack()
        self.canvases["logo"] = c

    # -- window manager hints

    def set_hints(self):
        from Xlib import Xatom
        a, w = self.d.intern_atom, self.wrapper
        w.change_property(a("_MOTIF_WM_HINTS"), a("_MOTIF_WM_HINTS"), 32, [2, 0, 0, 0, 0])
        w.change_property(a("_NET_WM_STATE"), Xatom.ATOM, 32,
                          [a("_NET_WM_STATE_SKIP_TASKBAR"), a("_NET_WM_STATE_SKIP_PAGER")])
        w.change_property(a("_NET_WM_USER_TIME"), Xatom.CARDINAL, 32, [0])  # don't focus on map
        self.d.flush()

    def no_focus(self):
        """Tk resets WM_HINTS when it maps the window, so re-apply input=False after."""
        from Xlib import Xutil
        self.wrapper.set_wm_hints(flags=Xutil.InputHint, input=0)
        self.d.flush()

    def skip_taskbar(self):
        """Tk rewrites _NET_WM_STATE when it maps the window; ask the window manager
        (and, with no window manager, set the property) to keep us out of the taskbar."""
        from Xlib import Xatom
        a = self.d.intern_atom
        skip = [a("_NET_WM_STATE_SKIP_TASKBAR"), a("_NET_WM_STATE_SKIP_PAGER")]
        self.app.xwin.client_message(self.wrapper, "_NET_WM_STATE", [1, skip[0], skip[1], 1])
        prop = self.wrapper.get_full_property(a("_NET_WM_STATE"), Xatom.ATOM)
        have = list(prop.value) if prop else []
        self.wrapper.change_property(a("_NET_WM_STATE"), Xatom.ATOM, 32, have + [s for s in skip if s not in have])
        self.d.flush()

    def round_corners(self, w, h):
        """Clip the window to a rounded rectangle (X Shape extension)."""
        if self.shaped == (w, h):
            return
        try:
            from Xlib.ext import shape
            r = self.RADIUS
            rects = []
            for y in range(r):
                inset = int(round(r - math.sqrt(max(r * r - (r - y - 0.5) ** 2, 0))))
                rects.append((inset, y, w - 2 * inset, 1))
                rects.append((inset, h - 1 - y, w - 2 * inset, 1))
            rects.append((0, r, w, h - 2 * r))
            self.wrapper.shape_rectangles(shape.SO.Set, shape.SK.Bounding, 0, 0, 0, rects)
            self.d.flush()
            self.shaped = (w, h)
        except Exception as e:  # no Shape extension: square corners are fine
            print("no rounded corners:", e, flush=True)
            self.shaped = (w, h)

    def show_at(self, x, y, main):
        from Xlib import Xatom
        x += self.GAP
        if self.transient_for != main.id:
            self.wrapper.change_property(Xatom.WM_TRANSIENT_FOR, Xatom.WINDOW, 32, [main.id])
            self.d.flush()
            self.transient_for = main.id
        h = self.root.winfo_reqheight()
        if (x, y, h) != self.pos:
            self.root.geometry(f"{self.W}x{h}+{x}+{y}")
            self.pos = (x, y, h)
        if not self.shown:
            self.root.deiconify()
            self.root.update_idletasks()
            self.no_focus()
            self.skip_taskbar()
            self.shown = True
        self.round_corners(self.W, h)

    def hide(self):
        self.tip_hide()
        if self.shown:
            self.root.withdraw()
            self.shown = False

    # -- buttons

    def button(self, parent, name, tip, kind, w, h):
        c = self.tk.Canvas(parent, width=w, height=h, bg=BG, highlightthickness=0, bd=0, cursor="hand2")
        c.pressed = False
        c.hover = False
        self.canvases[name] = c
        self.draw(c, name)

        def enter(_):
            c.hover = True
            self.draw(c, name)
            self.tip_schedule(c, tip)

        def leave(_):
            c.hover = False
            self.draw(c, name)
            self.tip_hide()
            if c.pressed and kind == "hold":
                c.pressed = False
                self.app.key_up(name)

        def press(_):
            c.pressed = True
            self.draw(c, name)
            self.tip_hide()
            if kind == "hold":
                self.app.key_down(name)

        def release(e):
            was, c.pressed = c.pressed, False
            self.draw(c, name)
            if not was:
                return
            if kind == "hold":
                self.app.key_up(name)
            elif 0 <= e.x < w and 0 <= e.y < h:
                self.app.action(name, c)

        c.bind("<Enter>", enter)
        c.bind("<Leave>", leave)
        c.bind("<ButtonPress-1>", press)
        c.bind("<ButtonRelease-1>", release)
        return c

    def refresh_toggle(self):
        c = self.canvases.get("toggle")
        if c is not None:
            self.draw(c, "toggle")

    def draw(self, c, name):
        """Show the button's picture for its current state (normal / hover / pressed)."""
        c.delete("all")
        hot = c.hover or c.pressed
        on = self.app.ctl.enabled if name == "toggle" else None
        key = (name, int(c["width"]), int(c["height"]), hot, c.pressed, on)
        img = self.images.get(key)
        if img is None:
            img = self.images[key] = paint_button(name, int(c["width"]), int(c["height"]), hot, c.pressed, on)
        c.create_image(0, 0, anchor="nw", image=img)
        if name == "toggle":
            col = (BG if hot else YELLOW) if on else (BG if hot else MUTED)
            c.create_text(int(c["width"]) / 2, int(c["height"]) / 2 + 12, text="ON" if on else "OFF",
                          fill=col, font=("Sans", 7, "bold"))

    # -- tooltips

    def tip_schedule(self, widget, text):
        self.tip_hide()
        if text:
            self.tip_job = self.root.after(450, lambda: self.tip_show(widget, text))

    def tip_show(self, widget, text):
        self.tip_hide()
        tk = self.tk
        t = tk.Toplevel(self.root, bg=YELLOW)
        t.overrideredirect(True)
        tk.Label(t, text=text, bg=PANEL, fg=TEXT, font=("Sans", 9), padx=10, pady=5).pack(padx=(0, 3))
        t.update_idletasks()
        x = widget.winfo_rootx() - t.winfo_reqwidth() - 10
        y = widget.winfo_rooty() + widget.winfo_height() // 2 - t.winfo_reqheight() // 2
        t.geometry(f"+{x}+{y}")
        self.tip = t

    def tip_hide(self):
        if self.tip_job:
            self.root.after_cancel(self.tip_job)
            self.tip_job = None
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


class App:
    """Ties the side bar, the controls and the emulator together."""

    KEYS = {"power": Emulator.POWER, "vol_up": Emulator.VOLUME_UP, "vol_down": Emulator.VOLUME_DOWN}

    def __init__(self, root, ctl, emu, profile_name):
        self.root, self.ctl, self.emu = root, ctl, emu
        self.xwin = ctl.xwin
        self.profile_name = profile_name
        self.settings = load_settings()
        self.hide_original = not self.settings["show_emulator_bar"]
        self.settings_win = None
        self.restart_requested = False
        self.shutting_down = False
        self.forced = False
        self.emu_pid = None
        self.missing_since = None
        self.branded = None
        self.watched = None
        self.editor = None
        self.icon_argb = load_icon_argb()
        self.bar = SideBar(root, self)
        ctl.on_change = self.bar.refresh_toggle
        ctl.on_configure = self.follow_now
        ctl.hotkeys[ctl.keycode("F11")] = self.toggle_fullscreen
        ctl.update_grabs()
        root.after(100, self.follow)

    # -- keeping the bar docked

    def follow(self):
        try:
            self.follow_now()
        finally:
            self.root.after(200, self.follow)

    def follow_now(self):
        main, orig = self.xwin.emulator()
        if main is None:
            self.bar.hide()
            if self.shutting_down:
                return
            self.missing_since = self.missing_since or time.time()
            gone = self.emu_pid is not None and not pid_alive(self.emu_pid)
            if gone or time.time() - self.missing_since > 30:
                print("emulator closed", flush=True)
                self.root.quit()
            return
        self.missing_since = None
        if self.emu_pid is None:
            self.emu_pid = self.xwin.pid(main)
        if self.watched != main.id:  # move the bar as soon as the game window moves
            main.change_attributes(event_mask=self.xwin.X.StructureNotifyMask)
            self.watched = main.id
        if self.branded != main.id:
            self.xwin.brand(main, self.icon_argb)
            self.branded = main.id
            self.restore_window(main)
        elif self.xwin.name(main) != APP:
            self.xwin.brand(main, None)
        if orig is not None:
            visible = self.xwin.geometry(orig) is not None
            if self.hide_original and visible:
                orig.unmap()
                self.xwin.d.flush()
            elif not self.hide_original and not visible:
                orig.map()
                self.xwin.d.flush()
        geo = self.xwin.geometry(main)
        if geo is None or self.shutting_down or "_NET_WM_STATE_FULLSCREEN" in self.xwin.net_state(main):
            self.bar.hide()
            return
        if not self.bar.shown and self.active_window() not in (main.id, self.bar.wrapper.id):
            # The window manager opens new windows just under the focused one, which
            # would put the bar over whatever covers the game. Wait until the game
            # window is active; after that the bar stays stacked with it.
            return
        x, y, w, h = geo
        self.remember_window(geo)
        bx = x + w
        if not self.hide_original and orig is not None:
            og = self.xwin.geometry(orig)
            if og:
                bx = og[0] + og[2]
        self.bar.show_at(bx, y, main)

    WINDOW_FILE = CONFIG_DIR / "window.json"

    def restore_window(self, main):
        """Put the game window back where it was last time (the emulator always
        opens it small, in its own spot)."""
        if not self.settings["remember_window"]:
            return
        try:
            x, y, w, h = json.loads(self.WINDOW_FILE.read_text())["geometry"]
        except (OSError, ValueError, KeyError):
            return
        if w > h:  # only landscape sizes; a rotated window gets its own size
            self.xwin.move_resize(main, x, y, w, h)
            self.saved_geo = (x, y, w, h)

    def remember_window(self, geo):
        if geo == getattr(self, "saved_geo", None) or geo[2] <= geo[3] or not self.settings["remember_window"]:
            return
        self.saved_geo = geo
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            self.WINDOW_FILE.write_text(json.dumps({"geometry": list(geo)}))
        except OSError:
            pass

    def active_window(self):
        prop = self.ctl.root.get_full_property(self.ctl.NET_ACTIVE, self.xwin.X.AnyPropertyType)
        return prop.value[0] if prop and len(prop.value) else 0

    # -- buttons

    def guard(fn):
        def wrapped(self, *a):
            try:
                return fn(self, *a)
            except grpc.RpcError as e:
                notify(f"The emulator didn't respond ({e.code().name})")
        return wrapped

    @guard
    def key_down(self, name):
        self.emu.key(self.KEYS[name], True)

    @guard
    def key_up(self, name):
        self.emu.key(self.KEYS[name], False)

    @guard
    def action(self, name, widget):
        if self.shutting_down:
            return
        simple = {"back": Emulator.BACK, "home": Emulator.HOME, "recents": Emulator.RECENTS}
        if name in simple:
            self.emu.press(simple[name])
        elif name in ("rot_left", "rot_right"):
            self.rotate(+90 if name == "rot_left" else -90)
        elif name == "screenshot":
            self.screenshot()
        elif name == "zoom":
            self.zoom_menu(widget)
        elif name == "keys":
            self.open_editor()
        elif name == "apk":
            self.install_apps()
        elif name == "settings":
            self.open_settings()
        elif name == "toggle":
            self.ctl.set_enabled(not self.ctl.enabled)
        elif name == "more":
            self.more_menu(widget)
        elif name == "min":
            main, _ = self.xwin.emulator()
            if main is not None:
                self.xwin.minimise(main)
        elif name == "close":
            self.confirm_close()

    def rotate(self, degrees):
        # the emulator resets the window to a default size when it rotates; remember
        # the landscape size so turning back to landscape restores it
        main, _ = self.xwin.emulator()
        g = self.xwin.geometry(main) if main is not None else None
        if g and g[2] > g[3]:
            self.landscape_geo = g
        self.emu.rotate(degrees)
        self.root.after(1500, self.restore_landscape_size)

    def restore_landscape_size(self):
        main, _ = self.xwin.emulator()
        g = self.xwin.geometry(main) if main is not None else None
        saved = getattr(self, "landscape_geo", None)
        if g and saved and g[2] > g[3] and g[2:] != saved[2:]:
            self.xwin.move_resize(main, *saved)

    def screenshot(self):
        folder = pictures_dir() / APP
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{APP}_{datetime.datetime.now():%Y-%m-%d_%H-%M-%S}.png"
        path.write_bytes(self.emu.screenshot_png())
        print("screenshot", path, flush=True)
        notify(f"Screenshot saved to {path}")

    def popup(self, menu, widget):
        menu.update_idletasks()  # so its real width is known
        menu.tk_popup(widget.winfo_rootx() - 4 - menu.winfo_reqwidth(), widget.winfo_rooty())

    def zoom_menu(self, widget):
        tk = self.bar.tk
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Full screen", accelerator="F11", command=self.toggle_fullscreen)
        m.add_separator()
        m.add_command(label="Fit to screen", command=lambda: self.set_size(None))
        w, h = self.ctl.dev_w, self.ctl.dev_h
        for pct in (75, 50):
            m.add_command(label=f"{pct} %   ({w * pct // 100} × {h * pct // 100})",
                          command=lambda p=pct: self.set_size(p / 100))
        self.popup(m, widget)

    def set_size(self, scale):
        main, _ = self.xwin.emulator()
        geo = self.xwin.geometry(main) if main is not None else None
        if geo is None:
            return
        if "_NET_WM_STATE_FULLSCREEN" in self.xwin.net_state(main):
            self.xwin.set_fullscreen(main, False)
        x, y, w, h = geo
        ax, ay, aw, ah = self.xwin.workarea_for(x + w // 2, y + h // 2)
        title, margin = 32, 8
        avail_w, avail_h = aw - SideBar.W - 2 * margin, ah - title - 2 * margin
        fit = min(avail_w / self.ctl.dev_w, avail_h / self.ctl.dev_h)
        s = fit if scale is None else min(scale, fit)
        nw, nh = int(self.ctl.dev_w * s), int(self.ctl.dev_h * s)
        nx = ax + margin + (avail_w - nw) // 2
        ny = ay + title + margin + (avail_h - nh) // 2
        self.xwin.move_resize(main, nx, ny, nw, nh)

    def toggle_fullscreen(self):
        main, _ = self.xwin.emulator()
        if main is not None:
            on = "_NET_WM_STATE_FULLSCREEN" not in self.xwin.net_state(main)
            self.xwin.set_fullscreen(main, on)
            if on:
                notify("Full screen - press F11 to leave")

    def more_menu(self, widget):
        tk = self.bar.tk
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Edit controls…", command=self.open_editor)
        on = tk.BooleanVar(value=self.ctl.enabled)
        m.add_checkbutton(label="Keyboard controls", accelerator="`", variable=on,
                          command=lambda: self.ctl.set_enabled(on.get()))
        m.add_command(label="Full screen", accelerator="F11", command=self.toggle_fullscreen)
        m.add_separator()
        m.add_command(label="Install an app (APK)…", command=self.install_apps)
        m.add_command(label="Your apps…", command=lambda: self.open_settings("apps"))
        m.add_command(label="Open screenshots folder",
                      command=lambda: subprocess.Popen(["xdg-open", str(pictures_dir() / APP)]))
        m.add_separator()
        m.add_command(label=f"Restart {GAME_NAME}  (fixes a stuck dark screen)", command=self.confirm_restart_game)
        m.add_command(label="Restart Android", command=self.confirm_restart)
        m.add_separator()
        m.add_command(label="Settings…", command=self.open_settings)
        m.add_command(label=f"About {APP}", command=self.about)
        m._vars = (on,)  # keep the variable alive while the menu is open
        self.popup(m, widget)

    def open_editor(self):
        if self.editor is not None and self.editor.alive:
            self.editor.top.lift()
            return
        main, _ = self.xwin.emulator()
        geo = self.xwin.geometry(main) if main is not None else None

        def saved(prof):
            save_profile(self.profile_name, prof)
            self.ctl.set_profile(prof)
            mark_profile_seen(self.profile_name)
            notify("Controls saved")

        self.editor = Editor(self.root, self.ctl.prof, self.emu, saved, geo)

    def about(self):
        self.open_settings("about")

    def open_settings(self, page="performance"):
        if self.settings_win is not None and self.settings_win.alive:
            self.settings_win.show(page)
            self.settings_win.top.deiconify()
            self.settings_win.top.lift()
            return
        self.settings_win = SettingsWindow(self.root, self, page)

    def apply_settings(self, s):
        """Settings that take effect straight away (the hardware ones need a restart)."""
        self.settings = dict(s)
        self.hide_original = not s["show_emulator_bar"]

    def install_apps(self, paths=None):
        def chosen(paths):
            if isinstance(paths, list) and paths:
                self.install_dialog = InstallDialog(
                    self.root, paths, on_close=lambda pkgs: self.settings_win and self.settings_win.alive
                    and pkgs and self.settings_win.reload_apps())
        if paths:
            chosen(list(paths))
        else:
            run_in_thread(self.root, choose_app_files, chosen)

    def show_game(self):
        """Bring the game window to the front (and out of minimised)."""
        main, _ = self.xwin.emulator()
        if main is not None:
            self.xwin.activate(main)

    def restart_emulator(self):
        """Start the emulator again (new hardware settings): shut Android down, then
        the app starts itself over, loading screen and all."""
        self.restart_requested = True
        notify("Restarting Android with the new settings…")
        self.shutdown()

    # -- restart / shutdown

    def confirm_restart(self):
        if ask(self.root, "Restart Android?", "Open apps will close.", yes="Restart"):
            threading.Thread(target=self._restart, daemon=True).start()

    def confirm_restart_game(self):
        if ask(self.root, "Restart the game?", "It closes and opens again in a few seconds. "
                                               "Don't do this during a match.", yes="Restart"):
            threading.Thread(target=restart_game, daemon=True).start()

    def _restart(self):
        subprocess.run([ADB, "shell", "sync"], timeout=30, capture_output=True)
        subprocess.run([ADB, "reboot"], timeout=30, capture_output=True)
        subprocess.run([ADB, "wait-for-device"], timeout=180, capture_output=True)
        for _ in range(90):
            out = subprocess.run([ADB, "shell", "getprop", "sys.boot_completed"],
                                 capture_output=True, text=True).stdout.strip()
            if out == "1":
                break
            time.sleep(2)
        after_boot()

    def confirm_close(self):
        if ask(self.root, f"Close {APP}?", "Android will shut down properly first. "
                                           "This takes a few seconds.", yes="Close", danger=True):
            self.shutdown()

    def shutdown(self):
        """Shut Android down properly; the emulator exits once it has powered off."""
        if self.shutting_down:
            return
        self.shutting_down = True
        self.ctl.enabled = False
        self.ctl.stopped = True
        self.ctl.update_grabs()
        self.bar.hide()
        notify("Shutting down Android…")
        print("shutting down", flush=True)
        self.shutdown_started = time.time()
        if self.emu_pid is None:
            found = running_emulator()
            self.emu_pid = found[0] if found else None
        threading.Thread(target=power_off, daemon=True).start()
        self.root.after(500, self.wait_for_exit)

    def wait_for_exit(self):
        main, _ = self.xwin.emulator()
        gone = (not pid_alive(self.emu_pid)) if self.emu_pid else main is None
        gone = gone and running_emulator() is None
        if gone:
            reap_children()
            print("emulator closed cleanly" if not self.forced else "emulator closed (forced)", flush=True)
            self.root.quit()
            return
        if time.time() - self.shutdown_started > 60 and not self.forced:
            self.forced = True  # Android didn't power off in time: stop the emulator
            print("shutdown timed out, stopping the emulator", flush=True)
            subprocess.run([ADB, "emu", "kill"], timeout=10, capture_output=True)
        self.root.after(500, self.wait_for_exit)


def onscreen_keyboard():
    """Android's "use on-screen keyboard" switch (shown even with a physical keyboard)."""
    out = subprocess.run([ADB, "shell", "settings", "get", "secure", "show_ime_with_hard_keyboard"],
                         capture_output=True, text=True).stdout.strip()
    return out == "1"


def set_onscreen_keyboard(on):
    # Games made for phones expect a real on-screen keyboard to open and close
    # around typing; without one, MLBB can leave its typing backdrop on screen.
    subprocess.run([ADB, "shell", "settings", "put", "secure", "show_ime_with_hard_keyboard",
                    "1" if on else "0"], capture_output=True)
    notify("On-screen keyboard " + ("on: it pops up while you type" if on else "off"))


def restart_game():
    subprocess.run([ADB, "shell", "am", "force-stop", GAME], capture_output=True, timeout=20)
    time.sleep(1)
    subprocess.run([ADB, "shell", "monkey", "-p", GAME, "-c", "android.intent.category.LAUNCHER", "1"],
                   capture_output=True, timeout=20)


def power_off():
    for cmd in (["shell", "sync"], ["shell", "reboot", "-p"]):
        try:
            subprocess.run([ADB, *cmd], timeout=30, capture_output=True)
        except (OSError, subprocess.TimeoutExpired):
            pass


STATUS_BAR_DIMENS = ("status_bar_height_default", "status_bar_height_portrait", "status_bar_height_landscape")


def home_screen_state():
    """(installed, is the home app) for the Yellowstacks home screen."""
    installed = HOME_PKG in adb_shell(f"pm list packages {HOME_PKG}")
    current = adb_shell("cmd package resolve-activity --brief -c android.intent.category.HOME "
                        "-a android.intent.action.MAIN")
    return installed, HOME_PKG in current


def install_home_screen(log=print):
    """Put the Yellowstacks home screen on the phone and make it the home app."""
    if not HOME_APK.exists():
        log(f"no home screen to install at {HOME_APK} (build it with launcher/build.sh)")
        return False
    installed, _ = home_screen_state()
    if not installed:
        log("installing the Yellowstacks home screen")
        out = subprocess.run([ADB, "install", "-r", "-g", str(HOME_APK)],
                             capture_output=True, text=True, timeout=180)
        if "Success" not in (out.stdout + out.stderr):
            log(f"couldn't install it: {(out.stderr or out.stdout).strip()[:200]}")
            return False
    adb_shell(f"cmd package set-home-activity {HOME_ACTIVITY}")
    log("the Yellowstacks home screen is the home app")
    return True


def remove_home_screen(log=print):
    """Give Android its own launcher back."""
    installed, _ = home_screen_state()
    if installed:
        subprocess.run([ADB, "uninstall", HOME_PKG], capture_output=True, timeout=120)
    adb_shell(f"cmd package set-home-activity {LAUNCHER_PKG}/.NexusLauncherActivity")
    log("Android's own home screen is back")


def prepare_android():
    """Set Android up for gaming. Safe to run any time; the one step that makes
    running apps reload is skipped while the game is open (it happens next start)."""
    sh = lambda *a: subprocess.run([ADB, "shell", *a], capture_output=True, text=True, timeout=30).stdout
    # the pixel_6 profile's camera-hole overlays letterbox landscape games (the AVD is
    # renamed so the emulator stops turning them on; this catches older setups)
    for pkg in ("com.android.internal.emulation.pixel_6", "com.android.systemui.emulation.pixel_6"):
        sh("cmd", "overlay", "disable", pkg)
    try:  # self-healing: a reset or a reinstall shouldn't lose the home screen
        installed, is_home = home_screen_state()
        if installed and not is_home:
            sh("cmd", "package", "set-home-activity", HOME_ACTIVITY)
    except (OSError, subprocess.SubprocessError):
        pass
    # Empty the status bar first. Shrinking it (below) leaves its last picture frozen
    # on the home screen, so that picture must have nothing on it.
    hide_status_bar()
    time.sleep(0.8)
    # no status bar over the game: give it zero height with a fabricated resource
    # overlay (creating one needs root, and it's gone after every reboot). Changing
    # framework resources reloads apps.
    enabled = sh("cmd", "overlay", "list", "android")
    if any(f"[x] com.android.shell:ys_{r}" not in enabled for r in STATUS_BAR_DIMENS):
        if sh("pidof", GAME).strip():
            print("status bar removal waits for the next start (the game is open)", flush=True)
        else:
            subprocess.run([ADB, "root"], capture_output=True, timeout=30)
            time.sleep(2)
            subprocess.run([ADB, "wait-for-device"], capture_output=True, timeout=60)
            for r in STATUS_BAR_DIMENS:
                sh("cmd", "overlay", "fabricate", "--target", "android", "--name", f"ys_{r}",
                   f"android:dimen/{r}", "0x05", "0x00000001")  # 0dip
            # switching on straight after creating can be ignored; retry until listed as on
            for _ in range(10):
                listed = sh("cmd", "overlay", "list", "android")
                off = [r for r in STATUS_BAR_DIMENS if f"[x] com.android.shell:ys_{r}" not in listed]
                if not off:
                    break
                for r in off:
                    sh("cmd", "overlay", "enable", "--user", "0", f"com.android.shell:ys_{r}")
                time.sleep(1)
    hide_status_bar()


def hide_status_bar():
    """If the status bar is swiped in anyway, there's nothing on it and it can't be pulled down."""
    adb_shell("cmd statusbar send-disable-flag statusbar-expansion system-icons clock "
              "notification-icons notification-peek", 30)


def after_boot():
    """Things to redo every time Android starts."""
    prepare_android()
    start = load_settings()["start_app"]
    if start:
        open_app(start)


def pin_adb_to_emulator():
    """Point every adb call (ours and our child processes') at the emulator we
    control, found by its gRPC port, so a second emulator can't confuse it."""
    found = running_emulator()
    if found:
        os.environ["ANDROID_SERIAL"] = found[1]
        return found[1]
    return None


def pid_alive(pid):
    """Is the process still running? Yellowstacks starts the emulator itself, so the
    emulator is our child: it counts as running until it can be collected (and is
    collected here). Going by the "zombie" state instead was wrong: the emulator's
    main thread shows as a zombie while its other threads are still closing, and a
    child never collected stays a zombie that the emulator's AVD lock takes for a
    running emulator ("Running multiple emulators with the same AVD")."""
    try:
        return os.waitpid(pid, os.WNOHANG)[0] != pid
    except ChildProcessError:  # not our child
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def reap_children():
    """Collect any child processes of ours that have exited. After Yellowstacks starts
    itself over (exec), it doesn't know the emulator it started before is its child."""
    me = os.getpid()
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            state, ppid = (d / "stat").read_text().rsplit(")", 1)[1].split()[:2]
            if state == "Z" and int(ppid) == me:
                os.waitpid(int(d.name), os.WNOHANG)
        except (OSError, ValueError, ChildProcessError):
            pass


def pictures_dir():
    try:
        out = subprocess.run(["xdg-user-dir", "PICTURES"], capture_output=True, text=True).stdout.strip()
        if out:
            return Path(out)
    except OSError:
        pass
    return Path.home() / "Pictures"


def load_icon_argb():
    """The app icon as _NET_WM_ICON data (32 and 64 px)."""
    try:
        from PIL import Image
        img = Image.open(ICON).convert("RGBA")
    except Exception:
        return None
    data = []
    for size in (32, 64):
        im = img.resize((size, size), Image.LANCZOS)
        data += [size, size]
        px = im.tobytes()  # RGBA bytes
        data += [(px[i + 3] << 24) | (px[i] << 16) | (px[i + 1] << 8) | px[i + 2]
                 for i in range(0, len(px), 4)]
    return data


# ---------------------------------------------------------------- editor

class Editor:
    """Shows a screenshot of the game with the controls on top; edit them with the mouse."""

    TAP_R = 22
    JOY_KEYS = [  # offered in order when adding a joystick
        {"up": "w", "left": "a", "down": "s", "right": "d"},
        {"up": "Up", "left": "Left", "down": "Down", "right": "Right"},
        {"up": "i", "left": "j", "down": "k", "right": "l"},
        {"up": "KP_8", "left": "KP_4", "down": "KP_5", "right": "KP_6"},
    ]

    def __init__(self, master, prof, emu, on_save, geo=None):
        import tkinter as tk
        from PIL import Image, ImageTk
        self.tk = tk
        self.prof = copy.deepcopy(prof)
        self.on_save = on_save
        self.dev_w, self.dev_h = self.prof["device_size"]
        self.items = {}       # uid -> control (dicts from self.prof["controls"])
        self.order = []       # uids in profile order
        self.next_uid = 0
        self.selected = None
        self.drag = None
        self.capture = None   # key capture in progress
        self.images = {}      # smooth marker images; Tk needs them kept referenced
        self.glyphs = {}      # the Add buttons' icons, likewise
        self.alive = True

        # same size and place as the emulator screen, so it looks like an overlay
        if geo:
            x, y, w, h = geo
        else:
            x = y = None
            w, h = 960, 540
        self.s = min(w / self.dev_w, h / self.dev_h)
        cw, ch = int(self.dev_w * self.s), int(self.dev_h * self.s)

        self.top = top = tk.Toplevel(master)
        top.title("Edit controls")
        top.resizable(False, False)
        if x is not None:
            top.geometry(f"+{x}+{max(0, y - 30)}")
        top.configure(bg=BG)
        self.c = c = tk.Canvas(top, width=cw, height=ch, highlightthickness=0, bg=BG)
        c.pack()
        try:
            img = Image.open(io.BytesIO(emu.screenshot_png())).convert("RGB")
            img = img.resize((cw, ch))
            img = Image.blend(img, Image.new("RGB", img.size, (0, 0, 0)), 0.25)  # dim a little
            self.photo = ImageTk.PhotoImage(img)
            c.create_image(0, 0, image=self.photo, anchor="nw")
        except Exception:
            c.create_text(cw / 2, ch / 2, fill=MUTED, font=("Sans", 14),
                          text="(no screenshot - is the emulator running?)")

        # the toolbar: what you can add on the left, what you do with it on the right,
        # and one line underneath that says what the thing you picked can do
        bar = tk.Frame(c, bg=PANEL, padx=14, pady=9, highlightthickness=1, highlightbackground=LINE)
        row = tk.Frame(bar, bg=PANEL)
        row.pack(fill="x")
        bstyle = dict(bg=RAISED, fg=YELLOW, activebackground=YELLOW, activeforeground=BG,
                      relief="flat", bd=0, highlightthickness=0,  # Tk draws a pale ring otherwise
                      padx=11, pady=5, font=("Sans", 10, "bold"),
                      cursor="hand2", compound="left")
        tk.Label(row, text="Add", bg=PANEL, fg=MUTED,
                 font=("Sans", 9)).pack(side="left", padx=(0, 9))
        for kind, label, command in (("joystick", "Joystick", self.add_joystick),
                                     ("tap", "Tap", self.add_tap)):
            self.glyphs[kind] = paint_control_glyph(kind, 18)
            tk.Button(row, text=f" {label}", image=self.glyphs[kind], command=command,
                      **bstyle).pack(side="left", padx=(0, 7))
        tk.Frame(row, width=1, bg=LINE).pack(side="left", fill="y", padx=14, pady=2)
        tk.Button(row, text="Save", command=self.save,
                  **{**bstyle, "bg": YELLOW, "fg": BG, "activebackground": YELLOW_DOWN,
                     "padx": 18}).pack(side="right")
        tk.Button(row, text="Cancel", command=self.close,
                  **{**bstyle, "fg": TEXT}).pack(side="right", padx=(0, 7))
        self.hint = tk.Label(bar, bg=PANEL, fg=MUTED, font=("Sans", 9))
        self.hint.pack(pady=(9, 0))
        c.create_window(cw / 2, 10, window=bar, anchor="n")

        for ctl in self.prof["controls"]:
            self.track(ctl)
        self.redraw_all()
        self.show_hint()

        c.bind("<ButtonPress-1>", self.on_press)
        c.bind("<B1-Motion>", self.on_motion)
        c.bind("<ButtonRelease-1>", self.on_release)
        c.bind("<Double-Button-1>", self.on_double)
        c.bind("<ButtonPress-3>", self.on_menu)
        c.bind("<Button-4>", lambda e: self.on_wheel(e, +1))
        c.bind("<Button-5>", lambda e: self.on_wheel(e, -1))
        top.bind("<KeyPress>", self.on_key)
        top.protocol("WM_DELETE_WINDOW", self.close)
        top.after(100, lambda: (top.lift(), top.focus_force()))

    # -- model

    def track(self, ctl):
        uid = self.next_uid
        self.next_uid += 1
        self.items[uid] = ctl
        self.order.append(uid)
        return uid

    def controls(self):
        return [self.items[u] for u in self.order]

    def profile_view(self):
        return {**self.prof, "controls": self.controls()}

    # -- drawing

    def redraw_all(self):
        for uid in self.order:
            self.draw(uid)

    def draw(self, uid):
        c, s = self.c, self.s
        tag = f"u{uid}"
        c.delete(tag)
        ctl = self.items[uid]
        x, y = ctl["pos"][0] * s, ctl["pos"][1] * s
        selected = uid == self.selected
        if ctl["type"] == "tap":
            c.create_image(x, y, image=self.marker("tap", self.TAP_R, selected), tags=(tag,))
            c.create_text(x, y, text=key_label(ctl["key"]) if ctl.get("key") else "?",
                          fill=BG, font=("Sans", 11, "bold"), tags=(tag,))
        else:
            R = round(ctl["radius"] * s)  # whole pixels, so the image cache stays small
            c.create_image(x, y, image=self.marker("joystick", R, selected), tags=(tag,))
            for d, (dx, dy) in DIRS.items():
                kx, ky = x + dx * R * 0.6, y + dy * R * 0.6
                k = ctl["keys"].get(d)
                c.create_text(kx, ky, text=key_label(k) if k else "?", fill=YELLOW,
                              font=("Sans", 9, "bold"), tags=(tag,))
        c.tag_raise("banner")

    # what the mouse can do here, for whatever is picked
    HINTS = {
        None: "Drag to move  ·  Double-click to change keys  ·  Right-click for more",
        "tap": "Double-click to change its key  ·  Delete removes it  ·  Right-click for more",
        "joystick": "Scroll to resize  ·  Double-click to change its keys  ·  Delete removes it",
    }
    NOTHING_YET = "Add a joystick or a tap to start mapping the keyboard"

    def show_hint(self):
        if not self.items:
            text = self.NOTHING_YET
        elif self.selected in self.items:
            text = self.HINTS[self.items[self.selected]["type"]]
        else:
            text = self.HINTS[None]
        self.hint.configure(text=text)

    def show_banner(self, text, color=YELLOW):
        c = self.c
        c.delete("banner")
        if not text:
            return
        cw = int(c["width"])
        t = c.create_text(cw / 2, 96, text=text, fill=BG if color == YELLOW else "white",
                          font=("Sans", 13, "bold"),
                          tags=("banner",))
        x1, y1, x2, y2 = c.bbox(t)
        r = c.create_rectangle(x1 - 14, y1 - 8, x2 + 14, y2 + 8, fill=color, outline="",
                               tags=("banner",))
        c.tag_lower(r, t)

    # -- hit testing / selection

    def marker(self, kind, size, selected):
        key = (kind, size, selected)
        if key not in self.images:
            self.images[key] = (paint_tap if kind == "tap" else paint_joystick)(size, selected)
        return self.images[key]

    def uid_at(self, x, y):
        for item in reversed(self.c.find_overlapping(x - 1, y - 1, x + 1, y + 1)):
            for t in self.c.gettags(item):
                if t.startswith("u") and t[1:].isdigit() and self.hits(int(t[1:]), x, y):
                    return int(t[1:])
        return None

    def hits(self, uid, x, y):
        """Is (x, y) on the control's round shape? Its image is a square with see-through
        corners, and those shouldn't catch clicks meant for what's underneath."""
        ctl = self.items[uid]
        cx, cy = ctl["pos"][0] * self.s, ctl["pos"][1] * self.s
        if ctl["type"] == "tap":
            return math.hypot(x - cx, y - cy) <= self.TAP_R + 3
        R = round(ctl["radius"] * self.s)
        return math.hypot(x - cx, y - cy) <= R + 3 or any(
            math.hypot(x - cx - dx * R * 0.6, y - cy - dy * R * 0.6) <= 17 for dx, dy in DIRS.values())

    def select(self, uid):
        old, self.selected = self.selected, uid
        for u in (old, uid):
            if u is not None and u in self.items:
                self.draw(u)
        self.show_hint()

    # -- mouse

    def on_press(self, e):
        if self.capture:
            return
        uid = self.uid_at(e.x, e.y)
        self.select(uid)
        if uid is not None:
            self.drag = {"uid": uid, "x": e.x, "y": e.y, "dx": 0, "dy": 0}

    def on_motion(self, e):
        if not self.drag:
            return
        d = self.drag
        self.c.move(f"u{d['uid']}", e.x - d["x"], e.y - d["y"])
        d["dx"] += e.x - d["x"]
        d["dy"] += e.y - d["y"]
        d["x"], d["y"] = e.x, e.y

    def on_release(self, e):
        d, self.drag = self.drag, None
        if not d or (d["dx"] == 0 and d["dy"] == 0):
            return
        ctl = self.items[d["uid"]]
        nx = ctl["pos"][0] + d["dx"] / self.s
        ny = ctl["pos"][1] + d["dy"] / self.s
        ctl["pos"] = [round(min(max(nx, 0), self.dev_w - 1)), round(min(max(ny, 0), self.dev_h - 1))]
        self.draw(d["uid"])

    def on_double(self, e):
        uid = self.uid_at(e.x, e.y)
        if uid is not None:
            self.start_capture(uid)

    def on_wheel(self, e, step):
        uid = self.uid_at(e.x, e.y)
        if uid is None or self.items[uid]["type"] != "joystick":
            return
        ctl = self.items[uid]
        ctl["radius"] = int(min(max(ctl["radius"] + step * 10, 40), 400))
        self.draw(uid)

    def on_menu(self, e):
        if self.capture:
            return
        tk = self.tk
        menu = tk.Menu(self.top, tearoff=0)
        uid = self.uid_at(e.x, e.y)
        if uid is not None:
            self.select(uid)
            what = "keys" if self.items[uid]["type"] == "joystick" else "key"
            menu.add_command(label=f"Change {what}…", command=lambda: self.start_capture(uid))
            menu.add_command(label="Delete", command=lambda: self.delete(uid))
        else:
            pos = [round(e.x / self.s), round(e.y / self.s)]
            menu.add_command(label="Add joystick here", command=lambda: self.add_joystick(pos))
            menu.add_command(label="Add tap here", command=lambda: self.add_tap(pos))
        menu.tk_popup(e.x_root, e.y_root)

    # -- add / delete

    def add_joystick(self, pos=None):
        if self.capture:
            return
        taken = used_keys(self.profile_view())
        keys = next((k for k in self.JOY_KEYS if not any(v in taken for v in k.values())), None)
        ctl = {"type": "joystick", "keys": dict(keys or {}),
               "pos": pos or [round(self.dev_w * 0.16), round(self.dev_h * 0.75)], "radius": 130}
        self.prof["controls"].append(ctl)
        uid = self.track(ctl)
        self.select(uid)
        self.draw(uid)
        if keys is None:
            self.start_capture(uid, new=True)

    def add_tap(self, pos=None):
        if self.capture:
            return
        ctl = {"type": "tap", "key": "", "pos": pos or [self.dev_w // 2, self.dev_h // 2]}
        self.prof["controls"].append(ctl)
        uid = self.track(ctl)
        self.select(uid)
        self.draw(uid)
        self.start_capture(uid, new=True)

    def delete(self, uid):
        if uid not in self.items:
            return
        self.c.delete(f"u{uid}")
        ctl = self.items.pop(uid)
        self.order.remove(uid)
        self.prof["controls"] = [c for c in self.prof["controls"] if c is not ctl]
        if self.selected == uid:
            self.selected = None
        self.show_hint()

    # -- key capture

    def start_capture(self, uid, new=False):
        ctl = self.items[uid]
        steps = list(DIRS) if ctl["type"] == "joystick" else ["key"]
        self.capture = {"uid": uid, "steps": steps, "i": 0, "keys": {}, "new": new}
        self.select(uid)
        self.prompt()
        self.top.focus_force()

    def prompt(self, error=None):
        cap = self.capture
        step = cap["steps"][cap["i"]]
        what = "this tap" if step == "key" else f"joystick {step.upper()}"
        text = f"Press the key for {what}   (Esc to cancel)"
        self.show_banner(f"{error}\n{text}" if error else text, DANGER if error else YELLOW)

    def on_key(self, e):
        name = e.keysym
        if not self.capture:
            if name in ("Delete", "BackSpace") and self.selected is not None:
                self.delete(self.selected)
            elif name == "Escape":
                self.select(None)
            return
        cap = self.capture
        if name == "Escape":
            self.end_capture(cancel=True)
            return
        if len(name) == 1:
            name = name.lower()
        if not valid_key(name):
            self.prompt(f"{name} can't be used")
            return
        ctl = self.items[cap["uid"]]
        taken = used_keys(self.profile_view(), skip=ctl)
        if name in taken:
            self.prompt(f"{key_label(name)} is already used by {taken[name]}")
            return
        if name in cap["keys"].values():
            self.prompt(f"{key_label(name)} is already used by this joystick")
            return
        cap["keys"][cap["steps"][cap["i"]]] = name
        cap["i"] += 1
        if cap["i"] < len(cap["steps"]):
            self.prompt()
            return
        if ctl["type"] == "tap":
            ctl["key"] = cap["keys"]["key"]
        else:
            ctl["keys"] = cap["keys"]
        self.end_capture()

    def end_capture(self, cancel=False):
        cap, self.capture = self.capture, None
        self.show_banner(None)
        if cancel and cap["new"]:
            ctl = self.items[cap["uid"]]
            if (ctl["type"] == "tap" and not ctl["key"]) or (ctl["type"] == "joystick" and len(ctl["keys"]) < 4):
                self.delete(cap["uid"])
                return
        self.draw(cap["uid"])

    # -- save / close

    def save(self):
        if self.capture:
            return
        bad = [c for c in self.controls()
               if (c["type"] == "tap" and not c.get("key")) or (c["type"] == "joystick" and len(c["keys"]) < 4)]
        if bad:
            self.show_banner("Some controls have no key yet - double-click them to set one", DANGER)
            return
        self.prof["controls"] = self.controls()
        self.on_save(copy.deepcopy(self.prof))
        self.close()

    def close(self):
        self.alive = False
        self.top.destroy()


def valid_key(name):
    from Xlib import XK
    return bool(XK.string_to_keysym(name)) and name not in ("Escape", "??")


# ---------------------------------------------------------------- shared widgets

def styled_button(parent, text, command, primary=False, danger=False, small=False, bg=None):
    import tkinter as tk
    fill = DANGER if danger else (YELLOW if primary else RAISED)
    return tk.Button(parent, text=text, command=command, bg=fill,
                     fg="white" if danger else (BG if primary else TEXT),
                     activebackground=YELLOW_DOWN if primary else LINE,
                     activeforeground=BG if primary else TEXT, disabledforeground=MUTED,
                     relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                     padx=10 if small else 16, pady=3 if small else 6,
                     font=("Sans", 9 if small else 10, "bold"))


def run_in_thread(widget, work, then):
    """Run work() in a thread and then(result) back in Tk's thread, so the UI never freezes."""
    box = {}

    def worker():
        try:
            box["result"] = work()
        except Exception as e:  # handed to `then` as the result
            box["result"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    def poll():
        if t.is_alive():
            widget.after(80, poll)
        else:
            try:
                then(box.get("result"))
            except Exception as e:
                print("callback failed:", e, flush=True)
    widget.after(80, poll)


def center_on_screen(top, up=0.33, size=None):
    """Put a window in the middle of the monitor the mouse is on."""
    from Xlib import display
    top.update_idletasks()
    w, h = size or (top.winfo_reqwidth(), top.winfo_reqheight())
    try:
        d = display.Display()
        px, py = top.winfo_pointerxy()
        ax, ay, aw, ah = XWindows(d).workarea_for(px, py)
        d.close()
    except Exception:
        ax, ay, aw, ah = 0, 0, top.winfo_screenwidth(), top.winfo_screenheight()
    top.geometry(f"+{ax + (aw - w) // 2}+{ay + int((ah - h) * up)}")


def window_icon(top):
    """Give a window the Yellowstacks icon."""
    import tkinter as tk
    try:
        if not hasattr(window_icon, "img"):
            from PIL import Image, ImageTk
            window_icon.img = ImageTk.PhotoImage(Image.open(ICON).convert("RGBA").resize((64, 64), Image.LANCZOS))
        top.iconphoto(False, window_icon.img)
    except (tk.TclError, OSError, ImportError):
        pass


def logo_photo(size, bg=None):
    """The app logo as a Tk image, `size` px; on a solid colour if bg is given."""
    from PIL import Image, ImageTk
    img = Image.open(ICON).convert("RGBA").resize((size, size), Image.LANCZOS)
    if bg:
        img = Image.alpha_composite(Image.new("RGBA", img.size, bg), img).convert("RGB")
    return ImageTk.PhotoImage(img)


class Switch:
    """An on/off switch, drawn smooth."""

    W, H = 44, 24
    images = {}

    def __init__(self, parent, value, command=None, bg=BG, enabled=True):
        import tkinter as tk
        self.value, self.command, self.bg, self.enabled = bool(value), command, bg, enabled
        self.hover = False
        self.c = tk.Canvas(parent, width=self.W, height=self.H, bg=bg, highlightthickness=0, bd=0,
                           cursor="hand2" if enabled else "")
        self.c.bind("<ButtonRelease-1>", self.toggle)
        self.c.bind("<Enter>", lambda e: self.set_hover(True))
        self.c.bind("<Leave>", lambda e: self.set_hover(False))
        self.draw()

    def set_hover(self, on):
        self.hover = on and self.enabled
        self.draw()

    def toggle(self, _=None):
        if not self.enabled:
            return
        self.value = not self.value
        self.draw()
        if self.command:
            self.command(self.value)

    def draw(self):
        key = (self.value, self.hover, self.enabled, self.bg)
        if key not in Switch.images:
            W, H = self.W, self.H
            p = Painter(W, H, self.bg)
            if self.value:
                track, knob = (YELLOW if self.enabled else MUTED), BG
            else:
                track, knob = (MUTED if self.hover else LINE), (TEXT if self.enabled else MUTED)
            p.rrect(1, 2, W - 1, H - 2, (H - 4) / 2, fill=track)
            p.dot(W - H / 2 if self.value else H / 2, H / 2, H / 2 - 5, knob)
            Switch.images[key] = p.photo()
        self.c.delete("all")
        self.c.create_image(0, 0, anchor="nw", image=Switch.images[key])


class Choice:
    """A row of options to pick one from; the chosen one is yellow."""

    def __init__(self, parent, options, value, command=None, bg=BG):
        """options: [(value, label, enabled)]."""
        import tkinter as tk
        self.command = command
        self.frame = tk.Frame(parent, bg=bg)
        self.labels = {}
        for v, text, enabled in options:
            lab = tk.Label(self.frame, text=text, font=("Sans", 9, "bold"), padx=13, pady=6,
                           cursor="hand2" if enabled else "")
            lab.pack(side="left", padx=(0, 4))
            if enabled:
                lab.bind("<ButtonRelease-1>", lambda e, v=v: self.set(v, user=True))
                lab.bind("<Enter>", lambda e, v=v: v != self.value and self.labels[v][0].configure(bg=LINE))
                lab.bind("<Leave>", lambda e: self.paint())
            self.labels[v] = (lab, enabled)
        self.set(value)

    def set(self, v, user=False):
        self.value = v
        self.paint()
        if user and self.command:
            self.command(v)

    def paint(self):
        for k, (lab, enabled) in self.labels.items():
            if k == self.value:
                lab.configure(bg=YELLOW, fg=BG)
            else:
                lab.configure(bg=RAISED if enabled else PANEL, fg=TEXT if enabled else "#4d4a44")


class Dropdown:
    """A button that opens a list to choose from."""

    def __init__(self, parent, options, value, command=None, width=34):
        import tkinter as tk
        self.command = command
        self.mb = tk.Menubutton(parent, bg=RAISED, fg=TEXT, activebackground=LINE, activeforeground=TEXT,
                                relief="flat", bd=0, highlightthickness=0, padx=12, pady=6, anchor="w",
                                font=("Sans", 9, "bold"), width=width, cursor="hand2")
        self.menu = tk.Menu(self.mb, tearoff=0)
        self.mb["menu"] = self.menu
        self.set_options(options, value)

    def set_options(self, options, value):
        self.options = options
        self.menu.delete(0, "end")
        for v, label in options:
            self.menu.add_command(label=label, command=lambda v=v: self.set(v, user=True))
        self.set(value)

    def set(self, v, user=False):
        self.value = v
        label = next((lab for val, lab in self.options if val == v), v)
        self.mb.configure(text=f"{label}  ▾")
        if user and self.command:
            self.command(v)


class Slider:
    """A smooth slider for a whole number: drag or click along it, or scroll over it."""

    H = 30          # the track's height; tick labels go under it
    R = 9           # knob radius
    images = {}

    def __init__(self, parent, lo, hi, step, value, command=None, width=360, ticks=(), unit="", bg=BG):
        import tkinter as tk
        self.lo, self.hi, self.step, self.W = lo, hi, step, width
        self.command, self.unit, self.bg = command, unit, bg
        self.hover = self.dragging = False
        self.value = None
        self.frame = tk.Frame(parent, bg=bg)
        self.c = tk.Canvas(self.frame, width=width, height=self.H + (18 if ticks else 0), bg=bg,
                           highlightthickness=0, bd=0, cursor="hand2")
        self.c.pack(side="left")
        self.label = tk.Label(self.frame, bg=bg, fg=YELLOW, font=("Sans", 11, "bold"), anchor="w", width=8)
        self.label.pack(side="left", anchor="n", padx=(12, 0), pady=(self.H // 2 - 11, 0))
        self.track = self.c.create_image(0, 0, anchor="nw")
        for t in ticks:
            self.c.create_text(self.x_of(t), self.H + 7, text=str(t), fill=MUTED, font=("Sans", 8))
        self.c.bind("<ButtonPress-1>", self.press)
        self.c.bind("<B1-Motion>", lambda e: self.set(self.value_at(e.x), user=True))
        self.c.bind("<ButtonRelease-1>", self.release)
        self.c.bind("<Button-4>", lambda e: self.set(self.value + step, user=True))
        self.c.bind("<Button-5>", lambda e: self.set(self.value - step, user=True))
        self.c.bind("<Enter>", lambda e: self.set_hover(True))
        self.c.bind("<Leave>", lambda e: self.set_hover(False))
        self.set(value)

    def x_of(self, v):
        m = self.R + 2
        return m + (v - self.lo) / (self.hi - self.lo) * (self.W - 2 * m)

    def value_at(self, x):
        m = self.R + 2
        return self.lo + (x - m) / (self.W - 2 * m) * (self.hi - self.lo)

    def set(self, v, user=False):
        v = self.lo + round((v - self.lo) / self.step) * self.step
        v = max(self.lo, min(self.hi, int(v)))
        if v == self.value:
            return
        self.value = v
        self.label.configure(text=f"{v} {self.unit}".strip())
        self.draw()
        if user and self.command:
            self.command(v)

    def press(self, e):
        self.dragging = True
        self.set(self.value_at(e.x), user=True)
        self.draw()

    def release(self, _=None):
        self.dragging = False
        self.draw()

    def set_hover(self, on):
        self.hover = on
        self.draw()

    def draw(self):
        active = self.hover or self.dragging
        key = (self.value, active, self.lo, self.hi, self.W, self.bg)
        if key not in Slider.images:
            W, H, R = self.W, self.H, self.R
            p = Painter(W, H, self.bg)
            cy, m, x = H / 2, R + 2, self.x_of(self.value)
            p.rrect(m, cy - 3, W - m, cy + 3, 3, fill=MUTED if active else LINE)
            p.rrect(m, cy - 3, max(x, m + 6), cy + 3, 3, fill=YELLOW)
            p.dot(x, cy, R + (1 if active else 0), YELLOW)
            p.dot(x, cy, 3.5, self.bg)
            Slider.images[key] = p.photo()
        self.c.itemconfigure(self.track, image=Slider.images[key])


class Scrollable:
    """A frame that scrolls with the mouse wheel."""

    def __init__(self, parent, bg=BG, height=260):
        import tkinter as tk
        self.outer = tk.Frame(parent, bg=bg)
        self.canvas = tk.Canvas(self.outer, bg=bg, highlightthickness=0, bd=0, height=height)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.win = self.canvas.create_window(0, 0, anchor="nw", window=self.inner)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.win, width=e.width))

    def bind_wheel(self, widget=None):
        """Scroll when the wheel turns over `widget` or any of its children."""
        widget = widget or self.inner
        widget.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-2, "units"))
        widget.bind("<Button-5>", lambda e: self.canvas.yview_scroll(2, "units"))
        for child in widget.winfo_children():
            self.bind_wheel(child)


def generic_app_icon(size, bg=RAISED):
    """A placeholder app icon: a rounded tile with a down arrow."""
    p = Painter(size, size)
    p.rrect(1, 1, size - 1, size - 1, size * 0.24, fill=bg)
    x, y, k = size / 2, size / 2, size / 40
    p.line([x, y - 9 * k, x, y + 4 * k], YELLOW, 2.4 * k)
    p.line([x - 6 * k, y - 2 * k, x, y + 4 * k, x + 6 * k, y - 2 * k], YELLOW, 2.4 * k)
    p.line([x - 10 * k, y + 9 * k, x + 10 * k, y + 9 * k], YELLOW, 2.4 * k)
    return p.photo()


def app_icon_photo(png, size):
    """A Tk image from the PNG bytes of an Android app icon (None if unreadable)."""
    try:
        from PIL import Image, ImageTk
        img = Image.open(io.BytesIO(png)).convert("RGBA").resize((size, size), Image.LANCZOS)
        return ImageTk.PhotoImage(img)
    except Exception:
        return None


# ---------------------------------------------------------------- installing apps

APP_FILES = (".apk", ".xapk", ".apks", ".apkm")

INSTALL_HELP = {
    "INSTALL_FAILED_NO_MATCHING_ABIS":
        "This app is made for a kind of processor Yellowstacks can't run (only 32-bit ARM). "
        "Look for an arm64-v8a or x86_64 version.",
    "INSTALL_FAILED_UPDATE_INCOMPATIBLE":
        "A copy of this app from a different source is already installed. Remove it first "
        "(its data on this phone is deleted) and then install this one.",
    "INSTALL_FAILED_VERSION_DOWNGRADE": "A newer version of this app is already installed.",
    "INSTALL_FAILED_INSUFFICIENT_STORAGE": "Android is out of space. Remove some apps and try again.",
    "INSTALL_FAILED_MISSING_SPLIT":
        "This app comes in several parts. Install its whole bundle (.xapk or .apks), "
        "or pick all of its .apk files together.",
    "INSTALL_FAILED_OLDER_SDK": "This app needs a newer Android than the one in Yellowstacks (13).",
    "INSTALL_PARSE_FAILED_NO_CERTIFICATES": "This APK isn't signed, so Android won't install it.",
    "INSTALL_FAILED_INVALID_APK": "This file isn't a working APK. It may be damaged or only partly downloaded.",
    "INSTALL_PARSE_FAILED_NOT_APK": "This file isn't a working APK. It may be damaged or only partly downloaded.",
    "INSTALL_FAILED_TEST_ONLY": "This is a developer test build, which Android won't install normally.",
}


class InstallError(Exception):
    def __init__(self, message, code=None, package=None):
        super().__init__(message)
        self.code, self.package = code, package


def manifest_info(apk):
    """(package, label) from an APK's binary AndroidManifest.xml. The label is only
    found when it's written out (usually it points into the app's resources: None)."""
    try:
        with zipfile.ZipFile(apk) as z:
            data = z.read("AndroidManifest.xml")
    except (OSError, KeyError, zipfile.BadZipFile, RuntimeError):
        return None, None
    strings, package, label = [], None, None
    pos = 8  # after the file's own chunk header
    try:
        while pos + 8 <= len(data):
            ctype, hsize, size = struct.unpack_from("<HHI", data, pos)
            if size < 8:
                break
            if ctype == 0x0001:  # string pool
                count, _, flags, start, _ = struct.unpack_from("<5I", data, pos + 8)
                for off in struct.unpack_from(f"<{count}I", data, pos + hsize):
                    p = pos + start + off
                    if flags & 0x100:  # UTF-8: char count, byte count, bytes
                        p += 2 if data[p] & 0x80 else 1
                        n = data[p]
                        if n & 0x80:
                            n, p = ((n & 0x7F) << 8) | data[p + 1], p + 2
                        else:
                            p += 1
                        strings.append(data[p:p + n].decode("utf-8", "replace"))
                    else:  # UTF-16
                        n = struct.unpack_from("<H", data, p)[0]
                        p += 2
                        if n & 0x8000:
                            n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", data, p)[0]
                            p += 2
                        strings.append(data[p:p + 2 * n].decode("utf-16-le", "replace"))
            elif ctype == 0x0102:  # start of an element
                name, attr_start, attr_size, attr_count = struct.unpack_from("<IHHH", data, pos + 20)
                tag = strings[name] if name < len(strings) else ""
                for i in range(attr_count):
                    a = pos + 16 + attr_start + i * attr_size
                    _, aname, raw = struct.unpack_from("<III", data, a)
                    key = strings[aname] if aname < len(strings) else ""
                    val = strings[raw] if raw < len(strings) else None
                    if tag == "manifest" and key == "package":
                        package = val
                    elif tag == "application" and key == "label":
                        label = val
                if tag == "application":
                    break
            pos += size
    except (struct.error, IndexError):
        pass
    return package, label


def unpack_bundle(path, dest):
    """Take the APKs and any OBB game data out of an .xapk / .apks / .apkm bundle.
    Returns ([apk paths], [(obb path, folder name or None)])."""
    try:
        z = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile):
        raise InstallError("This bundle can't be opened. It may be damaged, or locked by the "
                           "site it came from (newer .apkm files need APKMirror's own app).")
    with z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        apks = [n for n in names if n.lower().endswith(".apk") and not n.startswith("standalones/")]
        if any(n.startswith("splits/") for n in apks):  # bundletool .apks: just the split set
            apks = [n for n in apks if n.startswith("splits/")]
        if not apks:
            raise InstallError("There's no app inside this bundle.")
        out_apks, out_obbs = [], []
        for i, n in enumerate(sorted(apks)):
            safe = re.sub(r"[^\w.-]", "_", Path(n).name)
            target = dest / f"{i:03d}-{safe}"
            with z.open(n) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            out_apks.append(target)
        for n in names:
            if n.lower().endswith(".obb"):
                parts = Path(n).parts
                folder = parts[-2] if len(parts) >= 2 and re.fullmatch(r"[\w.]+", parts[-2]) and parts[-2] != "obb" else None
                target = dest / re.sub(r"[^\w.-]", "_", Path(n).name)
                with z.open(n) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                out_obbs.append((target, folder))
    return out_apks, out_obbs


def adb_install(apks, downgrade=False):
    cmd = [ADB, "install-multiple" if len(apks) > 1 else "install", "-r"]
    if downgrade:
        cmd.append("-d")
    for attempt in (1, 2):
        try:
            res = subprocess.run(cmd + [str(a) for a in apks], capture_output=True, text=True, timeout=1800)
        except subprocess.TimeoutExpired:
            raise InstallError("Installing took too long (over 30 minutes) and was stopped.")
        out = (res.stdout + "\n" + res.stderr).strip()
        if res.returncode == 0 and "Success" in out:
            return
        m = re.search(r"\b(INSTALL_[A-Z_]+)", out)
        if m or attempt == 2:
            break
        # no reason from Android: the connection dropped (adb restarting). Try once more.
        end = time.time() + 30
        while not adb_ready() and time.time() < end:
            time.sleep(1)
    code = m.group(1) if m else None
    last = out.splitlines()[-1] if out else "adb didn't say why."
    raise InstallError(INSTALL_HELP.get(code, last), code)


def install_files(paths, status, downgrade=False, replace=False):
    """Install app files (APKs, split APKs picked together, or bundles) into Android.
    status(text) reports what's happening. Returns the installed package names."""
    installed = []
    with tempfile.TemporaryDirectory(prefix="yellowstacks-") as tmp:
        groups = []  # [(apk paths, obbs)]
        plain = [Path(p) for p in paths if str(p).lower().endswith(".apk")]
        for p in paths:
            if not str(p).lower().endswith(".apk"):
                status(f"Unpacking {Path(p).name}…")
                d = Path(tempfile.mkdtemp(dir=tmp))
                groups.append(unpack_bundle(p, d))
        # several plain APKs: parts of one app go in together, other apps one by one
        by_package = {}
        for apk in plain:
            pkg, _ = manifest_info(apk)
            by_package.setdefault(pkg or str(apk), []).append(apk)
        groups = [(apks, []) for apks in by_package.values()] + groups
        for n, (apks, obbs) in enumerate(groups, 1):
            pkg = next((p for p in (manifest_info(a)[0] for a in apks) if p), None)
            size = sum(a.stat().st_size for a in apks) / 1e6
            count = f" ({n} of {len(groups)})" if len(groups) > 1 else ""
            if replace and pkg:
                status(f"Removing the old copy{count}…")
                subprocess.run([ADB, "uninstall", pkg], capture_output=True, timeout=120)
            status(f"Installing{count}: copying {size:.0f} MB into Android…")
            try:
                adb_install(apks, downgrade)
            except InstallError as e:
                e.package = pkg
                raise
            for obb, folder in obbs:
                dest = f"/sdcard/Android/obb/{folder or pkg}/{obb.name}"
                status(f"Copying game data ({obb.stat().st_size / 1e6:.0f} MB)…")
                adb_shell(f"mkdir -p '/sdcard/Android/obb/{folder or pkg}'")
                subprocess.run([ADB, "push", str(obb), dest], capture_output=True, timeout=3600)
            installed.append(pkg)
    return installed


def show_yellowstacks():
    """Start Yellowstacks, or bring its window to the front if it's running."""
    subprocess.Popen([str(HERE / "yellowstacks")], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def choose_app_files():
    """Ask for app files with the desktop's file picker (zenity), else Tk's own."""
    try:
        res = subprocess.run(
            ["zenity", "--file-selection", "--multiple", "--separator=\n", "--title=Install an app",
             "--file-filter=Android apps (.apk .xapk .apks .apkm) | *.apk *.APK *.xapk *.apks *.apkm",
             "--file-filter=All files | *"], capture_output=True, text=True)
        if res.returncode in (0, 1):  # 1 = cancelled
            return [line for line in res.stdout.splitlines() if line]
    except OSError:
        pass
    from tkinter import filedialog
    return list(filedialog.askopenfilenames(
        title="Install an app", filetypes=[("Android apps", "*.apk *.xapk *.apks *.apkm"), ("All files", "*")]))


class InstallDialog:
    """Installs app files into Android and shows how it's going."""

    def __init__(self, master, paths, on_close=None, wait_for_android=False):
        import tkinter as tk
        self.tk, self.paths, self.on_close = tk, [str(p) for p in paths], on_close
        self.wait_for_android = wait_for_android
        self.busy = False
        self.alive = True
        self.hidden = False
        self.packages = []
        self.status_text = ""
        self.top = top = tk.Toplevel(master, class_="Yellowstacks")
        top.title("Install app")
        top.configure(bg=BG, padx=24, pady=20)
        top.resizable(False, False)
        window_icon(top)
        row = tk.Frame(top, bg=BG)
        row.pack(fill="x")
        self.icon = tk.Canvas(row, width=56, height=56, bg=BG, highlightthickness=0)
        self.icon.pack(side="left", anchor="n")
        self.icon_img = generic_app_icon(56)
        self.icon.create_image(0, 0, anchor="nw", image=self.icon_img)
        texts = tk.Frame(row, bg=BG)
        texts.pack(side="left", fill="x", expand=True, padx=(16, 0))
        names = ", ".join(Path(p).name for p in self.paths)
        self.title = tk.Label(texts, text=f"Installing {names}", bg=BG, fg=TEXT, font=("Sans", 12, "bold"),
                              anchor="w", justify="left", wraplength=340)
        self.title.pack(fill="x")
        self.status = tk.Label(texts, text="Getting ready…", bg=BG, fg=MUTED, font=("Sans", 9),
                               anchor="w", justify="left", wraplength=340)
        self.status.pack(fill="x", pady=(4, 0))
        self.bar = tk.Canvas(top, width=412, height=6, bg=BG, highlightthickness=0)
        self.bar.pack(pady=(18, 0))
        self.buttons = tk.Frame(top, bg=BG)
        self.buttons.pack(fill="x", pady=(16, 0))
        styled_button(self.buttons, "Hide", self.hide).pack(side="right")
        top.protocol("WM_DELETE_WINDOW", self.hide)
        top.bind("<Escape>", lambda e: self.hide())
        self.phase = 0.0
        self.above = None     # the game window we're attached to
        self.ticks = 0
        try:
            from Xlib import display
            self.xwin = XWindows(display.Display())
            top.update_idletasks()
            self.wrapper = self.xwin.d.create_resource_object("window", top.winfo_id()).query_tree().parent
        except Exception:
            self.xwin = None
        self.animate()
        center_on_screen(top)
        self.start()

    # -- running

    def start(self, downgrade=False, replace=False):
        self.busy = True
        self.set_buttons([("Hide", self.hide, {})])
        run_in_thread(self.top, lambda: self.work(downgrade, replace), self.finished)

    def report(self, text):
        self.status_text = text  # picked up by animate() in Tk's thread

    def work(self, downgrade, replace):
        missing = [p for p in self.paths if not Path(p).is_file()]
        if missing:
            raise InstallError(f"{Path(missing[0]).name} isn't there any more.")
        if not running_emulator():
            if not self.wait_for_android:
                raise InstallError("Android isn't running. Start Yellowstacks first.")
            self.report("Starting Yellowstacks…")
            show_yellowstacks()
            time.sleep(2)  # it needs a moment before it says it's starting Android
        end = time.time() + 300
        while still_booting() or not (running_emulator() and pin_adb_to_emulator() and android_booted()):
            if time.time() > end:
                raise InstallError("Android didn't start in time, so nothing was installed.")
            self.report("Waiting for Android to start…")
            time.sleep(1)
        pkgs = install_files(self.paths, self.report, downgrade, replace)
        info = {}
        for _ in range(8):  # the home screen needs a moment to learn the new app's name
            info = {a["package"]: a for a in list_apps() if a["package"] in pkgs}
            if all(p in info and info[p]["label"] != p for p in pkgs if p):
                break
            time.sleep(0.5)
        return [info.get(p, {"package": p, "label": p or "The app", "icon": None, "launchable": bool(p)})
                for p in pkgs]

    def finished(self, result):
        self.busy = False
        if not self.alive:
            return
        if isinstance(result, InstallError):
            self.failed(result)
        elif isinstance(result, Exception):
            self.failed(InstallError(f"Something went wrong: {result}"))
        else:
            self.succeeded(result)
        if self.hidden:
            self.top.deiconify()
        self.above = None  # attach again (the game window may have come up meanwhile)
        self.stay_above_game()
        self.top.lift()

    def succeeded(self, apps):
        self.packages = [a["package"] for a in apps]
        self.bar.delete("all")
        self.bar.create_rectangle(0, 0, 412, 6, fill=YELLOW, outline="")
        last = apps[-1] if apps else {"label": "The app", "package": None, "icon": None, "launchable": False}
        if last.get("icon"):
            img = app_icon_photo(last["icon"], 56)
            if img:
                self.icon_img = img
                self.icon.delete("all")
                self.icon.create_image(0, 0, anchor="nw", image=img)
        names = [a["label"] for a in apps]
        self.title.configure(text=f"{' and '.join(names) if len(names) <= 2 else f'{len(names)} apps'} "
                                  f"{'is' if len(names) == 1 else 'are'} installed")
        self.status.configure(text="You'll find it on Android's home screen and in Settings → Apps.")
        buttons = [("Done", self.close, {})]
        if last.get("launchable") and last.get("package"):
            buttons.insert(0, ("Open", lambda: self.open(last["package"]), {"primary": True}))
        self.set_buttons(buttons)
        notify(f"Installed {', '.join(names)}")

    def failed(self, err):
        self.bar.delete("all")
        self.bar.create_rectangle(0, 0, 412, 6, fill=DANGER, outline="")
        self.title.configure(text="Couldn't install " + ", ".join(Path(p).name for p in self.paths), fg=DANGER)
        self.status.configure(text=str(err))
        buttons = [("Close", self.close, {})]
        if err.code == "INSTALL_FAILED_UPDATE_INCOMPATIBLE":
            buttons.insert(0, ("Remove the old one and install", lambda: self.retry(replace=True), {"danger": True}))
        elif err.code == "INSTALL_FAILED_VERSION_DOWNGRADE":
            buttons.insert(0, ("Install this older version", lambda: self.retry(downgrade=True), {"primary": True}))
        self.set_buttons(buttons)
        notify("The app wasn't installed")

    def retry(self, downgrade=False, replace=False):
        self.title.configure(text="Installing " + ", ".join(Path(p).name for p in self.paths), fg=TEXT)
        self.status_text = "Getting ready…"
        self.start(downgrade, replace)

    # -- window

    def set_buttons(self, buttons):
        for w in self.buttons.winfo_children():
            w.destroy()
        for text, cmd, kw in buttons:
            styled_button(self.buttons, text, cmd, **kw).pack(side="right", padx=(8, 0))

    def animate(self):
        if not self.alive:
            return
        if self.busy:
            if self.status_text and self.status.cget("text") != self.status_text:
                self.status.configure(text=self.status_text)
            self.phase = (self.phase + 0.012) % 1.0
            b = self.bar
            b.delete("all")
            b.create_rectangle(0, 0, 412, 6, fill=LINE, outline="")
            x = -140 + self.phase * (412 + 140)
            b.create_rectangle(max(0, x), 0, min(412, x + 140), 6, fill=YELLOW, outline="")
        self.ticks += 1
        if self.ticks % 30 == 0:
            self.stay_above_game()
        self.top.after(16, self.animate)

    def stay_above_game(self):
        """Attach to the game window once it's showing (it can appear after this dialog,
        when installing starts Android), so the dialog stays in front of it."""
        if self.xwin is None:
            return
        try:
            from Xlib import Xatom
            main, _ = self.xwin.emulator()
            if main is None or main.id == self.above or self.xwin.geometry(main) is None:
                return
            self.wrapper.change_property(Xatom.WM_TRANSIENT_FOR, Xatom.WINDOW, 32, [main.id])
            self.xwin.d.flush()
            self.above = main.id
            self.top.lift()
        except Exception:
            pass

    def open(self, package):
        threading.Thread(target=open_app, args=(package,), daemon=True).start()
        show_yellowstacks()
        self.close()

    def hide(self):
        if self.busy:  # keeps going; comes back when it's done
            self.hidden = True
            self.top.withdraw()
            notify("Installing in the background…")
        else:
            self.close()

    def close(self):
        self.alive = False
        self.top.destroy()
        if self.on_close:
            self.on_close(self.packages)


# ---------------------------------------------------------------- settings window

class SettingsWindow:
    """Yellowstacks' settings: pages on the left, the chosen page on the right."""

    PAGES = [("performance", "Performance"), ("display", "Display"), ("controls", "Game controls"),
             ("apps", "Apps"), ("startup", "Startup"), ("android", "Android"), ("about", "About")]

    def __init__(self, master, app=None, page="performance"):
        """app: the running App, or None when opened without Android running."""
        import tkinter as tk
        self.tk, self.master, self.app = tk, master, app
        self.saved = load_settings()
        self.draft = copy.deepcopy(self.saved)
        self.profile_name = app.profile_name if app else "mlbb"
        self.prof = load_profile(self.profile_name)
        self.toggle_key = self.prof.get("toggle_key", "grave")
        self.capturing = False
        self.android = None         # True/False once known
        self.apps = None            # list once loaded
        self.keyboard = None        # Android's on-screen keyboard switch, once read
        self.keyboard_draft = None
        self.icons = {}
        self.page = None
        self.alive = True

        self.top = top = tk.Toplevel(master, class_="Yellowstacks")
        top.title(f"{APP} settings")
        top.configure(bg=BG)
        top.geometry("880x600")
        center_on_screen(top, up=0.4, size=(880, 600))
        top.minsize(780, 540)
        window_icon(top)
        nav = tk.Frame(top, bg=PANEL, width=210)
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)
        head = tk.Frame(nav, bg=PANEL)
        head.pack(fill="x", padx=20, pady=(22, 18))
        try:
            self.logo = logo_photo(30, PANEL)
            tk.Label(head, image=self.logo, bg=PANEL).pack(side="left")
        except Exception:
            pass
        tk.Label(head, text="Settings", bg=PANEL, fg=TEXT, font=("Sans", 14, "bold")).pack(side="left", padx=10)
        self.nav = {}
        for key, title in self.PAGES:
            item = tk.Frame(nav, bg=PANEL, cursor="hand2")
            item.pack(fill="x")
            mark = tk.Frame(item, bg=PANEL, width=4)
            mark.pack(side="left", fill="y")
            lab = tk.Label(item, text=title, bg=PANEL, fg=TEXT, font=("Sans", 10, "bold"), anchor="w",
                           padx=18, pady=10)
            lab.pack(side="left", fill="x", expand=True)
            for w in (item, lab):
                w.bind("<ButtonRelease-1>", lambda e, k=key: self.show(k))
                w.bind("<Enter>", lambda e, k=key: k != self.page and self.paint_nav(k, LINE))
                w.bind("<Leave>", lambda e: self.paint_nav())
            self.nav[key] = (item, mark, lab)
        tk.Label(nav, text=f"{APP} {VERSION}", bg=PANEL, fg=MUTED, font=("Sans", 8)).pack(side="bottom", pady=14)

        right = tk.Frame(top, bg=BG)
        right.pack(side="left", fill="both", expand=True)
        bottom = tk.Frame(right, bg=BG)
        bottom.pack(side="bottom", fill="x", padx=28, pady=16)
        self.save_button = styled_button(bottom, "Save", self.save, primary=True)
        self.save_button.pack(side="right")
        styled_button(bottom, "Cancel", self.close).pack(side="right", padx=(0, 8))
        self.note = tk.Label(bottom, text="", bg=BG, fg=MUTED, font=("Sans", 9), anchor="w", justify="left")
        self.note.pack(side="left", fill="x", expand=True)
        tk.Frame(right, bg=LINE, height=1).pack(side="bottom", fill="x")
        self.body = tk.Frame(right, bg=BG)
        self.body.pack(fill="both", expand=True, padx=32, pady=(26, 8))
        top.protocol("WM_DELETE_WINDOW", self.close)
        top.bind("<Escape>", lambda e: not self.capturing and self.close())
        top.bind("<KeyPress>", self.on_key)
        self.show(page)
        run_in_thread(top, self.read_android, self.android_read)

    # -- Android state (read in the background)

    def read_android(self):
        if not (running_emulator() and pin_adb_to_emulator() and android_booted()):
            return False, None, None
        return True, onscreen_keyboard(), list_apps()

    def android_read(self, result):
        if not self.alive:
            return
        if isinstance(result, Exception):
            result = (False, None, None)
        self.android, self.keyboard, self.apps = result
        if self.keyboard_draft is None:
            self.keyboard_draft = self.keyboard
        if self.page in ("apps", "startup", "android"):
            self.show(self.page)

    def reload_apps(self):
        self.apps = None
        if self.page == "apps":
            self.show("apps")
        run_in_thread(self.top, list_apps, self.apps_loaded)

    def apps_loaded(self, apps):
        if not self.alive:
            return
        self.apps = apps if isinstance(apps, list) else []
        if self.page in ("apps", "startup"):
            self.show(self.page)

    # -- layout helpers

    def paint_nav(self, hover=None, color=None):
        for key, (item, mark, lab) in self.nav.items():
            on = key == self.page
            bg = RAISED if on else (color if key == hover else PANEL)
            item.configure(bg=bg)
            lab.configure(bg=bg, fg=YELLOW if on else TEXT)
            mark.configure(bg=YELLOW if on else bg)

    def show(self, page):
        if self.capturing:
            return
        self.page = page
        self.paint_nav()
        for w in self.body.winfo_children():
            w.destroy()
        getattr(self, f"page_{page}")()
        self.update_note()

    def header(self, title, sub):
        tk = self.tk
        tk.Label(self.body, text=title, bg=BG, fg=TEXT, font=("Sans", 17, "bold")).pack(anchor="w")
        tk.Label(self.body, text=sub, bg=BG, fg=MUTED, font=("Sans", 9), wraplength=580,
                 justify="left").pack(anchor="w", pady=(3, 22))

    def block(self, title, desc=None, restart=False):
        """A titled setting; returns the frame its control goes in."""
        tk = self.tk
        box = tk.Frame(self.body, bg=BG)
        box.pack(fill="x", pady=(0, 20))
        head = tk.Frame(box, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text=title, bg=BG, fg=TEXT, font=("Sans", 10, "bold")).pack(side="left")
        if restart:
            self.badge(head)
        if desc:
            tk.Label(box, text=desc, bg=BG, fg=MUTED, font=("Sans", 9), wraplength=580, justify="left",
                     anchor="w").pack(fill="x", pady=(2, 0))
        inner = tk.Frame(box, bg=BG)
        inner.pack(fill="x", pady=(9, 0))
        return inner

    def badge(self, parent):
        self.tk.Label(parent, text="RESTARTS ANDROID", bg=RAISED, fg=YELLOW, font=("Sans", 7, "bold"),
                      padx=6, pady=1).pack(side="left", padx=10)

    def switch(self, title, desc, value, command, enabled=True, restart=False):
        tk = self.tk
        row = tk.Frame(self.body, bg=BG)
        row.pack(fill="x", pady=(0, 18))
        sw = Switch(row, value, command=command, enabled=enabled)
        sw.c.pack(side="right", anchor="n", padx=(20, 0))
        left = tk.Frame(row, bg=BG)
        left.pack(side="left", fill="x", expand=True)
        head = tk.Frame(left, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text=title, bg=BG, fg=TEXT if enabled else MUTED, font=("Sans", 10, "bold"),
                 anchor="w").pack(side="left")
        if restart:
            self.badge(head)
        if desc:
            tk.Label(left, text=desc, bg=BG, fg=MUTED, font=("Sans", 9), wraplength=500, justify="left",
                     anchor="w").pack(fill="x", pady=(2, 0))
        return sw

    def set(self, key, value, rebuild=False):
        self.draft[key] = value
        if rebuild:
            self.show(self.page)
        self.update_note()

    def changes(self):
        return ({k for k in self.draft if self.draft[k] != self.saved[k]}
                | ({"toggle_key"} if self.toggle_key != self.prof.get("toggle_key", "grave") else set())
                | ({"keyboard"} if self.keyboard_draft != self.keyboard else set()))

    def update_note(self):
        changed = self.changes()
        if changed & set(HARDWARE_SETTINGS):
            text = "Android restarts to use the new settings when you save."
        elif changed:
            text = "You have unsaved changes."
        else:
            text = ""
        self.note.configure(text=text, fg=YELLOW if changed else MUTED)

    # -- pages

    def page_performance(self):
        cores, ram = os.cpu_count() or 4, host_ram_mb()
        self.header("Performance", "How much of your PC the virtual phone may use, and how smoothly "
                                   "it runs. Leave some of your PC for Linux itself.")
        box = self.block("CPU cores", f"Busy games run smoother with more. Your PC has {cores}; "
                                      f"the emulator uses at most {MAX_CORES}.", restart=True)
        Choice(box, [(n, f"{n} cores", n <= cores) for n in (2, 4, 6)], self.draft["cpu_cores"],
               lambda v: self.set("cpu_cores", v)).frame.pack(anchor="w")
        box = self.block("Memory (RAM)", f"4 GB is plenty for Mobile Legends. Your PC has "
                                         f"{ram / 1024:.0f} GB.", restart=True)
        Choice(box, [(mb, f"{mb // 1024} GB", mb <= ram * 0.6) for mb in (2048, 3072, 4096, 6144, 8192)],
               self.draft["ram_mb"], lambda v: self.set("ram_mb", v)).frame.pack(anchor="w")
        lo, hi, step = FPS_RANGE
        box = self.block("Frame rate", "The most frames per second games can show. Set the game's own "
                                       "frame rate high too.", restart=True)
        self.fps_slider = Slider(box, lo, hi, step, self.draft["fps"], lambda v: self.set("fps", v),
                                 ticks=(30, 60, 90, 120), unit="FPS")
        self.fps_slider.frame.pack(anchor="w")
        self.vsync_switch = self.switch(
            "VSync", "Waits for your monitor before showing each frame. Off has the least delay.",
            self.draft["vsync"], lambda v: self.set("vsync", v), restart=True)

    def page_display(self):
        self.header("Display", "The virtual phone's screen. The window can still be any size; "
                               "this is how many pixels Android draws.")
        box = self.block("Resolution", "Higher is sharper, lower is faster. Your keyboard controls "
                                       "move with the screen by themselves.", restart=True)
        Choice(box, [(f"{w}x{h}", f"{w} × {h}", True) for w, h in RESOLUTIONS],
               "{}x{}".format(*self.draft["resolution"]),
               lambda v: self.set("resolution", [int(n) for n in v.split("x")], rebuild=True)).frame.pack(anchor="w")
        auto = AUTO_DPI.get(self.draft["resolution"][1], 400)
        box = self.block("Pixel density (DPI)", "How big text and buttons are inside Android. "
                                                "Higher makes them bigger.", restart=True)
        Choice(box, [(0, f"Auto ({auto})", True)] + [(d, str(d), True) for d in DPIS], self.draft["dpi"],
               lambda v: self.set("dpi", v)).frame.pack(anchor="w")
        self.switch("Remember the window's size and place", "Yellowstacks opens where you left it.",
                    self.draft["remember_window"], lambda v: self.set("remember_window", v))

    def page_controls(self):
        tk = self.tk
        self.header("Game controls", f"Keyboard controls for {GAME_NAME}. They only work while the "
                                     "game is in front, so other apps and typing aren't affected.")
        self.switch("Keyboard controls on at start", "Off: press the on/off key when you want them.",
                    self.draft["controls_on_start"], lambda v: self.set("controls_on_start", v))
        box = self.block("On/off key", "Turns the keyboard controls on and off while you play.")
        self.key_button = styled_button(box, f"  {key_label(self.toggle_key)}  ", self.capture_key)
        self.key_button.pack(side="left")
        self.key_hint = tk.Label(box, text="Click, then press the key you want.", bg=BG, fg=MUTED,
                                 font=("Sans", 9))
        self.key_hint.pack(side="left", padx=12)
        box = self.block("Layout", "Place the joystick and buttons over the game screen.")
        styled_button(box, "Edit controls…", self.edit_controls).pack(side="left")
        box = self.block("Also good to know")
        for line in ("Ctrl+V pastes text from your PC into Android.",
                     "Enter presses OK when you type in the game.",
                     "F11 switches full screen on and off."):
            tk.Label(box, text="•  " + line, bg=BG, fg=TEXT, font=("Sans", 9), anchor="w").pack(fill="x")

    def page_apps(self):
        tk = self.tk
        self.header("Apps", "Install apps from APK files, open them, or remove them.")
        row = tk.Frame(self.body, bg=BG)
        row.pack(fill="x", pady=(0, 16))
        b = styled_button(row, "Install an APK…", self.install, primary=True)
        b.pack(side="left")
        if not self.android:
            b.configure(state="disabled")
        tk.Label(row, text="Or double-click an .apk, .xapk or .apks file in your file manager.",
                 bg=BG, fg=MUTED, font=("Sans", 9)).pack(side="left", padx=14)
        if self.android is None:
            msg = "Looking at Android…"
        elif not self.android:
            msg = f"Start {APP} to see and install apps."
        elif self.apps is None:
            msg = "Reading your apps…"
        elif not self.apps:
            msg = "No apps installed yet."
        else:
            msg = None
        if msg:
            tk.Label(self.body, text=msg, bg=BG, fg=MUTED, font=("Sans", 10)).pack(anchor="w", pady=20)
            return
        tk.Label(self.body, text=f"Your apps ({len(self.apps)})", bg=BG, fg=TEXT,
                 font=("Sans", 10, "bold")).pack(anchor="w", pady=(0, 6))
        sc = Scrollable(self.body, height=300)
        sc.outer.pack(fill="both", expand=True)
        for a in self.apps:
            line = tk.Frame(sc.inner, bg=PANEL)
            line.pack(fill="x", pady=(0, 6))
            key = a["package"]
            if key not in self.icons:
                self.icons[key] = (app_icon_photo(a["icon"], 40) if a.get("icon") else None) or generic_app_icon(40)
            tk.Label(line, image=self.icons[key], bg=PANEL).pack(side="left", padx=12, pady=8)
            texts = tk.Frame(line, bg=PANEL)
            texts.pack(side="left", fill="x", expand=True)
            tk.Label(texts, text=a["label"], bg=PANEL, fg=TEXT, font=("Sans", 10, "bold"), anchor="w").pack(fill="x")
            tk.Label(texts, text=a["package"], bg=PANEL, fg=MUTED, font=("Sans", 8), anchor="w").pack(fill="x")
            styled_button(line, "Remove", lambda a=a: self.uninstall(a), small=True).pack(side="right", padx=(6, 12))
            if a.get("launchable"):
                styled_button(line, "Open", lambda a=a: self.open(a), small=True,
                              primary=True).pack(side="right")
        sc.bind_wheel()

    def page_startup(self):
        self.header("Startup", f"What happens when you start {APP}.")
        box = self.block("When Android has started, open",
                         "Nothing shows Android's home screen. (The mlbb command always opens "
                         f"{GAME_NAME}.)")
        options = [("", "Nothing — the home screen")]
        for a in self.apps or []:
            if a.get("launchable"):
                options.append((a["package"], a["label"]))
        cur = self.draft["start_app"]
        if cur and cur not in [o[0] for o in options]:
            options.append((cur, GAME_NAME if cur == GAME else cur))
        Dropdown(box, options, cur, lambda v: self.set("start_app", v)).mb.pack(anchor="w")
        if self.apps is None and self.android is not False:
            self.tk.Label(box, text="Reading your apps…", bg=BG, fg=MUTED,
                          font=("Sans", 9)).pack(anchor="w", pady=(6, 0))

    def page_android(self):
        tk = self.tk
        self.header("Android", "Android 13 (Google APIs, x86_64) with Intel's ARM translation, "
                               "so ARM-only games run too.")
        known = self.android and self.keyboard is not None
        self.switch("On-screen keyboard while typing",
                    "A phone-style keyboard pops up when you type, as games expect. Your PC keyboard "
                    "still types." + ("" if known else f"  (Start {APP} to change this.)"),
                    bool(self.keyboard_draft), self.set_keyboard, enabled=bool(known))
        self.switch("Show the emulator's own side bar", "Google's original toolbar, next to Yellowstacks' one.",
                    self.draft["show_emulator_bar"], lambda v: self.set("show_emulator_bar", v))
        box = self.block("For developers")
        serial = os.environ.get("ANDROID_SERIAL") if self.android else None
        text = (f"adb device: {serial}    (adb -s {serial} shell)" if serial else
                f"adb works while {APP} is running.")
        tk.Label(box, text=text, bg=BG, fg=TEXT, font=("Monospace", 9), anchor="w").pack(fill="x")
        tk.Label(box, text=f"Virtual phone: {AVD}   ·   Emulator log: {LOG_DIR / 'emulator.log'}",
                 bg=BG, fg=MUTED, font=("Sans", 8), anchor="w").pack(fill="x", pady=(4, 0))

    def page_about(self):
        tk = self.tk
        head = tk.Frame(self.body, bg=BG)
        head.pack(anchor="w", pady=(0, 18))
        try:
            self.about_logo = logo_photo(72, BG)
            tk.Label(head, image=self.about_logo, bg=BG).pack(side="left")
        except Exception:
            pass
        t = tk.Frame(head, bg=BG)
        t.pack(side="left", padx=16)
        tk.Label(t, text=APP, bg=BG, fg=TEXT, font=("Sans", 20, "bold")).pack(anchor="w")
        tk.Label(t, text=f"Version {VERSION}", bg=BG, fg=MUTED, font=("Sans", 9)).pack(anchor="w")
        tk.Label(self.body, text="Android games and apps on Linux, with a keyboard-controls layer, "
                                 "built on Google's Android emulator.",
                 bg=BG, fg=TEXT, font=("Sans", 10), wraplength=560, justify="left").pack(anchor="w", pady=(0, 18))
        for title, path in (("Settings", CONFIG_DIR), ("Screenshots", pictures_dir() / APP), ("Logs", LOG_DIR)):
            row = tk.Frame(self.body, bg=BG)
            row.pack(fill="x", pady=(0, 8))
            tk.Label(row, text=title, bg=BG, fg=TEXT, font=("Sans", 10, "bold"), width=12, anchor="w").pack(side="left")
            tk.Label(row, text=str(path), bg=BG, fg=MUTED, font=("Sans", 9), anchor="w").pack(side="left")
            styled_button(row, "Open", lambda p=path: self.open_folder(p), small=True).pack(side="right")

    # -- actions

    def capture_key(self):
        self.capturing = True
        self.key_button.configure(text="  …  ", bg=YELLOW, fg=BG)
        self.key_hint.configure(text="Press a key  (Esc to cancel)", fg=YELLOW)
        self.top.focus_force()

    def on_key(self, e):
        if not self.capturing:
            return
        name = e.keysym
        if name == "Escape":
            self.capturing = False
            self.show("controls")
            return
        name = name.lower() if len(name) == 1 else name
        taken = used_keys(self.prof)
        taken.pop(self.prof.get("toggle_key", "grave"), None)
        if not valid_key(name) or name in taken:
            why = f"is used by {taken[name]}" if name in taken else "can't be used"
            self.key_hint.configure(text=f"{key_label(name)} {why}. Try another.", fg=DANGER)
            return
        self.toggle_key = name
        self.capturing = False
        self.show("controls")

    def set_keyboard(self, v):
        self.keyboard_draft = v
        self.update_note()

    def edit_controls(self):
        if self.app is not None:
            self.app.open_editor()
        else:
            Editor(self.master, self.prof, Emulator(), lambda p: save_profile(self.profile_name, p))

    def install(self):
        def chosen(paths):
            if self.alive and isinstance(paths, list) and paths:
                InstallDialog(self.top, paths, on_close=lambda pkgs: self.alive and pkgs and self.reload_apps())
        run_in_thread(self.top, choose_app_files, chosen)

    def open(self, a):
        threading.Thread(target=open_app, args=(a["package"],), daemon=True).start()
        if self.app is not None:
            self.app.show_game()

    def uninstall(self, a):
        if not ask(self.top, f"Remove {a['label']}?", "The app and everything it saved on this phone are "
                                                      "deleted.", yes="Remove", danger=True):
            return

        def done(res):
            notify(f"Removed {a['label']}")
            if self.alive:
                self.reload_apps()
        run_in_thread(self.top, lambda: subprocess.run([ADB, "uninstall", a["package"]], capture_output=True,
                                                       timeout=120), done)

    def open_folder(self, path):
        Path(path).mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def save(self):
        if self.capturing:
            return
        changed = self.changes()
        save_settings(self.draft)
        if "toggle_key" in changed:
            prof = load_profile(self.profile_name)
            prof["toggle_key"] = self.toggle_key
            save_profile(self.profile_name, prof)  # a running app picks it up by itself
        if "keyboard" in changed and self.keyboard_draft is not None:
            threading.Thread(target=set_onscreen_keyboard, args=(self.keyboard_draft,), daemon=True).start()
        if self.app is not None:
            self.app.apply_settings(self.draft)
        restart = bool(changed & set(HARDWARE_SETTINGS))
        self.close()
        if not restart:
            return
        if self.app is not None and not self.app.shutting_down:
            if ask(self.master, "Restart Android now?",
                   "The new settings are used once Android starts again. Apps you have "
                   "open will close.", yes="Restart now", no="Later"):
                self.app.restart_emulator()
            else:
                notify("The new settings are used the next time Yellowstacks starts")
        else:
            notify("The new settings are used the next time Yellowstacks starts")

    def close(self):
        self.alive = False
        self.top.destroy()


# ---------------------------------------------------------------- start-up

class BootError(Exception):
    pass


class BootCancelled(Exception):
    pass


class Booter:
    """Starts the emulator (unless it's already running) and follows Android's
    start-up in a background thread. The loading screen reads progress(), status,
    error and done."""

    # each step ends when its milestone is reached; the text shows during the step
    STEPS = [("started", "Starting the engine"), ("adb", "Starting Android"),
             ("booted", "Loading Android"), ("ready", "Getting things ready")]
    # seconds each step took in a cold boot on this PC (2026-11-01); replaced by the
    # times of the last start, so the bar keeps a realistic pace
    DEFAULT_TIMES = {"started": 1.0, "adb": 8.0, "booted": 3.0, "ready": 6.0}
    TIMES_FILE = LOG_DIR / "boot-times.json"

    def __init__(self, settings, open_app=None):
        self.settings = settings
        self.open_app = open_app
        self.t0 = time.time()
        self.reached = {}       # milestone -> seconds after the start
        self.status = self.STEPS[0][1]
        self.error = None
        self.done = False
        self.cancelled = False
        self.proc = None
        self.qemu_pid = None
        try:
            saved = json.loads(self.TIMES_FILE.read_text())
            self.times = {k: max(0.5, float(saved.get(k, v))) for k, v in self.DEFAULT_TIMES.items()}
        except (OSError, ValueError, AttributeError, TypeError):
            self.times = dict(self.DEFAULT_TIMES)
        threading.Thread(target=self._run, daemon=True).start()

    def progress(self):
        """0..1 for the loading bar. Each step gets a share of the bar by how long it
        took last time; inside a step the bar slows as it nears the step's end, so it
        never runs ahead of Android, and it jumps on when a step finishes early."""
        total = sum(self.times.values())
        start, prev_t, now = 0.0, 0.0, time.time() - self.t0
        for name, _ in self.STEPS:
            share = self.times[name] / total
            if name in self.reached:
                start += share
                prev_t = self.reached[name]
                continue
            dt = max(0.0, now - prev_t)
            return start + share * 0.95 * (1 - math.exp(-2.2 * dt / self.times[name]))
        return 1.0

    def _run(self):
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            BOOTING_FILE.write_text(str(os.getpid()))
            self._boot()
            self.done = True
        except BootCancelled:
            pass
        except BootError as e:
            self.error = str(e)
        except Exception as e:  # anything unexpected still ends up on the loading screen
            self.error = f"Something went wrong: {e}"
        finally:
            try:
                BOOTING_FILE.unlink()
            except OSError:
                pass

    def _mark(self, name, next_status):
        self.reached[name] = time.time() - self.t0
        self.status = next_status
        print(f"boot: {name} after {self.reached[name]:.1f} s", flush=True)

    def _until(self, cond, limit, message):
        end = time.time() + limit
        while True:
            if self.cancelled:
                raise BootCancelled()
            result = cond()
            if result:
                return result
            if self.proc is not None and self.proc.poll() is not None:
                raise BootError("The emulator stopped while starting." + emulator_complaint())
            if self.qemu_pid and not pid_alive(self.qemu_pid):
                raise BootError("The emulator closed while Android was starting.")
            if time.time() > end:
                raise BootError(message)
            time.sleep(0.3)

    def _boot(self):
        reap_children()  # an emulator from before a restart must not linger as a zombie
        if running_emulator() is None:
            apply_avd_config(self.settings)
            self.proc = start_emulator(self.settings)
        self.qemu_pid, serial = self._until(running_emulator, 60, "The emulator didn't start.")
        os.environ["ANDROID_SERIAL"] = serial
        self._mark("started", "Starting Android")
        self._until(adb_ready, 180, "Android didn't start in 3 minutes.")
        self._mark("adb", "Loading Android")
        self._until(android_booted, 180, "Android didn't finish starting in 3 minutes.")
        self._mark("booted", "Getting things ready")
        prepare_android()
        app = self.open_app if self.open_app in installed_packages() else None
        if app:
            label = next((a["label"] for a in list_apps() if a["package"] == app), "the app")
            self.status = f"Opening {label}"
            open_app(app)
        # wait until the home screen (or the app) is in front, so the window shows it
        try:
            self._until(lambda: (foreground_package() == app) if app else foreground_package(), 30, "")
        except BootError:
            pass  # show it anyway
        self._mark("ready", "Ready")
        if self.proc is not None:  # a full cold start: remember how long each step took
            prev, times = 0.0, {}
            for name, _ in self.STEPS:
                times[name] = round(self.reached[name] - prev, 2)
                prev = self.reached[name]
            try:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                self.TIMES_FILE.write_text(json.dumps(times))
            except OSError:
                pass

    def stop(self):
        """Cancel, and stop the emulator (blocks until it's gone, at most ~30 s)."""
        self.cancelled = True
        found = running_emulator()
        pid = found[0] if found else (self.proc.pid if self.proc else None)
        if pid is None:
            return
        if "booted" in self.reached:  # Android is up: shut it down properly
            power_off()
            end = time.time() + 25
            while pid_alive(pid) and time.time() < end:
                time.sleep(0.3)
        if pid_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            end = time.time() + 10
            while pid_alive(pid) and time.time() < end:
                time.sleep(0.2)


def glow_background(w, h, cy):
    """Dark background with a soft warm glow centred `cy` of the way down. A little
    noise is mixed in so the faint gradient doesn't show bands."""
    from PIL import Image, ImageChops
    tall = int(h * 1.3)
    mask = Image.radial_gradient("L").resize((w, tall), Image.BICUBIC)  # 0 in the middle
    top = max(0, min(tall - h, int(tall / 2 - h * cy)))
    mask = mask.crop((0, top, w, top + h)).point(lambda v: int(255 * 0.6 * max(0.0, 1 - v / 200) ** 2))
    img = Image.composite(Image.new("RGB", (w, h), (0x3d, 0x32, 0x10)),
                          Image.new("RGB", (w, h), (0x14, 0x13, 0x17)), mask)
    noise = Image.effect_noise((w, h), 1.6).convert("RGB")
    return ImageChops.add(img, noise, 1.0, -128)


class BootScreen:
    """The loading screen. It takes the game window's place while Android starts;
    the emulator's own window stays hidden until Android is ready, then appears
    exactly where the loading screen is."""

    TIPS = [
        "Press ` to turn the keyboard controls on or off.",
        "F11 switches full screen on and off.",
        "Double-click an .apk file in your file manager to install it.",
        "Ctrl+V pastes text from your PC into Android.",
        "Pick CPU cores, memory and resolution in Settings.",
        "Settings → Startup can open a game as soon as Android is ready.",
        "The keyboard button on the side bar moves the game controls.",
        "Screenshots go to Pictures/Yellowstacks.",
    ]
    SPIN = 40    # frames per turn of the ring
    FIND_MS = 8  # how often to look for the emulator's window before it first appears

    def __init__(self, root, booter, xwin, on_ready, on_cancel, on_retry):
        import tkinter as tk
        self.tk, self.root, self.booter, self.xwin = tk, root, booter, xwin
        self.on_ready, self.on_cancel, self.on_retry = on_ready, on_cancel, on_retry
        self.shown = 0.0
        self.frame = 0
        self.tip = random.randrange(len(self.TIPS))
        self.tip_time = time.time()
        self.revealing = False
        self.failed = False
        self.stopping = False
        self.alive = True
        self.size = None
        self.layout_job = None
        self.bar_px = None
        self.spin_frames = {}
        self.main_seen = None
        self.hidden_main = None
        self.icon_argb = load_icon_argb()

        self.top = top = tk.Toplevel(root, bg=BG, class_="Yellowstacks")
        top.title(APP)
        window_icon(top)
        self.geo = self.start_geometry()
        x, y, w, h = self.geo
        top.geometry(f"{w}x{h}+{x}+{max(0, y - 30)}")
        top.minsize(480, 270)
        self.c = tk.Canvas(top, bg=BG, highlightthickness=0, bd=0, width=w, height=h)
        self.c.pack(fill="both", expand=True)
        self.c.bind("<Configure>", self.on_resize)
        top.protocol("WM_DELETE_WINDOW", self.ask_cancel)
        top.update_idletasks()
        self.wrapper = xwin.d.create_resource_object("window", top.winfo_id()).query_tree().parent
        self.layout(w, h)
        top.after(120, lambda: self.xwin.move_resize(self.wrapper, *self.geo))  # client area exactly there
        self.animate()
        self.watch_for_emulator()

    # -- where it goes

    def start_geometry(self):
        """Where the game window was last time (its inside), else 1280x720, centred."""
        if self.booter.settings.get("remember_window", True):
            try:
                x, y, w, h = json.loads(App.WINDOW_FILE.read_text())["geometry"]
                if w > h >= 270:
                    return x, y, w, h
            except (OSError, ValueError, KeyError, TypeError):
                pass
        px, py = self.root.winfo_pointerxy()
        ax, ay, aw, ah = self.xwin.workarea_for(px, py)
        w = min(1280, aw - 80)
        h = w * 9 // 16
        if h > ah - 100:
            h = ah - 100
            w = h * 16 // 9
        return ax + (aw - w) // 2, ay + (ah - h) // 2 + 14, w, h

    def client_geometry(self):
        return self.xwin.geometry(self.wrapper)

    # -- drawing

    def on_resize(self, e):
        if (e.width, e.height) != self.size:
            if self.layout_job:
                self.top.after_cancel(self.layout_job)
            self.layout_job = self.top.after(60, lambda: self.layout(e.width, e.height))

    def layout(self, w, h):
        from PIL import ImageTk
        self.layout_job = None
        self.size = (w, h)
        c = self.c
        c.delete("all")
        self.bg_img = ImageTk.PhotoImage(glow_background(w, h, 0.39))
        c.create_image(0, 0, anchor="nw", image=self.bg_img)

        cx, cy = w / 2, h * 0.39
        logo = int(max(64, min(150, h * 0.17)))
        self.ring_r = logo / 2 + max(14, logo * 0.17)
        self.spin_frames = {}
        p = Painter(int(2 * self.ring_r + 8), int(2 * self.ring_r + 8))
        p.ring(p.w / 2, p.h / 2, self.ring_r, LINE, width=3)
        self.track_img = p.photo()
        c.create_image(cx, cy, image=self.track_img)
        self.spin_item = c.create_image(cx, cy, image=self.spinner(self.frame))
        self.logo_img = logo_photo(logo)
        c.create_image(cx, cy, image=self.logo_img)
        big = h >= 560
        y = cy + self.ring_r + (40 if big else 30)
        c.create_text(cx, y, text=APP, fill=TEXT, font=("Sans", 24 if big else 18, "bold"))
        self.bar_w = int(min(440, w * 0.42))
        self.bar_x = cx - self.bar_w / 2
        y += 46 if big else 38
        self.status_item = c.create_text(self.bar_x, y, anchor="w", fill=MUTED, font=("Sans", 10),
                                         text=self.status_text())
        self.pct_item = c.create_text(self.bar_x + self.bar_w, y, anchor="e", fill=YELLOW,
                                      font=("Sans", 10, "bold"), text="")
        self.bar_y = y + 20
        self.bar_px = None
        self.bar_item = c.create_image(self.bar_x, self.bar_y, anchor="w")
        self.detail_y = self.bar_y + 34
        self.tip_y = h - 34
        self.draw_tip()
        self.draw_bar()
        if self.failed:
            self.show_failure()

    def spinner(self, i):
        """One frame of the turning arc around the logo (drawn the first time it's needed)."""
        i %= self.SPIN
        if i not in self.spin_frames:
            size = int(2 * self.ring_r + 8)
            p = Painter(size, size)
            p.arc(size / 2, size / 2, self.ring_r, 90 - i * 360 / self.SPIN, -95, YELLOW, width=3)
            self.spin_frames[i] = p.photo()
        return self.spin_frames[i]

    def status_text(self):
        return self.booter.status + ("…" if not self.booter.done else "")

    def draw_bar(self):
        filled = round(self.bar_w * min(1.0, self.shown))
        color = DANGER if self.failed else YELLOW
        if (filled, color) == self.bar_px:
            return
        self.bar_px = (filled, color)
        p = Painter(self.bar_w, 6)
        p.rrect(0, 0, self.bar_w, 6, 3, fill=LINE)
        if filled >= 6:
            p.rrect(0, 0, filled, 6, 3, fill=color)
        self.bar_img = p.photo()
        self.c.itemconfigure(self.bar_item, image=self.bar_img)

    def draw_tip(self):
        c = self.c
        c.delete("tip")
        w = self.size[0]
        text = c.create_text(0, self.tip_y, anchor="w", text=self.TIPS[self.tip], fill=MUTED,
                             font=("Sans", 9), tags=("tip",))
        label = c.create_text(0, self.tip_y, anchor="w", text="TIP", fill=YELLOW,
                              font=("Sans", 8, "bold"), tags=("tip",))
        lw = c.bbox(label)[2] - c.bbox(label)[0]
        tw = c.bbox(text)[2] - c.bbox(text)[0]
        x = (w - lw - 10 - tw) / 2
        c.coords(label, x, self.tip_y)
        c.coords(text, x + lw + 10, self.tip_y)

    # -- every frame

    def animate(self):
        if not self.alive:
            return
        b = self.booter
        if not self.failed and self.size:
            target = 1.0 if b.done else b.progress()
            self.shown += (target - self.shown) * (0.25 if b.done else 0.12)
            self.frame += 1
            self.c.itemconfigure(self.spin_item, image=self.spinner(self.frame))
            self.c.itemconfigure(self.status_item, text="Stopping…" if self.stopping else self.status_text())
            self.c.itemconfigure(self.pct_item, text=f"{int(self.shown * 100)} %")
            self.draw_bar()
            if time.time() - self.tip_time > 6:
                self.tip = (self.tip + 1) % len(self.TIPS)
                self.tip_time = time.time()
                self.draw_tip()
            if b.error and not self.stopping:
                self.fail()
            elif b.done and not self.revealing and self.shown > 0.985:
                self.reveal()
        if self.frame % 3 == 0 or self.failed:
            self.hide_emulator()
        self.top.after(33, self.animate)

    def active_window(self):
        prop = self.xwin.root.get_full_property(self.xwin.d.intern_atom("_NET_ACTIVE_WINDOW"),
                                                self.xwin.X.AnyPropertyType)
        return prop.value[0] if prop and len(prop.value) else 0

    def watch_for_emulator(self):
        """Look for the emulator's window often until it first turns up, then leave it to
        animate(). The window manager only lists a window once it has mapped it, so the
        window is always on screen for a moment before we can minimise it; at the
        animation's pace that moment was a tenth of a second, and it showed as a flash."""
        if not self.alive or self.hidden_main is not None:
            return
        self.hide_emulator()
        self.top.after(self.FIND_MS, self.watch_for_emulator)

    def hide_emulator(self):
        """Keep the emulator's window out of sight while Android starts: minimised and
        out of the taskbar. (It stays mapped and keeps drawing, as when you minimise it
        yourself; taking it off the screen completely made the emulator's graphics
        crash.) Google's side bar is hidden for good, as always."""
        main, bar = self.xwin.emulator()
        try:
            if main is not None and not self.revealing:
                self.main_seen = main
                if self.hidden_main != main.id or self.xwin.geometry(main) is not None:
                    stole = self.active_window() == main.id
                    self.xwin.brand(main, self.icon_argb)
                    self.xwin.skip_taskbar(main, True)
                    self.xwin.minimise(main)
                    self.hidden_main = main.id
                    if stole:  # it took the focus when it opened: give it back
                        self.top.after(60, lambda: self.xwin.activate(self.wrapper))
            if bar is not None and bar.get_attributes().map_state != self.xwin.X.IsUnmapped:
                bar.unmap()
                self.xwin.d.flush()
        except Exception:  # a window went away while we looked
            pass

    # -- the end of the boot

    def reveal(self):
        """Android is ready: put the game window where the loading screen is and show
        it. It opens under the loading screen (or over it), which then goes away."""
        self.revealing = True
        main = self.xwin.emulator()[0] or self.main_seen
        geo = self.client_geometry()
        if geo and self.booter.settings.get("remember_window", True):
            try:
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                App.WINDOW_FILE.write_text(json.dumps({"geometry": list(geo)}))
            except OSError:
                pass
        if main is not None:
            try:
                self.xwin.skip_taskbar(main, False)
                if geo:
                    main.configure(x=geo[0], y=geo[1], width=geo[2], height=geo[3])
                    self.xwin.move_resize(main, *geo)
                self.xwin.activate(main)  # out of minimised, in front
                self.xwin.d.flush()
            except Exception as e:
                print("couldn't show the emulator window:", e, flush=True)
            if geo:
                self.top.after(250, lambda: self.xwin.move_resize(main, *geo))
        self.top.after(900, self.finish)

    def finish(self):
        self.alive = False
        main = self.xwin.emulator()[0] or self.main_seen
        try:
            self.on_ready()   # side bar + controls take over from here
        finally:
            self.top.destroy()
            if main is not None:
                self.xwin.activate(main)

    def fail(self):
        self.failed = True
        print("boot failed:", self.booter.error, flush=True)
        self.show_failure()

    def show_failure(self):
        tk, c = self.tk, self.c
        c.itemconfigure(self.spin_item, state="hidden")
        c.itemconfigure(self.status_item, text="Android couldn't start", fill=DANGER)
        c.itemconfigure(self.pct_item, text="")
        self.bar_px = None
        self.draw_bar()
        c.delete("failure")
        c.create_text(self.size[0] / 2, self.detail_y, text=self.booter.error, fill=TEXT, font=("Sans", 10),
                      width=self.bar_w + 80, justify="center", tags=("failure",))
        row = tk.Frame(c, bg=BG)
        styled_button(row, "Try again", self.on_retry, primary=True).pack(side="left", padx=4)
        styled_button(row, "Show the log", lambda: subprocess.Popen(
            ["xdg-open", str(LOG_DIR / "emulator.log")], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)).pack(side="left", padx=4)
        styled_button(row, "Close", self.on_cancel).pack(side="left", padx=4)
        c.create_window(self.size[0] / 2, self.detail_y + 52, window=row, tags=("failure",))

    def ask_cancel(self):
        if self.failed or self.stopping:
            self.on_cancel()
        elif ask(self.top, "Stop starting Android?", f"{APP} will close.", yes="Stop", danger=True):
            self.stopping = True
            self.on_cancel()

    def lift(self):
        self.top.deiconify()
        self.top.lift()
        self.xwin.activate(self.wrapper)


# ---------------------------------------------------------------- commands

_seen_mtime = {}


def mark_profile_seen(name):
    try:
        _seen_mtime[name] = profile_path(name).stat().st_mtime
    except OSError:
        pass


REQUEST_FILE = LOG_DIR / "request"


def read_request():
    """What the launcher asked a running Yellowstacks to do: "show", "settings",
    "open <package>" (written before it sends SIGUSR2)."""
    try:
        text = REQUEST_FILE.read_text().strip()
        REQUEST_FILE.unlink()
    except OSError:
        return "show", None
    word, _, arg = text.partition(" ")
    return word or "show", arg.strip() or None


# ---------------------------------------------------------------- a clean Android

# Apps a virtual phone used for games has no use for. They are removed for the
# only user (`pm uninstall --user 0`), which never touches /system, so
# `customize --restore` can put every one of them back.
ANDROID_BLOAT = (
    "com.google.android.apps.maps", "com.google.android.apps.docs",
    "com.google.android.apps.photos", "com.google.android.apps.messaging",
    "com.google.android.gm", "com.google.android.calendar", "com.google.android.contacts",
    "com.google.android.deskclock", "com.google.android.dialer", "com.google.android.youtube",
    "com.google.android.apps.youtube.music", "com.google.android.marvin.talkback",
    "com.google.android.marvin.talkbackoverlay", "com.google.android.apps.wellbeing",
    "com.google.android.projection.gearhead", "com.google.android.apps.restore",
    "com.google.android.tts", "com.google.android.apps.wallpaper",
    "com.google.android.apps.wallpaper.nexus", "com.google.android.apps.customization.pixel",
    "com.google.android.feedback", "com.google.android.onetimeinitializer",
    "com.google.android.partnersetup", "com.google.android.printservice.recommendation",
    "com.google.android.tag", "com.google.android.markup", "com.google.android.setupwizard",
    "com.google.android.googlequicksearchbox", "com.google.android.cellbroadcastreceiver",
    "com.android.chrome", "com.android.egg", "com.android.traceur", "com.android.camera2",
    "com.android.cameraextensions", "com.android.stk", "com.android.bips",
    "com.android.printspooler", "com.android.musicfx", "com.android.emergency",
    "com.android.cellbroadcastreceiver", "com.android.bookmarkprovider",
    "com.android.providers.partnerbookmarks", "com.android.wallpaper.livepicker",
    "com.android.wallpapercropper", "com.breel.wallpapers18", "com.android.dreams.basic",
    "com.android.simappdialog", "com.android.managedprovisioning",
    "com.android.bluetoothmidiservice", "com.android.calllogbackup",
    "com.android.companiondevicemanager", "com.android.storagemanager",
    "com.android.wallpaperbackup",
)
# Android settings that make it feel like part of Yellowstacks instead of a phone:
# (table, key, value)
ANDROID_LOOK = (
    ("global", "window_animation_scale", "0.5"),      # snappier, still readable
    ("global", "transition_animation_scale", "0.5"),
    ("global", "animator_duration_scale", "0.5"),
    ("global", "heads_up_notifications_enabled", "0"),  # no pop-ups over a game
    ("system", "sound_effects_enabled", "0"),
    ("system", "haptic_feedback_enabled", "0"),
    ("system", "screen_brightness_mode", "0"),
    ("system", "screen_brightness", "255"),
    ("secure", "lockscreen.disabled", "1"),
)


def debloat_android(restore=False, log=print):
    """Remove (or put back) the apps the virtual phone doesn't need. Returns how many."""
    done = 0
    for pkg in dict.fromkeys(ANDROID_BLOAT):      # dict.fromkeys drops duplicates
        if restore:
            ok = "Success" in adb_shell(f"pm install-existing {pkg}", timeout=30)
        else:
            ok = "Success" in adb_shell(f"pm uninstall --user 0 {pkg}", timeout=30)
        done += bool(ok)
    log(f"{'restored' if restore else 'removed'} {done} apps")
    return done


def android_look(log=print):
    """Dark mode and the quality-of-life settings."""
    adb_shell("cmd uimode night yes")
    for table, key, value in ANDROID_LOOK:
        adb_shell(f"settings put {table} {key} {value}")
    log("dark mode and display settings applied")


def wallpaper_image(w, h):
    """The Yellowstacks wallpaper: the dark theme, a soft glow and the logo."""
    from PIL import Image, ImageDraw, ImageFilter
    bg = tuple(int(BG[i:i + 2], 16) for i in (1, 3, 5))
    yellow = tuple(int(YELLOW[i:i + 2], 16) for i in (1, 3, 5))
    img = Image.new("RGB", (w, h), bg)
    # a wide, soft glow behind the middle (drawn small, then scaled up: cheap and smooth)
    glow = Image.new("L", (w // 4, h // 4), 0)
    d = ImageDraw.Draw(glow)
    cx, cy = w // 8, int(h // 4 * 0.62)
    for i in range(70, 0, -1):
        r = i / 70
        d.ellipse([cx - w * 0.156 * r, cy - h * 0.176 * r, cx + w * 0.156 * r, cy + h * 0.176 * r],
                  fill=int(26 * (1 - r) ** 1.7))
    glow = glow.resize((w, h), Image.LANCZOS).filter(ImageFilter.GaussianBlur(h // 15))
    img = Image.composite(Image.new("RGB", (w, h), yellow), img, glow)
    # the logo, dimmed a little so app icons stay easy to read over it
    try:
        size = int(min(w, h) * 0.28)
        logo = Image.open(ICON).convert("RGBA").resize((size, size), Image.LANCZOS)
        logo.putalpha(logo.getchannel("A").point(lambda v: int(v * 0.82)))
        img.paste(logo, (w // 2 - size // 2, int(h * 0.46) - size // 2), logo)
    except OSError:
        pass
    return img


def set_wallpaper(log=print):
    """Write the wallpaper into Android. It shows after the next restart, because
    Android only reads these files when it starts."""
    size = adb_shell("wm size")
    try:
        w, h = (int(n) for n in size.split(":")[-1].strip().split("x"))
    except ValueError:
        w, h = 1920, 1080
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        wallpaper_image(w, h).save(f.name)
        subprocess.run([ADB, "push", f.name, "/data/local/tmp/ys_wall.png"],
                       capture_output=True, timeout=60)
    adb_shell("cp /data/local/tmp/ys_wall.png /data/system/users/0/wallpaper; "
              "cp /data/local/tmp/ys_wall.png /data/system/users/0/wallpaper_orig; "
              "chown system:system /data/system/users/0/wallpaper /data/system/users/0/wallpaper_orig; "
              "chmod 600 /data/system/users/0/wallpaper /data/system/users/0/wallpaper_orig; "
              "restorecon /data/system/users/0/wallpaper /data/system/users/0/wallpaper_orig; "
              "rm -f /data/local/tmp/ys_wall.png", timeout=30)
    log(f"wallpaper set ({w}x{h}); it shows after the next restart")


def customize_android(restore=False, log=print):
    """Strip the virtual phone back to what games need and make it look like
    Yellowstacks. Everything here can be undone with restore=True."""
    if not (running_emulator() and pin_adb_to_emulator()):
        raise BootError("Android isn't running.")
    subprocess.run([ADB, "root"], capture_output=True, timeout=30)
    subprocess.run([ADB, "wait-for-device"], capture_output=True, timeout=60)
    debloat_android(restore, log)
    if restore:
        remove_home_screen(log)
        log("the look was left as it is; change it in Settings -> Android")
        return
    android_look(log)
    install_home_screen(log)
    set_wallpaper(log)


def cmd_customize(args):
    customize_android(restore=args.restore)


def cmd_run(args):
    import tkinter as tk
    from Xlib import display
    settings = load_settings()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = tk.Tk(className="yellowstacks")
    root.withdraw()  # the root window becomes the side bar later
    theme_tk(root)
    state = {"app": None, "ctl": None, "boot": None, "booter": None, "settings": None}

    def start_app():
        prof = load_profile(args.profile)
        mark_profile_seen(args.profile)
        print("adb device:", pin_adb_to_emulator(), flush=True)
        emu = Emulator()
        try:
            emu.booted()
        except grpc.RpcError:
            raise SystemExit(f"Can't reach the emulator on {GRPC_ADDR}. Start it with -grpc {GRPC_PORT}.")
        ctl = Controls(prof, emu, tk_root=root)
        ctl.enabled = settings["controls_on_start"]
        app = App(root, ctl, emu, args.profile)
        ctl.refresh_focus()
        state.update(app=app, ctl=ctl, boot=None)
        print(f"{APP} running. ` toggles the controls, F11 full screen.", flush=True)
        root.after(TICK_MS, tick)
        root.after(1000, watch_profile)

    def tick():
        state["ctl"].tick()
        root.after(TICK_MS, tick)

    def watch_profile():
        # pick up changes saved by `setup`, the settings or by hand
        try:
            m = profile_path(args.profile).stat().st_mtime
            if m != _seen_mtime.get(args.profile):
                _seen_mtime[args.profile] = m
                state["ctl"].set_profile(load_profile(args.profile))
                print("profile reloaded", flush=True)
        except (OSError, ValueError, KeyError) as e:
            print("profile not reloaded:", e, flush=True)
        root.after(1000, watch_profile)

    def start_over():
        """Run Yellowstacks again from the top (a fresh loading screen)."""
        print("starting over", flush=True)
        reap_children()
        os.execv(sys.executable, [sys.executable, str(HERE / "yellowstacks.py"), "run",
                                  "--profile", args.profile])

    def stop_then(after):
        booter = state["booter"]
        run_in_thread(root, booter.stop if booter else (lambda: None), lambda _: after())

    def requested(*_):
        word, arg = read_request()
        app, boot = state["app"], state["boot"]
        if word == "open" and arg:
            if app is not None:
                threading.Thread(target=open_app, args=(arg,), daemon=True).start()
            elif state["booter"] is not None and not state["booter"].reached.get("booted"):
                state["booter"].open_app = arg
        if word == "settings":
            if app is not None:
                app.open_settings()
            elif state["settings"] is None or not state["settings"].alive:
                state["settings"] = SettingsWindow(root, None)
        elif app is not None:
            app.show_game()
        elif boot is not None and boot.alive:
            boot.lift()

    def shutdown_requested(*_):
        if state["app"] is not None:
            state["app"].shutdown()
        elif state["boot"] is not None:
            state["boot"].stopping = True
            stop_then(root.quit)
        else:
            root.quit()

    found = pin_adb_to_emulator()
    if found and android_booted():
        # Android is already up (Yellowstacks itself was restarted): no loading screen
        def ready():
            prepare_android()
            if args.open:
                open_app(args.open)
        threading.Thread(target=ready, daemon=True).start()
        start_app()
    else:
        booter = Booter(settings, open_app=args.open or settings["start_app"] or None)
        state["booter"] = booter
        state["boot"] = BootScreen(root, booter, XWindows(display.Display()), on_ready=start_app,
                                   on_cancel=lambda: stop_then(root.quit),
                                   on_retry=lambda: stop_then(start_over))

    # SIGTERM/SIGINT: just quit (the launcher's `stop` does the Android shutdown);
    # SIGUSR1: shut Android down properly, same as the close button;
    # SIGUSR2: the launcher asks for something (see read_request)
    signal.signal(signal.SIGTERM, lambda *_: root.quit())
    signal.signal(signal.SIGINT, lambda *_: root.quit())
    signal.signal(signal.SIGUSR1, lambda *_: root.after(0, shutdown_requested))
    signal.signal(signal.SIGUSR2, lambda *_: root.after(0, requested))
    try:
        root.mainloop()
    finally:
        app, ctl = state["app"], state["ctl"]
        if ctl is not None:
            ctl.shutdown()
        if app is not None and not app.shutting_down:
            # leave the emulator usable if we exit for another reason
            main, orig = ctl.xwin.emulator()
            for w in (main, orig):
                if w is not None and ctl.xwin.geometry(w) is None:
                    w.map()
            ctl.d.flush()
    if app is not None and app.restart_requested:
        start_over()


def cmd_install(args):
    """Install app files (from the file manager or the command line)."""
    import tkinter as tk
    root = tk.Tk(className="yellowstacks")
    root.withdraw()
    theme_tk(root)
    paths = [str(Path(p).resolve()) for p in args.files]
    if not paths:
        picked = choose_app_files()
        if not picked:
            return
        paths = picked
    pin_adb_to_emulator()
    InstallDialog(root, paths, on_close=lambda _: root.destroy(), wait_for_android=True)
    root.mainloop()


def cmd_settings(args):
    """The settings window on its own (when Yellowstacks isn't running)."""
    import tkinter as tk
    root = tk.Tk(className="yellowstacks")
    root.withdraw()
    theme_tk(root)
    win = SettingsWindow(root, None, args.page or "performance")
    win.top.bind("<Destroy>", lambda e: root.after(0, root.destroy) if e.widget is win.top else None)
    root.mainloop()


def cmd_open(args):
    """Open an app in the running Android."""
    if not pin_adb_to_emulator():
        raise SystemExit("Android isn't running.")
    open_app(args.files[0])


def cmd_setup(args):
    import tkinter as tk
    from Xlib import display
    prof = load_profile(args.profile)
    root = tk.Tk()
    root.withdraw()
    xw = XWindows(display.Display())
    main, _ = xw.emulator()
    geo = xw.geometry(main) if main is not None else None

    def saved(p):
        save_profile(args.profile, p)
        print(f"Saved to {profile_path(args.profile)}")

    ed = Editor(root, prof, Emulator(), saved, geo)
    ed.top.bind("<Destroy>", lambda e: root.after(0, root.destroy) if e.widget is ed.top else None)
    root.mainloop()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command",
                    choices=["run", "setup", "prepare", "install", "settings", "open", "customize"])
    ap.add_argument("files", nargs="*", help="install: app files; open: a package name")
    ap.add_argument("--profile", default="mlbb")
    ap.add_argument("--open", help="run: open this app (package name) once Android has started")
    ap.add_argument("--page", help="settings: the page to show first")
    ap.add_argument("--restore", action="store_true",
                    help="customize: put the removed Android apps back")
    args = ap.parse_args()
    {"run": cmd_run, "setup": cmd_setup, "install": cmd_install, "settings": cmd_settings, "open": cmd_open,
     "customize": cmd_customize,
     "prepare": lambda a: (pin_adb_to_emulator(), prepare_android())}[args.command](args)


if __name__ == "__main__":
    main()
