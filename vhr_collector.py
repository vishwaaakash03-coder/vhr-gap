"""
VHR collector - one Excel workbook per race day, built the morning the card drops.

    python vhr_collector.py            run forever (waits for each new card)
    python vhr_collector.py --status   show what the site is serving right now
    python vhr_collector.py --once     scrape whatever is on the card now and exit
    python vhr_collector.py --day 2026-08-31   work on a specific card date

STBET swaps the whole 24-hour card in one go, normally between 04:30 and 05:00
LK time - the finished races vanish and the next 24 hours appear together. It
does not add races during the day, so the card only has to be scraped once.

The catch is knowing when the swap has happened. Until it does, the site is
still serving the *finished* card: 70-odd races in total, but none of them left
to run. So readiness is measured in races that have not started yet - more than
CARD_READY_MIN of them on every track means the new card is up. (Counting total
races instead is what made the old scheduler scrape the dead card at 04:30:02
and come away with an incomplete day.)
"""

import os
import sys
import time
import traceback
from datetime import datetime, timedelta

import vhr_core as core

POLL_SECS      = 300     # 5 minutes, from 04:30 until the new card appears
CARD_READY_MIN = 20      # more not-yet-run races than this on every track = new card is up
LOG_PATH  = os.path.join(core.BASE_DIR, "vhr.log")
LOCK_PATH = os.path.join(core.BASE_DIR, ".vhr.lock")


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def describe(status):
    return ", ".join(f"{t} {v['upcoming']}/{v['total']}" for t, v in sorted(status.items()))


def card_is_ready(status):
    """The new card is up once every main track has a full day still to run."""
    return (len(status) >= 2
            and all(v["upcoming"] > CARD_READY_MIN for v in status.values()))


def build_day(day, state):
    """Scrape the whole card and write the workbook. Returns {track: n_races}."""
    n_new, n_filled = core.collect(day, state, log=log)
    path, counts = core.build_workbook(day, state, core.workbook_path(day))
    if not path:
        log("  nothing priced yet - will try again")
        return {}

    total = sum(counts.values())
    log(f"  {total} races ({', '.join(f'{t}: {c}' for t, c in counts.items())})")
    log(f"  saved {os.path.basename(path)}")

    # A short card means the swap was caught mid-flight; leave the day unmarked
    # so the next poll picks up the rest.
    if any(c < core.MIN_TRACK_EVENTS for c in counts.values()):
        log("  card looks short - not marking the day done yet")
        return {}

    state["built_at"] = datetime.now().isoformat(timespec="seconds")
    core.save_state(day, state)
    return counts


def next_rollover(now=None):
    now = now or datetime.now()
    start, end = core.card_window(core.card_day(now))
    return end


def acquire_lock():
    """One collector at a time - a stale lock from a killed run is reclaimed."""
    if os.path.exists(LOCK_PATH):
        try:
            age = time.time() - os.path.getmtime(LOCK_PATH)
            if age < 3 * POLL_SECS:
                log(f"Another collector looks alive (lock {int(age)}s old). Exiting.")
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


def nap(seconds):
    """Sleep in poll-sized pieces so the lock stays fresh and Ctrl+C still works."""
    end = time.time() + seconds
    while time.time() < end:
        touch_lock()
        time.sleep(min(POLL_SECS, max(1, end - time.time())))


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    day = core.card_day()
    if "--day" in args:
        day = datetime.strptime(args[args.index("--day") + 1], "%Y-%m-%d").date()

    if "--status" in args:
        status = core.card_status(day)
        print(f"Card {day} - not-yet-run / total races per track:")
        for t, v in sorted(status.items()):
            print(f"  {t:16s} {v['upcoming']:3d} / {v['total']:3d}")
        print("\n=> " + ("new card is up" if card_is_ready(status)
                         else f"old card still being served (need >{CARD_READY_MIN} to run)"))
        return

    if "--once" in args:
        log(f"Single run for card {day} ...")
        build_day(day, core.load_state(day))
        return

    if not acquire_lock():
        return

    log("VHR collector started.")
    log(f"  card day {day}, checking every {POLL_SECS // 60} min until the new card drops")
    log(f"  output: {core.workbook_path(day)}")

    try:
        while True:
            touch_lock()
            day = core.card_day()
            state = core.load_state(day)

            # Resuming after a restart: a workbook already holding a full card
            # counts as done, so the day is not scraped twice.
            if not state.get("built_at") and os.path.exists(core.workbook_path(day)):
                have = [len([r for r in v.values() if r["odds"]])
                        for v in state["tracks"].values()]
                if have and all(n >= core.MIN_TRACK_EVENTS for n in have):
                    state["built_at"] = datetime.now().isoformat(timespec="seconds")
                    core.save_state(day, state)
                    log(f"  {day} workbook already complete ({sum(have)} races)")

            if state.get("built_at"):
                wait = (next_rollover() - datetime.now()).total_seconds()
                log(f"  {day} already done at {state['built_at']} - "
                    f"next card in {wait / 3600:.1f} h")
                nap(max(60, wait))
                continue

            try:
                status = core.card_status(day)
                if not status:
                    log("  no VR meetings on the site right now")
                elif card_is_ready(status):
                    log(f"  new card is up ({describe(status)}) - scraping ...")
                    build_day(day, state)
                else:
                    log(f"  old card still being served ({describe(status)}) - waiting")
            except Exception as e:
                log(f"  check failed: {e}")
                if "--debug" in args:
                    traceback.print_exc()

            time.sleep(POLL_SECS)
    except KeyboardInterrupt:
        log("Stopped by user.")
    finally:
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass


if __name__ == "__main__":
    main()
