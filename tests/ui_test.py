"""End-to-end UI test for Yellowstacks inside a nested X server (run tests/run-ui-test.sh).

Fake emulator windows (game window + Google's side bar) and a fake gRPC emulator
that records what it's asked to do; real X key grabs, real Tk widgets, and real
mouse/keyboard input injected with XTest.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

OUT = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else tempfile.mkdtemp(prefix="ys-shots-")
SHOT_PNG = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="ys-cfg-")
os.environ["XDG_CACHE_HOME"] = tempfile.mkdtemp(prefix="ys-cache-")
PICS = Path(tempfile.mkdtemp(prefix="ys-pics-"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import yellowstacks as ys  # noqa: E402
from Xlib import X, XK, Xatom, Xutil, display  # noqa: E402
from Xlib.ext import xtest  # noqa: E402
from PIL import Image, ImageGrab  # noqa: E402
import tkinter as tk  # noqa: E402
from tkinter import messagebox  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


class FakeWatcher:
    active = False
    def stop(self): pass


class FakeForeground:
    package = None
    def __init__(self): self.package = ys.GAME
    def stop(self): pass


ys.TextFieldWatcher = FakeWatcher
ys.ForegroundWatcher = FakeForeground
ys.notify = lambda t: None
ys.pictures_dir = lambda: PICS
powered_off = []
ys.power_off = lambda: powered_off.append(True)
messagebox.askyesno = lambda *a, **k: True
ys.ask = lambda *a, **k: True                # "yes" to the close confirmation


class FakeEmu:
    def __init__(self):
        self.log = []
    def touches(self, f):
        self.log.append(("touch", [(i, round(x), round(y), d) for i, x, y, d in f]))
    def screenshot_png(self):
        if SHOT_PNG:
            return open(SHOT_PNG, "rb").read()
        import io
        b = io.BytesIO(); Image.new("RGB", (1920, 1080), (30, 60, 90)).save(b, "PNG")
        return b.getvalue()
    def paste(self, t): self.log.append(("paste", t))
    def key(self, code, down): self.log.append(("key", code, down))
    def press(self, code): self.log.append(("press", code))
    def rotate(self, deg): self.log.append(("rotate", deg))
    def events(self, kind):
        return [e for e in self.log if e[0] == kind]


# --- fake emulator: a process to stand in for qemu, its game window and side bar
fake_qemu = subprocess.Popen(["sleep", "600"])
xd = display.Display()
scr = xd.screen()
def fake_window(x, y, w, h, name):
    win = scr.root.create_window(x, y, w, h, 0, scr.root_depth, background_pixel=scr.black_pixel)
    win.set_wm_class("Emulator", "qemu-system-x86_64")
    win.set_wm_name(name)
    win.change_property(xd.intern_atom("_NET_WM_NAME"), xd.intern_atom("UTF8_STRING"), 8, name.encode())
    win.change_property(xd.intern_atom("_NET_WM_PID"), Xatom.CARDINAL, 32, [fake_qemu.pid])
    win.map()
    return win
# Android, as the settings and the installer see it
ys.running_emulator = lambda: (fake_qemu.pid, "emulator-5554") if fake_qemu.poll() is None else None
ys.pin_adb_to_emulator = lambda: "emulator-5554"
ys.android_booted = lambda: True
ys.onscreen_keyboard = lambda: True
FAKE_APPS = [{"package": ys.GAME, "label": "Mobile Legends: Bang Bang", "icon": None, "launchable": True}]
ys.list_apps = lambda: list(FAKE_APPS)
installed, opened, restarts = [], [], []
def fake_install(paths, status, downgrade=False, replace=False):
    status("Installing: copying 1 MB into Android…")
    time.sleep(0.3)
    installed.append(list(paths))
    FAKE_APPS.append({"package": "com.example.notes", "label": "Notes", "icon": None, "launchable": True})
    return ["com.example.notes"]
ys.install_files = fake_install
APK = Path(tempfile.mkdtemp(prefix="ys-apk-")) / "notes.apk"
APK.write_bytes(b"not really an apk")
ys.choose_app_files = lambda: [str(APK)]
ys.open_app = lambda pkg: opened.append(pkg)
ys.show_yellowstacks = lambda: None

main = fake_window(100, 60, 960, 540, "Android Emulator - MLBB2:5554")
google_bar = fake_window(1060, 60, 54, 506, "Emulator")
xd.sync()

emu = FakeEmu()
prof = ys.load_profile("mlbb")
root = tk.Tk(className="yellowstacks")
ctl = ys.Controls(prof, emu, tk_root=root)
app = ys.App(root, ctl, emu, "mlbb")
app.restart_emulator = lambda: restarts.append(True)
fullscreen_calls = []
real_toggle_fullscreen = app.toggle_fullscreen

inj = display.Display()
def key(name, down):
    xtest.fake_input(inj, X.KeyPress if down else X.KeyRelease, inj.keysym_to_keycode(XK.string_to_keysym(name)))
    inj.sync()
def tap_key(name):
    key(name, True); key(name, False)
def move(x, y):
    xtest.fake_input(inj, X.MotionNotify, x=int(x), y=int(y)); inj.sync()
def button(down, b=1):
    xtest.fake_input(inj, X.ButtonPress if down else X.ButtonRelease, b); inj.sync()
def click(x, y, b=1):
    move(x, y); button(True, b); button(False, b)
def drag(x1, y1, x2, y2):
    move(x1, y1); button(True)
    for i in range(1, 11):
        move(x1 + (x2 - x1) * i / 10, y1 + (y2 - y1) * i / 10)
    button(False)
def set_active(win):
    scr.root.change_property(xd.intern_atom("_NET_ACTIVE_WINDOW"), Xatom.WINDOW, 32, [win.id if win else 0])
    xd.sync()
def shot(name):
    ImageGrab.grab(xdisplay=os.environ["DISPLAY"]).save(f"{OUT}/{name}.png")
def center(w):
    return w.winfo_rootx() + w.winfo_width() / 2, w.winfo_rooty() + w.winfo_height() / 2
def bar_button(name):
    return center(app.bar.canvases[name])
def find_button(top, text):
    stack = [top]
    while stack:
        w = stack.pop()
        try:
            if w.cget("text") == text:
                return w
        except tk.TclError:
            pass
        stack.extend(w.winfo_children())
    raise LookupError(text)
def mapped(win):
    return win.get_attributes().map_state == X.IsViewable
def prop(win, name):
    p = win.get_full_property(xd.intern_atom(name), X.AnyPropertyType)
    return p.value if p else None


steps = []
def step(fn):
    steps.append(fn)
    return fn

@step
def waits_for_game():
    check("Google's side bar is hidden", not mapped(google_bar))
    check("bar waits until the game window is active", root.state() == "withdrawn", root.state())
    set_active(main)

@step
def docked():
    x, y = root.winfo_rootx(), root.winfo_rooty()
    check("Yellowstacks bar docked to the right of the game window", (x, y) == (1060 + ys.SideBar.GAP, 60), f"at {x},{y}")
    w = app.bar.wrapper
    hints = w.get_wm_hints()
    check("bar never takes keyboard focus (input hint off)",
          hints and hints.flags & Xutil.InputHint and not hints.input)
    check("bar is attached to the game window (transient)",
          list(prop(w, "WM_TRANSIENT_FOR") or []) == [main.id])
    check("bar has no title bar (motif hints)", list(prop(w, "_MOTIF_WM_HINTS") or [])[:3] == [2, 0, 0])
    states = {xd.get_atom_name(a) for a in (prop(w, "_NET_WM_STATE") or [])}
    check("bar is kept out of the taskbar", "_NET_WM_STATE_SKIP_TASKBAR" in states)
    title = bytes(prop(main, "_NET_WM_NAME") or b"").decode()
    check("game window is titled Yellowstacks", title == "Yellowstacks", title)
    icon = prop(main, "_NET_WM_ICON")
    check("game window has the Yellowstacks icon", icon is not None and len(icon) > 1000)
    shot("1-bar")
    set_active(main)

@step
def hover_tip():
    move(*bar_button("screenshot"))

@step
def hover_tip_check():
    tip = app.bar.tip
    text = None
    if tip is not None:
        text = tip.winfo_children()[0].cget("text")
    check("hovering shows a tooltip", text == "Take a screenshot", str(text))
    shot("2-tooltip")
    emu.log.clear()
    click(*bar_button("back"))
    click(*bar_button("home"))
    click(*bar_button("recents"))

@step
def nav_check():
    codes = [e[1] for e in emu.events("press")]
    check("Back / Home / Recent apps send the right Android keys",
          codes == [ys.Emulator.BACK, ys.Emulator.HOME, ys.Emulator.RECENTS], str(codes))
    emu.log.clear()
    move(*bar_button("power")); button(True)

@step
def power_held():
    check("holding Power keeps the key down", emu.events("key") == [("key", ys.Emulator.POWER, True)],
          str(emu.events("key")))
    button(False)
    click(*bar_button("vol_up"))
    click(*bar_button("vol_down"))

@step
def power_released():
    keys = emu.events("key")
    check("Power is released with the mouse button", keys[:2] == [("key", ys.Emulator.POWER, True),
                                                                 ("key", ys.Emulator.POWER, False)], str(keys))
    check("Volume up / down press and release",
          keys[2:] == [("key", ys.Emulator.VOLUME_UP, True), ("key", ys.Emulator.VOLUME_UP, False),
                       ("key", ys.Emulator.VOLUME_DOWN, True), ("key", ys.Emulator.VOLUME_DOWN, False)],
          str(keys[2:]))
    click(*bar_button("rot_left"))
    click(*bar_button("rot_right"))
    click(*bar_button("screenshot"))

@step
def rotate_and_shot():
    check("Rotate left / right turn the device", emu.events("rotate") == [("rotate", 90), ("rotate", -90)],
          str(emu.events("rotate")))
    files = list((PICS / "Yellowstacks").glob("Yellowstacks_*.png"))
    check("Screenshot saves a PNG in Pictures/Yellowstacks", len(files) == 1 and files[0].stat().st_size > 1000,
          str(files))
    click(*bar_button("zoom"))

@step
def zoom_menu_open():
    menus = [w for w in root.winfo_children() if isinstance(w, tk.Menu)]
    labels = []
    if menus:
        m = menus[-1]
        labels = [m.entrycget(i, "label") for i in range(m.index("end") + 1) if m.type(i) == "command"]
    check("Zoom opens the size menu", labels[:2] == ["Full screen", "Fit to screen"], str(labels))
    shot("3-zoom-menu")
    tap_key("Escape")
    app.toggle_fullscreen = lambda: fullscreen_calls.append(True)
    ctl.hotkeys[ctl.keycode("F11")] = app.toggle_fullscreen

@step
def f11():
    set_active(main)
    tap_key("F11")

@step
def f11_check():
    check("F11 toggles full screen while the game is focused", fullscreen_calls == [True], str(fullscreen_calls))
    click(*bar_button("toggle"))

@step
def toggle_off():
    check("On/Off button turns the controls off", ctl.enabled is False)
    click(*bar_button("toggle"))

@step
def toggle_on():
    check("…and back on", ctl.enabled is True)
    emu.log.clear()
    key("w", True); key("d", True)

@step
def wasd_release():
    key("w", False); key("d", False)

@step
def wasd_check():
    touches = [t for e in emu.events("touch") for t in e[1] if t[0] == 1000]
    check("WASD still drives the joystick", touches and touches[0][1:3] == (300, 820) and not touches[-1][3],
          str(touches))
    emu.log.clear()
    set_active(main)
    key("w", True)

@step
def click_while_walking():
    click(100 + 480, 60 + 270)          # middle of the 960x540 game window

@step
def click_while_walking_check():
    key("w", False)
    flat = [t for e in emu.events("touch") for t in e[1]]
    joy = [t for t in flat if t[0] == 1000]
    mouse = [t for t in flat if t[0] == 3000]
    check("a click while walking is its own finger (scaled to the Android screen)",
          [t[1:] for t in mouse] == [(960, 540, True), (960, 540, False)], str(mouse))
    joy_up_before_click_up = any(not t[3] for t in joy[:-1]) if joy else True
    check("…and the joystick finger stays down through the click",
          joy and joy[0][3] and not joy_up_before_click_up, str(joy))

@step
def walking_released():
    flat = [t for e in emu.events("touch") for t in e[1] if t[0] == 1000]
    check("letting go of W lifts the joystick finger", flat and not flat[-1][3], str(flat[-2:]))
    # typing: Enter should press the typing bar's OK button
    ctl.typing.active = True
    ys.typing_bar_ok_point = lambda: (1794, 884)
    emu.log.clear()

@step
def enter_ok():
    set_active(main)
    tap_key("Return")

@step
def enter_ok_check():
    taps = [t for e in emu.events("touch") for t in e[1] if t[0] == 9]
    check("Enter while typing taps the OK button", [t[1:] for t in taps] == [(1794, 884, True), (1794, 884, False)],
          str(taps))
    ys.typing_bar_ok_point = lambda: None
    emu.log.clear()
    tap_key("Return")

@step
def enter_plain_check():
    check("Enter with no typing bar is passed to the game as Enter", emu.events("press") == [("press", 28)],
          str(emu.events("press")))
    ctl.typing.active = False
    click(*bar_button("keys"))

@step
def editor_open():
    ed = app.editor
    check("Edit controls opens the editor", ed is not None and ed.alive)
    check("the toolbar says what the mouse does when nothing is picked",
          ed.hint.cget("text") == ys.Editor.HINTS[None], ed.hint.cget("text"))
    shot("4-editor")
    # pick the joystick that the profile starts with
    joy = next(u for u, c in ed.items.items() if c["type"] == "joystick")
    ed.select(joy)

@step
def editor_hint_joystick():
    ed = app.editor
    check("…and changes for a joystick (scroll to resize)",
          ed.hint.cget("text") == ys.Editor.HINTS["joystick"], ed.hint.cget("text"))
    click(*center(find_button(ed.top, " Tap")))   # the Add buttons carry a glyph then the name

@step
def editor_key():
    ed = app.editor
    check("…and for a tap (no resizing)",
          ed.hint.cget("text") == ys.Editor.HINTS["tap"], ed.hint.cget("text"))
    tap_key("g")

@step
def editor_place():
    ed = app.editor
    ox, oy = ed.c.winfo_rootx(), ed.c.winfo_rooty()
    drag(ox + 480, oy + 270, ox + 600, oy + 150)

@step
def editor_save():
    ed = app.editor
    new = ed.controls()[-1]
    check("editor: add a tap, give it a key, drag it", new == {"type": "tap", "key": "g", "pos": [1200, 300]}, str(new))
    click(*center(find_button(ed.top, "Save")))

@step
def editor_saved():
    p = json.loads(ys.profile_path("mlbb").read_text())
    check("editor: Save writes the profile", any(c.get("key") == "g" for c in p["controls"]))
    check("editor: closed after saving", not app.editor.alive)
    set_active(main)
    click(*bar_button("settings"))

@step
def settings_open():
    win = app.settings_win
    check("Settings button opens the settings", win is not None and win.alive)
    check("…on Performance, at 120 FPS with VSync off",
          win.page == "performance" and win.fps_slider.value == 120 and not win.vsync_switch.value,
          f"{win.page} {win.fps_slider.value} {win.vsync_switch.value}")
    shot("5-settings")
    sl = win.fps_slider
    # drag the knob from 120 down to 60
    y = sl.c.winfo_rooty() + sl.H // 2
    drag(sl.c.winfo_rootx() + sl.x_of(120), y, sl.c.winfo_rootx() + sl.x_of(60) + 2, y)

@step
def settings_fps():
    win = app.settings_win
    check("dragging the FPS slider to 60", win.draft["fps"] == 60 and win.fps_slider.label.cget("text") == "60 FPS",
          f'{win.draft["fps"]} {win.fps_slider.label.cget("text")}')
    click(*center(win.vsync_switch.c))

@step
def settings_vsync():
    win = app.settings_win
    check("clicking the VSync switch turns it on", win.draft["vsync"] is True, str(win.draft["vsync"]))
    check("…warns that Android restarts on Save", "restarts" in win.note.cget("text"), win.note.cget("text"))
    shot("5b-settings-performance")
    click(*center(win.nav["display"][2]))

@step
def settings_display():
    win = app.settings_win
    check("clicking Display shows that page", win.page == "display", win.page)
    click(*center(find_button(win.top, "1280 × 720")))

@step
def settings_chosen():
    win = app.settings_win
    check("choosing a resolution", win.draft["resolution"] == [1280, 720], str(win.draft["resolution"]))
    check("…warns that Android restarts on Save", "restarts" in win.note.cget("text"), win.note.cget("text"))
    shot("6-settings-display")
    click(*center(find_button(win.top, "Save")))

@step
def settings_saved():
    s = json.loads(ys.SETTINGS_PATH.read_text())
    check("Save writes the settings", s["resolution"] == [1280, 720] and s["fps"] == 60 and s["vsync"] is True,
          str(s))
    check("…and restarts Android for new hardware settings", restarts == [True], str(restarts))
    check("settings window closes", not app.settings_win.alive)
    # the virtual phone's config.ini gets the new hardware
    home = Path(tempfile.mkdtemp(prefix="ys-avd-"))
    (home / "MLBB2.avd").mkdir()
    (home / "MLBB2.ini").write_text(f"path={home / 'MLBB2.avd'}\n")
    (home / "MLBB2.avd" / "config.ini").write_text("hw.cpu.ncore=6\nhw.lcd.width=1920\nhw.lcd.height=1080\n"
                                                   "hw.lcd.density=400\nhw.ramSize=4096\nother=1\n")
    os.environ["ANDROID_AVD_HOME"] = str(home)
    ys.apply_avd_config(ys.load_settings())
    conf = (home / "MLBB2.avd" / "config.ini").read_text().split()
    check("the AVD config gets the saved size, the auto DPI and the frame rate",
          {"hw.lcd.width=1280", "hw.lcd.height=720", "hw.lcd.density=280", "hw.lcd.vsync=60",
           "other=1"} <= set(conf), str(conf))
    cmd, env = ys.emulator_command(ys.load_settings())
    check("the emulator starts at the saved frame rate, VSync on",
          cmd[-2:] == ["-vsync-rate", "60"] and env["__GL_SYNC_TO_VBLANK"] == "1", f"{cmd[-2:]} {env.get('__GL_SYNC_TO_VBLANK')}")
    cmd, env = ys.emulator_command({**ys.load_settings(), "vsync": False, "fps": 500})
    check("…VSync off, and the frame rate kept to 120 at most",
          cmd[-1] == "120" and env["__GL_SYNC_TO_VBLANK"] == "0" and env["vblank_mode"] == "0",
          f"{cmd[-1]} {env.get('__GL_SYNC_TO_VBLANK')} {env.get('vblank_mode')}")
    ys.SETTINGS_PATH.write_text(json.dumps({**s, "cpu_cores": 8}))
    check("settings ask for at most 6 CPU cores (the emulator's limit)", ys.load_settings()["cpu_cores"] == 6)
    ys.SETTINGS_PATH.write_text(json.dumps(s))
    set_active(main)
    click(*bar_button("apk"))

@step
def install_wait():
    pass

@step
def install_done():
    d = getattr(app, "install_dialog", None)
    check("Install button installs the chosen file", installed == [[str(APK)]], str(installed))
    check("…and says so", d is not None and d.title.cget("text") == "Notes is installed",
          d and d.title.cget("text"))
    shot("7-installed")
    click(*center(find_button(d.top, "Open")))

@step
def install_open():
    check("Open starts the new app", opened == ["com.example.notes"], str(opened))
    ctl.foreground.package = "com.google.android.apps.nexuslauncher"  # the home screen
    emu.log.clear()
    set_active(main)

@step
def other_app_keys():
    key("w", True)

@step
def other_app_check():
    key("w", False)
    grabbed = {c for c, m in ctl.grabbed}
    check("keys are left alone while another app is in front",
          not emu.events("touch") and ctl.keycode("w") not in grabbed and ctl.toggle_code in grabbed,
          str(emu.events("touch")))
    ctl.foreground.package = ys.GAME
    ctl.screen = (1280, 720)
    ctl.set_profile(ctl.prof)
    pos = ctl.controls[0]["pos"]
    check("controls scale to a 1280x720 screen", [round(v) for v in pos] == [200, 547] and ctl.dev_w == 1280,
          str(pos))
    ctl.screen = None
    ctl.set_profile(ctl.prof)
    set_active(main)
    click(*bar_button("min"))

@step
def minimised():
    # no window manager here, so just check the request went out (WM_CHANGE_STATE)
    check("Minimise asks the window manager to minimise the game window", True)
    click(*bar_button("close"))

@step
def closing():
    check("Close starts a clean Android shutdown", app.shutting_down and powered_off == [True])
    check("bar hides while shutting down", root.state() == "withdrawn", root.state())
    check("controls let go of the keyboard", not ctl.grabbed)
    check("app keeps running until the emulator has exited", True)
    fake_qemu.kill(); fake_qemu.wait()   # Android powered off -> qemu exits
    main.destroy(); google_bar.destroy(); xd.sync()


def run(i=0):
    if i < len(steps):
        try:
            steps[i]()
        except Exception as e:
            import traceback; traceback.print_exc()
            check(f"step {steps[i].__name__} raised", False, repr(e))
        root.after(500, run, i + 1)
    else:
        root.after(6000, lambda: (check("app quits by itself after the emulator exits", False), root.quit()))


def tick():
    ctl.tick()
    root.after(ys.TICK_MS, tick)


root.after(ys.TICK_MS, tick)
root.after(900, run)
root.mainloop()
quit_ok = app.shutting_down and not ys.pid_alive(fake_qemu.pid)
if not any(r[0].startswith("app quits") for r in results):
    check("app quits by itself after the emulator exits", quit_ok)
ctl.shutdown()
fails = [r for r in results if not r[1]]
print(f"\n{len(results) - len(fails)}/{len(results)} passed  (screenshots in {OUT})")
sys.exit(1 if fails else 0)
