"""
VHR collector - one Excel workbook per race day, complete by the time it is read.

    python vhr_collector.py            run forever
    python vhr_collector.py --status   what each source is serving right now
    python vhr_collector.py --once     one cycle, then exit
    python vhr_collector.py --day 2026-09-08   work on a specific card date

The workbook gets printed at 04:30 and worked from all day, so it has to be
complete at 04:30. STBET is not: 219 races on its card at 04:30, 302 by 08:15.
betvirtual.co publishes the whole day at once and agrees with STBET exactly -
218 races cross-checked, every winning price identical - so it is the primary
source and STBET fills in behind it.

STBET is still collected for two reasons: it carries the event ids the results
collector needs, and it is a second opinion on the card. Its own readiness rule
still applies - until the 04:30-05:00 swap the site serves the *finished* card,
which looks full but has nothing left to run, so readiness is counted in races
that have not started yet, not in total races.
"""

import os
import sys
import time
import traceback
from datetime import datetime

import vhr_cards as cards
import vhr_core as core
import vhr_data as data

POLL_SECS      = 300     # 5 minutes, while waiting for the day's card
TOPUP_SECS     = 900     # 15 minutes, once the card is complete
CARD_READY_MIN = 20      # not-yet-run races per track that mean STBET has swapped
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


def stbet_is_ready(status):
    """STBET has swapped to the new card once every track has a day still to run."""
    return (len(status) >= 2
            and all(v["upcoming"] > CARD_READY_MIN for v in status.values()))


def collect_stbet(day):
    """Merge STBET's card into its state file, for the event ids."""
    state = core.load_state(day)
    try:
        status = core.card_status(day)
    except Exception as e:
        log(f"  stbet check failed: {e}")
        return 0, None
    if not status:
        return 0, None
    if not stbet_is_ready(status) and not state.get("tracks"):
        return 0, status                      # still serving the finished card
    n_new, _ = core.collect(day, state, log=log)
    if n_new:
        core.save_state(day, state)
    return n_new, status


def cycle(day):
    """One pass over every source. Returns the per-track card counts."""
    store = data.load_store()
    before = cards.card_counts(store, day)

    try:
        cards.merge_betvirtual(store, day, log=log)
    except Exception as e:
        log(f"  betvirtual failed: {type(e).__name__}: {e}")

    n_stbet, status = collect_stbet(day)
    if status:
        log(f"  stbet: +{n_stbet} ({describe(status)})")

    data.import_state(store, days=[day])
    data.save_store(store)

    after = cards.card_counts(store, day)
    if after != before or not os.path.exists(core.workbook_path(day)):
        cards.build_workbook(store, day, log=log)
    return after


# -- lock ----------------------------------------------------------------------

def acquire_lock():
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
    """Sleep in short pieces so the lock stays fresh and Ctrl+C still works."""
    end = time.time() + seconds
    while time.time() < end:
        touch_lock()
        time.sleep(min(60, max(1, end - time.time())))


def next_rollover(now=None):
    _, end = core.card_window(core.card_day(now or datetime.now()))
    return end


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    day = core.card_day()
    if "--day" in args:
        day = datetime.strptime(args[args.index("--day") + 1], "%Y-%m-%d").date()

    if "--status" in args:
        store = data.load_store()
        counts = cards.card_counts(store, day)
        print(f"Card {day}")
        print(f"  in the store : {counts}   "
              f"{'complete' if cards.is_full(counts) else 'still filling'}")
        try:
            st = core.card_status(day)
            print(f"  on STBET     : {describe(st)}")
            print("                 " + ("swapped to the new card" if stbet_is_ready(st)
                                         else f"still the finished card (need >{CARD_READY_MIN} to run)"))
        except Exception as e:
            print(f"  on STBET     : check failed - {e}")
        return

    if "--once" in args:
        log(f"Single cycle for card {day} ...")
        cycle(day)
        return

    # Under vhr_service.py every part shares one process, and the service holds
    # the only lock that matters. Taking a second one here made a restart race
    # with its own previous instance: the lock was still warm, this part exited,
    # and nothing restarted it.
    if "--no-lock" not in args and not acquire_lock():
        return

    log("VHR collector started.")
    log(f"  card day {day}, betvirtual primary, STBET behind it")

    try:
        while True:
            touch_lock()
            day = core.card_day()
            full = False
            try:
                counts = cycle(day)
                full = cards.is_full(counts)
                total = sum(counts.values())
                log(f"  {day}: {total} races {counts}"
                    + ("  [complete]" if full else "  [still filling]"))
            except Exception as e:
                log(f"  cycle failed: {type(e).__name__}: {e}")
                if "--debug" in args:
                    traceback.print_exc()

            wait = (next_rollover() - datetime.now()).total_seconds()
            nap(min(TOPUP_SECS if full else POLL_SECS, max(60, wait)))
    except KeyboardInterrupt:
        log("Stopped by user.")
    finally:
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass


if __name__ == "__main__":
    main()
