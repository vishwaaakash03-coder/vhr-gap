"""
VHR service - runs the long-lived parts in one process.

    card collector    waits for the 04:30 card swap, writes days/*.xlsx
    results collector records which odds won each race, every 2 minutes
    gap dashboard     serves http://localhost:8770
    publisher         pushes the page to GitHub Pages every 10 minutes

They are separate scripts and still run standalone, but Windows Task Scheduler
executes a task's actions one after another and waits for each to finish - so
four actions would only ever start the first. One process, four threads.

Each part is network-bound and spends nearly all its time asleep, so threads are
plenty. A thread that crashes is logged and restarted rather than silently lost.

    python vhr_service.py           run everything
    pythonw vhr_service.py          same, no console window (what the task uses)
"""

import threading
import time
from datetime import datetime

import vhr_core as core
import vhr_collector
import vhr_results
import vhr_dashboard
import vhr_publish

RESTART_DELAY = 30


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] service: {msg}"
    print(line, flush=True)
    try:
        with open(vhr_collector.LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def supervise(name, fn, argv):
    """Keep one part running. A crash costs a restart, not the whole service."""
    while True:
        try:
            fn(argv)
            log(f"{name} exited cleanly")
            return
        except Exception as e:
            log(f"{name} crashed: {type(e).__name__}: {e} - restarting in {RESTART_DELAY}s")
            time.sleep(RESTART_DELAY)


# STBET refuses every request from outside Sri Lanka, so the collecting cannot
# be moved to a cloud runner - only the finished page travels.
PARTS = [
    ("card collector",    vhr_collector.main, []),
    ("results collector", vhr_results.main,   []),
    ("gap dashboard",     vhr_dashboard.main, ["--no-open"]),
    ("publisher",         vhr_publish.main,   ["--loop"]),
]


def main():
    log("starting")
    threads = []
    for name, fn, argv in PARTS:
        t = threading.Thread(target=supervise, args=(name, fn, argv),
                             name=name, daemon=True)
        t.start()
        threads.append(t)
        time.sleep(0.4)          # stagger, so the log reads in order
    log(f"{len(threads)} parts running - dashboard on http://localhost:{vhr_dashboard.PORT}")

    try:
        while any(t.is_alive() for t in threads):
            time.sleep(5)
    except KeyboardInterrupt:
        log("stopped by user")


if __name__ == "__main__":
    main()
