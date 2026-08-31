"""
VHR results collector - records which odds actually won each race.

The card tells you what was on offer; only the result tells you what won, and
the gap analysis needs both. This walks the races the collector has already
carded, asks STBET for each finished race's result, and merges the winning odds
into the shared race store.

    python vhr_results.py           run forever (2-minute polls)
    python vhr_results.py --once    one pass, then exit

Runs alongside vhr_collector.py; both are started by START VHR.bat.
"""

import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import vhr_core as core
import vhr_data as data

POLL_SECS   = 120       # results settle within a minute or two of the off
SETTLE_SECS = 120       # do not ask before a race has had time to finish
LOG_PATH    = os.path.join(core.BASE_DIR, "vhr.log")
LOCK_PATH   = os.path.join(core.BASE_DIR, ".vhr-results.lock")


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] results: {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def pending_races(store):
    """Carded races that have run but whose result we have not stored yet.

    Yesterday's card is included too: a race just before the 04:30 rollover can
    settle after the card day has already turned over.
    """
    now = datetime.now()
    cutoff = now - timedelta(seconds=SETTLE_SECS)
    today = core.card_day(now)
    out = []
    for day in (today, today - timedelta(days=1)):
        state = core.load_state(day)
        for track, slot in state.get("tracks", {}).items():
            for event_id, rec in slot.items():
                if core.race_dt(rec) > cutoff:
                    continue                      # not run yet
                key = data.race_key(track, rec["date"], rec["time"])
                if store["races"].get(key, {}).get("winner_odds"):
                    continue                      # already have it
                out.append((track, rec, event_id))
    return out


def poll_once(store=None):
    """Fetch every outstanding result. Returns how many were added."""
    store = store or data.load_store()

    # Pull the current card into the store first. A race needs both halves -
    # the odds offered and the odds that won - and only the state files carry
    # the first. Restricted to the two live card days so this stays cheap.
    today = core.card_day()
    carded = data.import_state(store, days=[today, today - timedelta(days=1)])

    todo = pending_races(store)
    if not todo:
        if carded:
            data.save_store(store)
        return 0

    added = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(core.fetch_event_result, eid): (track, rec)
                   for track, rec, eid in todo}
        for fu in as_completed(futures):
            track, rec = futures[fu]
            res = fu.result()
            if not res or not res.get("winner_odds"):
                continue                          # not settled yet - try later
            data.upsert(store, track, rec["date"], rec["time"], **res)
            added += 1

    if added or carded:
        data.save_store(store)
    if added:
        log(f"+{added} results ({len(todo) - added} still unsettled)")
    return added


def acquire_lock():
    if os.path.exists(LOCK_PATH):
        try:
            if time.time() - os.path.getmtime(LOCK_PATH) < 3 * POLL_SECS:
                log("another results collector looks alive - exiting")
                return False
        except OSError:
            pass
    touch_lock()
    return True


def touch_lock():
    try:
        with open(LOCK_PATH, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv

    if "--once" in args:
        n = poll_once()
        log(f"single pass - {n} added")
        return

    if not acquire_lock():
        return

    log(f"collector started, polling every {POLL_SECS // 60} min")
    try:
        while True:
            touch_lock()
            try:
                poll_once()
            except Exception as e:
                log(f"poll failed: {e}")
                if "--debug" in args:
                    traceback.print_exc()
            time.sleep(POLL_SECS)
    except KeyboardInterrupt:
        log("stopped by user")
    finally:
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass


if __name__ == "__main__":
    main()
