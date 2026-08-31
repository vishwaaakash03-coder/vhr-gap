"""
VHR gap analysis - how long each odds price goes between wins, per track.

Two ways of measuring the same gap, because they answer different questions:

  RACES   - how many races ran between one win and the next.
            Simple, but a price that is only offered in 3 of 4 races looks
            slower than it is.
  CHANCES - how many races that actually *offered* that price ran between one
            win and the next. A race with no 25/1 runner was never a chance for
            25/1 to win, so it does not count.

Each measure runs over its own consistent universe: RACES over every race with
a known result, CHANCES over the races that also have a known card. Mixing them
would undercount, since a race can reach the store with a result but no card.

The signal is a percentile of that price's own history: if the current drought
is longer than 95% of its past droughts, it reads VERY HOT. That describes the
past. These are RNG races and each one is independent, so a long drought does
not make the next race more likely to end it.

Everything is computed in a single pass over each track, because the static
build recomputes the whole report from scratch on every run.
"""

import math
from collections import defaultdict

SIGNAL_BANDS = [
    (0.95, "VERY HOT"),
    (0.80, "HOT"),
    (0.50, "WARM"),
    (0.00, "COLD"),
]
MIN_GAPS = 3            # below this a percentile is noise, not a reading
HISTORY_LIMIT = 60      # gaps kept per price for the detail chart


def odds_value(o):
    """Fractional odds -> decimal, so columns sort by real price (this is what
    put 80/1 after 100/1 in the old sheet: unknown prices were just appended)."""
    if (o or "").lower() == "evs":
        return 1.0
    try:
        a, b = o.split("/")
        return int(a) / int(b)
    except Exception:
        return math.inf


def _percentile(gaps, current):
    """Share of past gaps this price has already outlasted."""
    if not gaps:
        return None
    return sum(1 for g in gaps if g <= current) / len(gaps)


def _signal(pct, n_gaps):
    if pct is None or n_gaps < MIN_GAPS:
        return "NEW", None
    for threshold, label in SIGNAL_BANDS:
        if pct >= threshold:
            return label, pct
    return "COLD", pct


def _summary(gaps):
    if not gaps:
        return {"n": 0, "avg": None, "median": None, "max": None, "min": None}
    s = sorted(gaps)
    mid = len(s) // 2
    median = s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2
    return {"n": len(gaps),
            "avg": round(sum(gaps) / len(gaps), 1),
            "median": round(median, 1),
            "max": max(gaps),
            "min": min(gaps)}


def _measure(win_positions, total):
    """Gaps between consecutive wins, plus the drought still running.

    A gap is the interval length: two wins back to back give 1. The first win
    has no preceding gap, so it starts no interval. The current gap is how many
    have passed since the last win.
    """
    gaps = [b - a for a, b in zip(win_positions, win_positions[1:])]
    current = total - 1 - win_positions[-1] if win_positions else total
    stats = _summary(gaps)
    signal, pct = _signal(_percentile(gaps, current), len(gaps))
    return gaps, {"total": total, "wins": len(win_positions), "current": current,
                  "pct": pct, "signal": signal, **stats}


def analyse_track(races, history_limit=HISTORY_LIMIT):
    """One track's records in running order -> per-price stats and gap history.

    Returns {"rows": [...], "history": {price: {mode: {gaps, current}}}, ...}.
    A single pass collects the win positions in both universes; the per-price
    numbers all fall out of those two lists.
    """
    seq_a = [r for r in races if r.get("winner_odds")]
    seq_b = [r for r in seq_a if r.get("odds")]

    # -- pass 1: races universe (every result counts) -------------------------
    wins_a = defaultdict(list)
    for i, r in enumerate(seq_a):
        wins_a[r["winner_odds"]].append((i, r))

    # -- pass 2: chances universe (only races that offered the price) ---------
    seen_b = defaultdict(int)
    wins_b = defaultdict(list)
    for r in seq_b:
        prices = set(r["odds"])
        prices.add(r["winner_odds"])       # a win always counts as a chance
        for p in prices:
            idx = seen_b[p]
            seen_b[p] += 1
            if r["winner_odds"] == p:
                wins_b[p].append((idx, r))

    rows, history = [], {}
    for price in set(wins_a) | set(seen_b):
        pos_a = [i for i, _ in wins_a.get(price, [])]
        pos_b = [i for i, _ in wins_b.get(price, [])]
        gaps_a, stat_a = _measure(pos_a, len(seq_a))
        gaps_b, stat_b = _measure(pos_b, seen_b.get(price, 0))

        last = wins_a[price][-1][1] if wins_a.get(price) else None
        rows.append({
            "odds": price,
            "value": odds_value(price),
            "races": stat_a,
            "chances": stat_b,
            "win_rate": round(100 * stat_b["wins"] / stat_b["total"], 1)
                        if stat_b["total"] else None,
            "last_win": {"date": last["date"], "time": last["time"]} if last else None,
        })
        history[price] = {
            "races":   _history(gaps_a, wins_a.get(price, []), stat_a["current"], history_limit),
            "chances": _history(gaps_b, wins_b.get(price, []), stat_b["current"], history_limit),
        }

    rows.sort(key=lambda r: r["value"])
    return {"rows": rows, "history": history,
            "n_races": len(seq_a), "n_carded": len(seq_b)}


def _history(gaps, wins, current, limit):
    """Each completed gap tagged with the race that ended it."""
    entries = [{"gap": g, "date": r["date"], "time": r["time"]}
               for g, (_, r) in zip(gaps, wins[1:])]
    return {"gaps": entries[-limit:], "current": current}


def next_race_signals(rows, card_race):
    """Cross the upcoming race's card against the table: what is hot in it."""
    if not card_race:
        return []
    by_price = {r["odds"]: r for r in rows}
    card = card_race.get("odds", [])
    out = []
    # One line per price, not per runner - two horses at 18/1 is still one price.
    for price in sorted(set(card), key=odds_value):
        row = by_price.get(price)
        if not row:
            continue
        out.append({
            "odds": price,
            "runners": card.count(price),
            "current": row["chances"]["current"],
            "avg": row["chances"]["avg"],
            "pct": row["chances"]["pct"],
            "signal": row["chances"]["signal"],
            "value": row["value"],
        })
    rank = {"VERY HOT": 0, "HOT": 1, "WARM": 2, "COLD": 3, "NEW": 4}
    out.sort(key=lambda r: (rank.get(r["signal"], 9), -(r["pct"] or 0)))
    return out
