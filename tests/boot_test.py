"""Loading-screen test for Yellowstacks inside a nested X server (run tests/run-ui-test.sh).

A fake start-up (FakeBooter) and fake emulator windows that open while Android
"boots". Checks that the emulator's windows stay hidden behind the loading screen,
that the bar moves, and that the game window appears exactly where the loading
screen was once Android is ready. Then a start-up that fails.
"""
import json
import os
import subprocess
import sys
import tempfile

OUT = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else tempfile.mkdtemp(prefix="ys-shots-")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="ys-cfg-")
os.environ["XDG_CACHE_HOME"] = tempfile.mkdtemp(prefix="ys-cache-")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import yellowstacks as ys  # noqa: E402
from Xlib import X, Xatom, display  # noqa: E402
from Xlib.ext import xtest  # noqa: E402
from PIL import ImageGrab  # noqa: E402
import tkinter as tk  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def shot(name):
    ImageGrab.grab(xdisplay=os.environ["DISPLAY"]).save(f"{OUT}/{name}.png")


class FakeBooter:
    def __init__(self):
        self.settings = ys.load_settings()
        self.status = "Starting the engine"
        self.error = None
        self.done = False
        self.reached = {}
        self.p = 0.0
        self.stopped = False

    def progress(self):
        return self.p

    def stop(self):
        self.stopped = True


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
    xd.sync()
    return win


def mapped(win):
    return win.get_attributes().map_state == X.IsViewable


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


inj = display.Display()


def click(w):
    x, y = w.winfo_rootx() + w.winfo_width() // 2, w.winfo_rooty() + w.winfo_height() // 2
    xtest.fake_input(inj, X.MotionNotify, x=x, y=y)
    xtest.fake_input(inj, X.ButtonPress, 1)
    xtest.fake_input(inj, X.ButtonRelease, 1)
    inj.sync()


root = tk.Tk(className="yellowstacks")
root.withdraw()
booter = FakeBooter()
ready, cancelled, retried = [], [], []
screen = ys.BootScreen(root, booter, ys.XWindows(display.Display()), on_ready=lambda: ready.append(True),
                       on_cancel=lambda: cancelled.append(True), on_retry=lambda: retried.append(True))
wins = {}
steps = []


def step(fn):
    steps.append(fn)
    return fn


@step
def loading_shown():
    top = screen.top
    check("loading screen is shown", top.winfo_ismapped() and top.title() == "Yellowstacks", top.title())
    status = screen.c.itemcget(screen.status_item, "text")
    check("it says what's happening", status == "Starting the engine…", status)
    shot("b1-loading")
    # the emulator opens its windows while Android starts
    wins["main"] = fake_window(200, 200, 768, 432, "Android Emulator - MLBB2:5554")
    wins["bar"] = fake_window(970, 200, 54, 506, "Emulator")


@step
def emulator_hidden():
    # (no window manager here, so check the minimise request went out and it's rebranded)
    title = bytes(wins["main"].get_full_property(xd.intern_atom("_NET_WM_NAME"), 0).value).decode()
    check("the emulator's window is minimised while Android starts",
          screen.hidden_main == wins["main"].id and title == "Yellowstacks", title)
    check("…and Google's side bar is hidden", not mapped(wins["bar"]))
    booter.p = 0.55
    booter.status = "Loading Android"


@step
def halfway():
    pct = screen.c.itemcget(screen.pct_item, "text")
    check("the bar moves with the start-up", pct.endswith("%") and 30 <= int(pct.split()[0]) <= 60, pct)
    shot("b2-halfway")
    booter.done = True
    booter.status = "Ready"


@step
def wait1():
    pass


@step
def wait2():
    pass


@step
def revealed():
    check("when Android is ready, the side bar + controls take over", ready == [True], str(ready))
    check("the loading screen goes away", not screen.alive and not screen.top.winfo_exists())
    check("the game window appears", mapped(wins["main"]))
    check("Google's side bar stays hidden", not mapped(wins["bar"]))
    g = wins["main"].get_geometry()
    pos = wins["main"].translate_coords(scr.root, 0, 0)
    got = [-pos.x, -pos.y, g.width, g.height]
    saved = json.loads(ys.App.WINDOW_FILE.read_text())["geometry"]
    check("…exactly where the loading screen was", got == saved, f"{got} vs {saved}")
    # a start-up that fails
    global booter2, screen2
    booter2 = FakeBooter()
    screen2 = ys.BootScreen(root, booter2, ys.XWindows(display.Display()), on_ready=lambda: None,
                            on_cancel=lambda: cancelled.append(True), on_retry=lambda: retried.append(True))
    booter2.error = "Android didn't start in 3 minutes."


@step
def failed():
    status = screen2.c.itemcget(screen2.status_item, "text")
    check("a failed start says so", status == "Android couldn't start", status)
    try:
        buttons = [find_button(screen2.top, t) for t in ("Try again", "Show the log", "Close")]
    except LookupError as e:
        buttons = None
        check("…with Try again / Show the log / Close", False, str(e))
    else:
        check("…with Try again / Show the log / Close", True)
    shot("b3-failed")
    if buttons:
        click(buttons[0])


@step
def retry_clicked():
    check("Try again starts over", retried == [True], str(retried))
    click(find_button(screen2.top, "Close"))


@step
def close_clicked():
    check("Close gives up", cancelled == [True], str(cancelled))
    root.quit()


def run(i=0):
    if i < len(steps):
        try:
            steps[i]()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(f"step {steps[i].__name__} raised", False, repr(e))
        root.after(600, run, i + 1)


root.after(800, run)
root.after(30000, root.quit)
root.mainloop()
fake_qemu.kill()
fails = [r for r in results if not r[1]]
print(f"\n{len(results) - len(fails)}/{len(results)} passed  (screenshots in {OUT})")
sys.exit(1 if fails else 0)
