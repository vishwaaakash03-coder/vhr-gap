"""
VHR report - the single payload the dashboard renders, however it is served.

The local server hands this out at /data.json; the GitHub Actions build writes
the same structure to a file next to the page. One shape, so the page has no
idea whether it is talking to a live server or a static file, and there is no
second endpoint to keep in step.

The next race is read from the race store rather than the collector's state
files, so this works unchanged on a runner that has no state directory.
"""

from datetime import datetime, timedelta

import vhr_data as data
import vhr_stats as stats


def race_dt(rec):
    h, m = map(int, rec["time"].split(":"))
    return datetime(*map(int, rec["date"].split("-")), h, m)


def next_race_per_track(store, now=None):
    """The next not-yet-run carded race on each track."""
    now = now or datetime.now()
    horizon = now + timedelta(days=1)      # ignore anything beyond one card
    best = {}
    for rec in store["races"].values():
        if not rec.get("odds"):
            continue
        when = race_dt(rec)
        if not (now < when <= horizon):
            continue
        cur = best.get(rec["track"])
        if cur is None or when < race_dt(cur):
            best[rec["track"]] = rec
    return {
        track: {
            "time": rec["time"],
            "date": rec["date"],
            "odds": rec["odds"],
            "runners": len(rec["odds"]),
            "in_mins": round((race_dt(rec) - now).total_seconds() / 60),
        }
        for track, rec in best.items()
    }


def _last_result(races):
    for rec in reversed(races):
        if rec.get("winner_odds"):
            return {"time": rec["time"], "date": rec["date"],
                    "odds": rec["winner_odds"], "winner": rec.get("winner", "")}
    return None


def build_payload(store, now=None):
    now = now or datetime.now()
    by_track = data.races_by_track(store)
    upcoming = next_race_per_track(store, now)

    tracks = {}
    for track in data.TRACK_ORDER:
        races = by_track.get(track)
        if not races:
            continue
        analysis = stats.analyse_track(races)
        card = upcoming.get(track)
        tracks[track] = {
            "rows": analysis["rows"],
            "history": analysis["history"],
            "n_races": analysis["n_races"],
            "n_carded": analysis["n_carded"],
            "next": card,
            "next_signals": stats.next_race_signals(analysis["rows"], card),
            "last_result": _last_result(races),
        }

    return {
        "generated": now.strftime("%Y-%m-%d %H:%M:%S"),
        "coverage": data.coverage(store),
        "tracks": tracks,
        "order": [t for t in data.TRACK_ORDER if t in tracks],
    }
