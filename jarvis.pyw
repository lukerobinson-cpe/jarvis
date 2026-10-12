"""Start Jarvis with no console window, or just show its window if it is already running.

This is what the taskbar, desktop and startup shortcuts run. Pass --no-browser
to start without opening the window (the startup shortcut does). Jarvis passes
--restart when it restarts itself after a change to its program.
"""
import shutil
import socket
import sys
import time
import traceback
from pathlib import Path

import desktop
import ui_server

ROOT = Path(__file__).parent


def running() -> bool:
    try:
        socket.create_connection(("127.0.0.1", ui_server.PORT), timeout=0.5).close()
        return True
    except OSError:
        return False


if "--restart" in sys.argv:  # wait for the copy that started us to finish closing
    for _ in range(40):
        if not running():
            break
        time.sleep(0.25)

if running():
    if "--no-browser" not in sys.argv:
        desktop.open_hud()
else:
    try:
        import listen  # loads the speech models, which takes several seconds
    except Exception:
        # The program is broken, most likely by a change made to it: note why,
        # put back the most recent backup and try once more.
        backups = sorted((ROOT / "backups").glob("*")) if (ROOT / "backups").is_dir() else []
        with open(ROOT / "jarvis.log", "a", encoding="utf-8") as log:
            log.write(f"\nFailed to start; restoring {backups[-1].name if backups else 'nothing (no backup)'}:\n{traceback.format_exc()}\n")
        if not backups:
            raise
        shutil.copytree(backups[-1], ROOT, dirs_exist_ok=True)
        import listen

    listen.main()
