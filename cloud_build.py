"""
VHR cloud build - one pass of the whole system, for a GitHub Actions runner.

    python cloud_build.py --store <path> --out <dir>

The PC is not always on, so the collecting has to happen somewhere that is.
This does in one shot what vhr_service.py does continuously:

    1. if the new 24-hour card is up, scrape it into the store
    2. fetch the result of every carded race that has run
    3. write the site - dashboard.html + data.json + the store itself

The store travels with the published site, so each run picks up where the last
one left off. There is no state directory: the card is merged straight into the
store, which is the only file that has to persist.

A run that finds nothing new still rewrites the site, because the countdown to
the next race and the current drought lengths move on their own.
"""

import json
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import vhr_core as core
import vhr_data as data
import vhr_report as report

CARD_READY_MIN = 20        # not-yet-run races per track that mean the new card is up
SETTLE_SECS    = 120       # do not ask for a result before the race can have finished
MAX_WORKERS    = 8


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# -- 1. the card ---------------------------------------------------------------

def collect_card(store, day):
    """Merge the day's card into the store, once the site has swapped to it."""
    status = core.card_status(day)
    if not status:
        log("no VR meetings on the site right now")
        return 0
    ready = len(status) >= 2 and all(v["upcoming"] > CARD_READY_MIN
                                     for v in status.values())
    shape = ", ".join(f"{t} {v['upcoming']}/{v['total']}" for t, v in sorted(status.items()))

    start, end = core.card_window(day)

    # The readiness gate stops a half-swapped card being taken as the new one.
    # On the very first run there is nothing to protect: an empty store should
    # take whatever the site is serving rather than sit blank until 04:30. Races
    # outside this card's window are dropped below either way, so a lingering
    # finished card cannot sneak in.
    have_today = any(start <= report.race_dt(r) < end
                     for r in store["races"].values() if r.get("odds"))
    if not ready and have_today:
        log(f"old card still being served ({shape}) - not scraping")
        return 0
    if not ready:
        log(f"store is empty - taking the card as served ({shape})")
    todo = []
    for track, mid in core.get_meetings().items():
        if track not in status:
            continue                                   # side meeting
        for ev in core.get_track_events(mid, day):
            when = report.race_dt(ev)
            if not (start <= when < end):
                continue
            key = data.race_key(track, ev["date"], ev["time"])
            if store["races"].get(key, {}).get("odds"):
                continue                               # already carded
            todo.append((track, ev))

    if not todo:
        log(f"card already collected ({shape})")
        return 0

    log(f"{'new card is up' if ready else 'bootstrapping'} ({shape}) "
        f"- fetching {len(todo)} races")
    added = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(core.fetch_event_odds, ev["id"]): (track, ev)
                   for track, ev in todo}
        for fu in as_completed(futures):
            track, ev = futures[fu]
            odds = [o for o in (fu.result() or []) if o and o != "N/A"]
            if not odds:
                continue
            data.upsert(store, track, ev["date"], ev["time"],
                        odds=sorted(set(odds)), runners=len(odds),
                        event_id=str(ev["id"]), odds_src="cloud")
            added += 1
    log(f"carded {added} races")
    return added


# -- 2. the results ------------------------------------------------------------

def collect_results(store, now=None):
    """Fetch the result of every carded race that has run and is still missing."""
    now = now or datetime.now()
    cutoff = now - timedelta(seconds=SETTLE_SECS)
    todo = [rec for rec in store["races"].values()
            if rec.get("event_id") and not rec.get("winner_odds")
            and report.race_dt(rec) <= cutoff]
    if not todo:
        return 0

    added = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(core.fetch_event_result, rec["event_id"]): rec
                   for rec in todo}
        for fu in as_completed(futures):
            rec = futures[fu]
            res = fu.result()
            if not res or not res.get("winner_odds"):
                continue                               # not settled - try next run
            data.upsert(store, rec["track"], rec["date"], rec["time"], **res)
            added += 1
    log(f"{added} results added ({len(todo) - added} still unsettled)")
    return added


# -- 3. the site ---------------------------------------------------------------

def write_site(store, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.join(out_dir, "store"), exist_ok=True)

    shutil.copy(os.path.join(core.BASE_DIR, "dashboard.html"),
                os.path.join(out_dir, "index.html"))

    payload = report.build_payload(store)
    with open(os.path.join(out_dir, "data.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))

    # The store rides along so the next run can carry on from it.
    with open(os.path.join(out_dir, "store", "races.json"), "w", encoding="utf-8") as f:
        json.dump(store, f, separators=(",", ":"))

    # Nothing here is a Jekyll site; without this GitHub Pages would skip files.
    open(os.path.join(out_dir, ".nojekyll"), "w").close()

    cov = payload["coverage"]
    size = os.path.getsize(os.path.join(out_dir, "data.json")) / 1024
    log(f"site written: {cov['with_winner']} results over {cov['days']} days, "
        f"data.json {size:.0f} KB")
    return payload


def main():
    args = sys.argv[1:]
    if "--store" in args:
        data.STORE = args[args.index("--store") + 1]
        data.DATA_DIR = os.path.dirname(data.STORE) or "."
    out_dir = args[args.index("--out") + 1] if "--out" in args else "site"

    store = data.load_store()
    log(f"store in: {len(store['races'])} races")

    day = core.card_day()
    try:
        collect_card(store, day)
    except Exception as e:
        log(f"card step failed: {type(e).__name__}: {e}")
    try:
        collect_results(store)
    except Exception as e:
        log(f"results step failed: {type(e).__name__}: {e}")

    data.save_store(store)
    write_site(store, out_dir)
    log("done")


if __name__ == "__main__":
    main()
