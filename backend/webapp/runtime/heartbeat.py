"""Frontend heartbeat state shared by FastAPI runtime code."""
from __future__ import annotations

import threading
import time

heartbeat_ts = [0.0]
heartbeat_seen = [False]
heartbeat_paused = [False]


def watchdog() -> None:
    while True:
        time.sleep(3)
        if not heartbeat_seen[0] or heartbeat_paused[0]:
            continue
        if time.time() - heartbeat_ts[0] > 15:
            heartbeat_seen[0] = False


threading.Thread(target=watchdog, daemon=True).start()

