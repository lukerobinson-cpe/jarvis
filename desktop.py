"""Windows desktop extras: the HUD window, a push-to-talk hotkey and a tray icon."""
import ctypes
import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from ctypes import wintypes
from pathlib import Path

import ui_server

hotkey = threading.Event()  # set each time Ctrl+Alt+J is pressed, anywhere in Windows

MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
WM_HOTKEY, WM_CLOSE, SW_RESTORE = 0x0312, 0x0010, 9
HUD_TITLE = "J.A.R.V.I.S."  # the page title in ui/index.html, which the window takes
KEY_J = 0x4A
ICON = Path(__file__).parent / "ui" / "jarvis.png"  # drawn by make_icon.py


def _hud_windows() -> list[int]:
    """Every open Jarvis window, found by its title."""
    user32, found = ctypes.windll.user32, []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def each(window, _) -> bool:
        title = ctypes.create_unicode_buffer(64)
        user32.GetWindowTextW(window, title, 64)
        if user32.IsWindowVisible(window) and title.value.startswith(HUD_TITLE):
            found.append(window)
        return True

    user32.EnumWindows(each, 0)
    return found


def open_hud() -> None:
    """Show the HUD. A window left open from before is brought to the front
    (it reconnects by itself after a restart) and any extra ones are closed;
    only if there is none is a new one opened, as a standalone Edge app window
    or failing that in the default browser."""
    user32 = ctypes.windll.user32
    windows = _hud_windows()
    for extra in windows[1:]:
        user32.PostMessageW(extra, WM_CLOSE, 0, 0)
    if windows:
        if user32.IsIconic(windows[0]):  # minimised
            user32.ShowWindow(windows[0], SW_RESTORE)
        user32.SetForegroundWindow(windows[0])
        return
    candidates = [
        shutil.which("msedge"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for edge in candidates:
        if edge and Path(edge).exists():
            subprocess.Popen([edge, f"--app={ui_server.URL}", "--window-size=1280,800"])
            return
    webbrowser.open(ui_server.URL)


def restart() -> None:
    """Start Jarvis afresh and end this copy. The HUD window stays open and reconnects."""
    subprocess.Popen([sys.executable, str(Path(__file__).parent / "jarvis.pyw"), "--restart", "--no-browser"], cwd=Path(__file__).parent)
    os._exit(0)


def shut_down() -> None:
    """Close the HUD window and end Jarvis, until it is next opened from a shortcut."""
    for window in _hud_windows():
        ctypes.windll.user32.PostMessageW(window, WM_CLOSE, 0, 0)
    os._exit(0)


def start_hotkey() -> None:
    def listen() -> None:
        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, KEY_J):
            print("Ctrl+Alt+J is taken by another program, so the push-to-talk hotkey is off.")
            return
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            if message.message == WM_HOTKEY:
                hotkey.set()

    threading.Thread(target=listen, daemon=True).start()


def start_tray() -> None:
    """A tray icon to reopen the HUD, switch the microphone and quit. Skipped if pystray is missing."""
    try:
        import pystray
        from PIL import Image
    except ImportError:
        return
    image = Image.open(ICON)

    def quit_jarvis(icon) -> None:
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Open Jarvis", lambda: open_hud(), default=True),
        pystray.MenuItem(
            "Microphone on",
            lambda: ui_server.set_muted(not ui_server.muted.is_set()),
            checked=lambda item: not ui_server.muted.is_set(),
        ),
        pystray.MenuItem("Quit Jarvis", quit_jarvis),
    )
    pystray.Icon("jarvis", image, "Jarvis (Ctrl+Alt+J to talk)", menu).run_detached()
