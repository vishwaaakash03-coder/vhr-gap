"""
VHR card builder - assemble the day's card from every source, then write it.

The workbook is printed at 04:30 and worked from all day, so it has to be
complete at 04:30. STBET is not: it had 219 races on the card at 04:30 and 302
by 08:15. betvirtual.co lists the whole day at once, and its prices agree with
STBET exactly - 218 races cross-checked, 218 identical winning odds.

So the card is built from the store, and the store is fed by both:

    betvirtual  the whole day, in one shot, at 04:30      (primary)
    STBET       whatever it has, topped up during the day (fills gaps, and
                supplies the event ids the results collector needs)

Neither source is trusted to be complete on its own; `upsert` only ever adds, so
whichever sees a race first wins and the other confirms it.
"""

import os
from datetime import datetime

import vhr_core as core
import vhr_data as data

# A track that has this many races is a full day's card, not a partial one.
FULL_CARD_MIN = 90


def merge_betvirtual(store, day, log=print):
    """Fetch betvirtual and merge its card and results into the store."""
    import vhr_betvirtual as bv

    n_card = n_res = 0
    collected = bv.collect_day(log=lambda *a: None)
    for track, v in collected.items():
        for rec in v["races"].values():
            fields = {}
            if rec.get("odds"):
                fields["odds"] = sorted(rec["odds"])
                fields["runners"] = len(rec["odds"])
                fields["odds_src"] = "betvirtual"
                n_card += 1
            if rec.get("winner_odds"):
                fields["winner_odds"] = rec["winner_odds"]
                fields["winner"] = rec.get("winner")
                n_res += 1
            if fields:
                data.upsert(store, track, rec["date"], rec["time"], **fields)
    log(f"  betvirtual: {n_card} carded, {n_res} results")
    return n_card, n_res


def state_from_store(store, day):
    """The store, shaped like a collector state file so the writer can use it.

    The workbook writer wants {tracks: {track: {key: {date, time, odds}}}}; the
    store is the same races keyed differently, filtered to this card's window.
    """
    start, end = core.card_window(day)
    tracks = {}
    for key, rec in store["races"].items():
        if not rec.get("odds"):
            continue
        when = datetime.combine(
            datetime.strptime(rec["date"], "%Y-%m-%d").date(),
            datetime.strptime(rec["time"], "%H:%M").time())
        if not (start <= when < end):
            continue
        tracks.setdefault(rec["track"], {})[key] = rec
    return {"tracks": tracks}


def card_counts(store, day):
    st = state_from_store(store, day)
    return {t: len(v) for t, v in st["tracks"].items()}


def is_full(counts):
    """Every main track showing a whole day's racing."""
    mains = [n for t, n in counts.items() if t in data.TRACK_ORDER]
    return len(mains) >= 3 and all(n >= FULL_CARD_MIN for n in mains)


def build_workbook(store, day, log=print):
    """Write the day's workbook from everything the store knows."""
    st = state_from_store(store, day)
    if not st["tracks"]:
        log("  nothing carded yet")
        return None, {}
    path, counts = core.build_workbook(day, st, core.workbook_path(day))
    if path:
        total = sum(counts.values())
        log(f"  {total} races ({', '.join(f'{t}: {c}' for t, c in counts.items())})")
        log(f"  saved {os.path.basename(path)}")
    return path, counts
