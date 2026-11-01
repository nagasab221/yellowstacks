# Yellowstacks

An Android app player for Linux, built around Google's Android emulator — a
BlueStacks-style experience on X11, with a loading screen, its own side bar,
a settings window, an APK installer and keyboard controls for touch games.

Made for playing Mobile Legends: Bang Bang on Linux Mint, but it runs any
Android app.

![Yellowstacks](yellowstacks.png)

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/nagasab221/yellowstacks/main/install.sh | bash
```

## Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/nagasab221/yellowstacks/main/uninstall.sh | bash
```

Add `--purge` to also delete your settings and keyboard controls:

```bash
curl -fsSL https://raw.githubusercontent.com/nagasab221/yellowstacks/main/uninstall.sh | bash -s -- --purge
```

Uninstalling never touches your virtual phone, the apps inside it, or the
Android SDK.

## What you get

- **Loading screen** — a proper start-up screen with a progress bar that
  learns how long your PC actually takes, instead of a blank emulator window.
- **Its own side bar** — minimise, close, power, volume, rotate, back, home,
  recent apps, screenshot, window sizes and full screen.
- **Settings** — CPU cores, memory, frame rate (30–120 FPS), VSync,
  resolution, DPI, what opens at start, and Android options.
- **APK installer** — installs `.apk`, split APKs, `.xapk`, `.apks` and
  `.apkm` (OBB data included). Double-click an APK in your file manager.
- **Keyboard controls** — map keys to taps and a virtual joystick, with a
  visual editor. They only work while the game is in front, so typing and
  other apps are never affected.
- **A clean Android** — no status bar, no emulator toolbar, no boot animation.

## Commands

| Command | What it does |
| --- | --- |
| `yellowstacks` | Start it (loading screen, then Android's home screen) |
| `yellowstacks open <package>` | Start if needed, then open that app |
| `mlbb` | Shortcut for `yellowstacks open com.mobile.legends` |
| `yellowstacks install [files]` | Install apps; with no files, pick them |
| `yellowstacks settings` | Open the settings |
| `yellowstacks edit` | Open the keyboard-controls editor |
| `yellowstacks stop` | Shut Android down properly and close everything |
| `yellowstacks customize` | Strip Android back to what games need (see below) |

While playing: `` ` `` turns the controls on and off, `F11` is full screen,
`Ctrl+V` pastes your PC clipboard into Android, and `Enter` presses OK when a
game asks you to type.

## A clean Android

`yellowstacks customize` makes the virtual phone feel like part of Yellowstacks
instead of a stock phone:

- **Removes about 50 apps** it has no use for — Gmail, Maps, Photos, Chrome,
  YouTube, the clock, the camera, printing and the rest. They are removed for
  the single user, so `/system` is never touched and nothing can break.
- **Dark mode**, faster animations, no notification pop-ups over a game, no
  system sounds or vibration, no lock screen.
- **A Yellowstacks wallpaper** generated at your screen's resolution.

Everything is reversible:

```bash
yellowstacks customize --restore    # puts the removed apps back
```

The wallpaper appears after the next restart, because Android only reads it
when it starts. `setup-avd.sh` runs this for you on a freshly built phone.

## Requirements

- Linux with **X11** (tested on Linux Mint Cinnamon). Wayland will not work —
  the controls rely on X11 input.
- Python 3.10+
- A GPU with working OpenGL drivers
- The Android SDK and a virtual phone — see below

## Setting up the virtual phone

The installer sets up Yellowstacks itself, but not Android. That part needs
the Android SDK plus Intel's ARM translation, which cannot be downloaded
automatically.

1. Install the Android SDK (Android Studio, or the command line tools) so you
   have `~/Android/Sdk/emulator` and `~/Android/Sdk/platform-tools`.

2. Get Intel's libhoudini ARM translation — the `prebuilts` folder from
   [vendor_intel_proprietary_houdini](https://github.com/supremegamers/vendor_intel_proprietary_houdini)
   (Android 13, commit `2f8f088`). Games built only for ARM phones need it.

3. Build the virtual phone:

   ```bash
   ~/.local/share/yellowstacks/setup-avd.sh /path/to/houdini/prebuilts
   ```

   This creates an AVD called `MLBB2` from the Android 13 Google APIs x86_64
   image, installs the ARM translation into it, and applies the fixes that
   make games work.

4. Install your game, then start it:

   ```bash
   yellowstacks install mygame.apk
   yellowstacks
   ```

## Notes and limits

- **ARM games need step 2.** Without libhoudini, apps built only for ARM
  phones will not start.
- **Frame rate.** Android defaults to 60 FPS; the slider raises it to 120.
  The game's own graphics settings also need to be turned up, and your
  monitor has to be fast enough to show it.
- **Anti-cheat.** Some games refuse to run in any emulator. That is the
  game's decision, and Yellowstacks does not try to hide anything from it.
- **One virtual phone at a time.** Yellowstacks expects a single AVD, named
  by `YS_AVD` (default `MLBB2`).

## Development

```bash
git clone https://github.com/nagasab221/yellowstacks.git
cd yellowstacks
python3 -m venv .venv && .venv/bin/pip install grpcio protobuf pillow python-xlib
.venv/bin/python yellowstacks.py run
```

The UI tests run in a throwaway nested X server, so they never touch your
real desktop, mouse or keyboard:

```bash
tests/run-ui-test.sh
```
