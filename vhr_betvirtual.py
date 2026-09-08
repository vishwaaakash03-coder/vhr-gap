"""
VHR betvirtual source - the complete day's card, for all three tracks.

STBET publishes its card in pieces: 219 races on it at 04:30, 302 by 08:15. That
is fine for an analysis that keeps topping up, but useless for a sheet that gets
printed at 04:30 and worked from all day.

betvirtual.co lists the whole day at once. One page per track holds every
not-yet-run race with its full field and prices, and a second page holds every
finished race with the finishing order and the odds each horse went off at:

    /<track>-racecard    every race still to run, one <table> each
    /<track>-results     every race that has run, winner marked by rosette-1.png

Times on the site are UK. They are converted here with the real zone rules
rather than a fixed offset, so the sheet stays right through the October clock
change (UK 00:10 is LK 04:40 in summer, 05:40 in winter).

The catch is Cloudflare: plain requests, browser-impersonating TLS, and headless
Chrome are all refused. Only a real browser window gets through, so the fetch
runs Chrome with the window parked far off-screen - invisible, but real. It is
needed a handful of times a day, not continuously.
"""

import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")
LK = ZoneInfo("Asia/Colombo")

BASE = "https://www.betvirtual.co"
TRACKS = {                       # our name -> the site's slug
    "Portman Park":  "portman-park",
    "Sprint Valley": "sprint-valley",
    "Steepledowns":  "steepledowns",
}

# A visible window that nobody can see. Cloudflare refuses headless Chrome.
CHROME_ARGS = [
    "--window-position=-32000,-32000",
    "--window-size=1280,900",
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--mute-audio",
]
SETTLE_MS = 4000                 # let the challenge clear and the page render


# -- time ----------------------------------------------------------------------

def uk_to_lk(card_day, hhmm):
    """'0410' on a UK date -> (LK ISO date, 'HH:MM').

    The card day is the UK calendar day, which is exactly the 04:30 -> 04:30 LK
    window the rest of the system already uses.
    """
    hh, mm = int(hhmm[:2]), int(hhmm[2:])
    lk = datetime(card_day.year, card_day.month, card_day.day, hh, mm,
                  tzinfo=UK).astimezone(LK)
    return lk.date().isoformat(), lk.strftime("%H:%M")


# -- fetch ---------------------------------------------------------------------

def fetch_pages(paths, log=print):
    """{path: html} for several pages in one browser session."""
    from playwright.sync_api import sync_playwright

    out = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=False,
                                    args=CHROME_ARGS)
        try:
            page = browser.new_page()
            for path in paths:
                try:
                    page.goto(BASE + path, timeout=60000,
                              wait_until="domcontentloaded")
                    page.wait_for_timeout(SETTLE_MS)
                    html = page.content()
                    if "One moment" in html:
                        log(f"  {path}: still behind the challenge")
                        continue
                    out[path] = html
                except Exception as e:
                    log(f"  {path}: {type(e).__name__}: {e}")
        finally:
            browser.close()
    return out


# -- parse ---------------------------------------------------------------------

_ODDS = re.compile(r'<td class="odds[^"]*">\s*([0-9]+/[0-9]+|Evs|EVS)\s*<', re.I)
_ROW = re.compile(r'<tr>(.*?)</tr>', re.S)
_ANCHOR = re.compile(r'id="(\d{4})"')
# Every results block carries its own stamp: "04:31 • R30 • 07/09/2026". Reading
# the date beats inferring it - the results page shows one whole finished day,
# which is yesterday's for most of the morning.
_STAMP = re.compile(
    r'(\d{2}):(\d{2})\s*(?:&nbsp;|\s)*•(?:&nbsp;|\s)*R(\d+)'
    r'\s*(?:&nbsp;|\s)*•(?:&nbsp;|\s)*(\d{2})/(\d{2})/(\d{4})')


def _race_blocks(html):
    """[(hhmm, block_html)] - the page splits cleanly on the race anchors."""
    marks = [(m.group(1), m.start()) for m in _ANCHOR.finditer(html)]
    blocks = []
    for i, (hhmm, pos) in enumerate(marks):
        end = marks[i + 1][1] if i + 1 < len(marks) else len(html)
        blocks.append((hhmm, html[pos:end]))
    return blocks


def parse_card(html):
    """{hhmm: [odds, ...]} for every race still to run."""
    out = {}
    for hhmm, block in _race_blocks(html):
        odds = [o.strip().replace("EVS", "Evs") for o in _ODDS.findall(block)]
        if odds:
            out[hhmm] = odds
    return out


def parse_results(html):
    """{(uk_date, hhmm): {'winner_odds', 'winner', 'race_no', 'placed'}}.

    Finishing position is carried by the rosette image, not by any text, and the
    UK date comes from the block's own stamp rather than from the clock.
    """
    out = {}
    for hhmm, block in _race_blocks(html):
        st = _STAMP.search(block)
        if not st:
            continue
        uk_date = date(int(st.group(6)), int(st.group(5)), int(st.group(4)))
        race_no = int(st.group(3))
        placed = []
        for row in _ROW.findall(block):
            pos = re.search(r"rosette-(\d+)\.png", row)
            name = re.search(r"<a href=\"runner/[^\"]*\">\s*([^<(]+)", row)
            odd = re.search(r'<td class="odds[^"]*">\s*([0-9]+/[0-9]+|Evs|EVS)\s*<', row, re.I)
            if pos and odd:
                placed.append((int(pos.group(1)),
                               (name.group(1).strip() if name else ""),
                               odd.group(1).strip().replace("EVS", "Evs")))
        if not placed:
            continue
        placed.sort()
        win = next((p for p in placed if p[0] == 1), None)
        if not win:
            continue
        out[(uk_date, hhmm)] = {"winner_odds": win[2], "winner": win[1],
                                "race_no": race_no, "placed": placed}
    return out


# -- one pass ------------------------------------------------------------------

def collect_day(card_day=None, want_results=True, log=print):
    """The whole day for all three tracks.

    Returns {track: {"races": {lk_key: {...}}, "n_card": n, "n_results": n}},
    where lk_key is (iso_date, 'HH:MM') in Sri Lanka time.
    """
    card_day = card_day or datetime.now(UK).date()
    paths = []
    for slug in TRACKS.values():
        paths.append(f"/{slug}-racecard")
        if want_results:
            paths.append(f"/{slug}-results")

    pages = fetch_pages(paths, log=log)

    out = {}
    for track, slug in TRACKS.items():
        races = {}
        card = parse_card(pages.get(f"/{slug}-racecard", ""))
        for hhmm, odds in card.items():
            d, t = uk_to_lk(card_day, hhmm)
            races[(d, t)] = {"date": d, "time": t, "uk": hhmm, "odds": odds}

        # The results page shows one whole finished UK day, which for most of the
        # morning is yesterday's. Each block says which, so nothing is guessed.
        res = parse_results(pages.get(f"/{slug}-results", "")) if want_results else {}
        for (uk_date, hhmm), r in res.items():
            d, t = uk_to_lk(uk_date, hhmm)
            rec = races.setdefault((d, t), {"date": d, "time": t, "uk": hhmm})
            rec["winner_odds"] = r["winner_odds"]
            rec["winner"] = r["winner"]

        days = sorted({d for d, _ in res}) if res else []
        out[track] = {"races": races, "n_card": len(card), "n_results": len(res),
                      "result_days": days}
        span = f" for {days[0]}" + (f"..{days[-1]}" if len(days) > 1 else "") if days else ""
        log(f"  {track:15s} card {len(card):3d}  results {len(res):3d}{span}  "
            f"total {len(races):3d}")
    return out


if __name__ == "__main__":
    print("Fetching betvirtual for all three tracks ...")
    day = datetime.now(UK).date()
    data = collect_day(day)
    total = sum(len(v["races"]) for v in data.values())
    print(f"\nUK card day {day}   {total} races")
    for track, v in data.items():
        rs = sorted(v["races"].values(), key=lambda r: (r["date"], r["time"]))
        if not rs:
            continue
        won = sum(1 for r in rs if r.get("winner_odds"))
        print(f"  {track:15s} {len(rs):3d} races, {won:3d} with a result   "
              f"LK {rs[0]['date'][5:]} {rs[0]['time']} -> {rs[-1]['date'][5:]} {rs[-1]['time']}")
