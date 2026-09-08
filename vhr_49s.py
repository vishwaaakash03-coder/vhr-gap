"""
VHR 49s.co.uk backfill - past results, by date, for all three tracks.

betvirtual only serves the latest finished day, so it cannot reach backwards.
49s.co.uk can: the date is in the URL, and every race carries a SportsEvent
JSON-LD block with the finishing order.

    https://49s.co.uk/virtual-horse-racing/results/YYYY-MM-DD/{PP|SV|SD}

What comes back is the **winner and its price, not the card** - 49s does not
publish the other runners' odds. That is enough for the BY RACES analysis, which
only asks how many races passed between wins, and useless for BY CHANCES, which
needs to know which prices were on offer. `vhr_stats` already keeps those two
universes separate, so a results-only day simply does not appear in the second.

The site is a React app behind Cloudflare: plain requests and curl_cffi both get
the "enable JavaScript" shell, so this drives a real Chrome parked off-screen,
the same way vhr_betvirtual does. One page load per (date, track), then one
click per race - the app re-injects the JSON-LD for whichever race is selected.

    python vhr_49s.py 2026-09-08                 one day, all three tracks
    python vhr_49s.py 2026-08-01 2026-09-07      a range
    python vhr_49s.py 2026-09-08 --dry-run       parse, but do not touch the store
"""

import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import vhr_core as core
import vhr_data as data

UK = ZoneInfo("Europe/London")
LK = ZoneInfo("Asia/Colombo")

BASE = "https://49s.co.uk/virtual-horse-racing/results"
TRACKS = {"PP": "Portman Park", "SV": "Sprint Valley", "SD": "Steepledowns"}

LOG_PATH = os.path.join(core.BASE_DIR, "vhr.log")
DONE_MIN = 60   # winners on a track for a day; below this the day is unfinished

CHROME_ARGS = [
    "--window-position=-32000,-32000",
    "--window-size=1280,900",
    "--disable-blink-features=AutomationControlled",
    "--no-first-run", "--no-default-browser-check",
    "--disable-extensions", "--mute-audio",
]

JS_RACE_NUMBERS = r"""
() => {
  const nums = new Set();
  for (const el of document.querySelectorAll('div')) {
    if (el.children.length) continue;
    const t = (el.textContent || '').trim();
    if (/^\d{1,3}$/.test(t)) { const n = +t; if (n >= 1 && n <= 200) nums.add(n); }
  }
  return [...nums].sort((a, b) => a - b);
}
"""

JS_CLICK = r"""
(n) => {
  for (const el of document.querySelectorAll('div')) {
    if (el.children.length) continue;
    if ((el.textContent || '').trim() === String(n)) {
      (el.closest('[tabindex],[role="button"],div') || el).click();
      return true;
    }
  }
  return false;
}
"""

# The winner's price is not in the JSON-LD, only on the page. The layout is
# strictly line-based - "1st" / "10 RAMPANT RAMS" / "9/1" - so read it that way
# rather than by proximity: "1st" also turns up in dates like "Tuesday 1st
# September", and a proximity match there grabs a fraction from the wrong race.
JS_RACE = r"""
() => {
  let ld = null;
  for (const t of document.querySelectorAll('script[type="application/ld+json"]')) {
    try { const j = JSON.parse(t.textContent);
          if (j && j['@type'] === 'SportsEvent') { ld = j; break; } } catch (e) {}
  }
  const lines = (document.body.innerText || '').split('\n').map(s => s.trim());
  let odds = null, ran = null;
  for (let i = 0; i < lines.length - 2; i++) {
    if (lines[i] !== '1st') continue;
    if (!/^\d{1,3}\s+\S/.test(lines[i + 1])) continue;   // "10 RAMPANT RAMS"
    const m = lines[i + 2].match(/^(\d{1,3}\/\d{1,3}|Evs)$/i);
    if (m) { odds = m[1]; break; }
  }
  for (const l of lines) {
    const m = l.match(/^(\d{1,3})\s+ran/i);
    if (m) { ran = +m[1]; break; }
  }
  return {start: ld && ld.startDate, odds: odds, ran: ran,
          winner: ld && (ld.competitor || []).filter(c =>
            (c.additionalProperty || []).some(p =>
              p.name === 'Finish Position' && p.value === 1)).map(c => c.name)[0]};
}
"""


def log(msg):
    """Progress goes to the same log as everything else, so it can be watched."""
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 49s: {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def uk_iso_to_lk(start_iso):
    """'2026-09-06T23:48:00+01:00' -> (LK ISO date, 'HH:MM')."""
    lk = datetime.fromisoformat(start_iso).astimezone(LK)
    return lk.date().isoformat(), lk.strftime("%H:%M")


def scrape_track_day(page, day, code, log=print):
    """[{date, time, winner, winner_odds, runners}] for one track on one date."""
    url = f"{BASE}/{day.isoformat()}/{code}"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_function(
            "document.body.innerText && document.body.innerText.includes('1st')",
            timeout=30000)
    except Exception as e:
        log(f"    {day} {code}: page did not render ({type(e).__name__})")
        return []

    numbers = page.evaluate(JS_RACE_NUMBERS)
    out, last_start = [], None
    for n in numbers:
        if not page.evaluate(JS_CLICK, n):
            continue
        # Wait for the app to swap in the next race rather than guessing a delay.
        race = None
        for _ in range(25):
            page.wait_for_timeout(60)
            race = page.evaluate(JS_RACE)
            if race and race.get("start") and race["start"] != last_start:
                break
        if not race or not race.get("start") or race["start"] == last_start:
            continue
        last_start = race["start"]
        if not race.get("odds"):
            continue
        d, t = uk_iso_to_lk(race["start"])
        out.append({"date": d, "time": t, "winner": race.get("winner"),
                    "winner_odds": race["odds"].replace("EVS", "Evs"),
                    "runners": race.get("ran")})
    return out


def day_is_done(store, day):
    """True when every track already has a full day of winners for this date.

    This is what makes the backfill resumable: re-running it walks past the days
    it already finished instead of scraping them again.
    """
    start, end = core.card_window(day)
    have = {t: 0 for t in TRACKS.values()}
    for rec in store["races"].values():
        if not rec.get("winner_odds") or rec["track"] not in have:
            continue
        when = datetime.combine(date.fromisoformat(rec["date"]),
                                datetime.strptime(rec["time"], "%H:%M").time())
        if start <= when < end:
            have[rec["track"]] += 1
    return all(n >= DONE_MIN for n in have.values()), have


def backfill(start, end, dry_run=False, on_progress=None):
    """Walk the date range, merging winners into the store. Resumable."""
    from playwright.sync_api import sync_playwright

    say = on_progress or log
    totals = {"days": 0, "skipped_days": 0, "added": 0, "updated": 0, "already": 0}
    n_days = (end - start).days + 1
    say(f"backfill {start} -> {end}  ({n_days} days)")

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=False, args=CHROME_ARGS)
        page = browser.new_page()
        try:
            day = start
            while day <= end:
                idx = (day - start).days + 1
                done, have = day_is_done(data.load_store(), day)
                if done:
                    totals["skipped_days"] += 1
                    say(f"[{idx}/{n_days}] {day}: already done "
                        f"({sum(have.values())} results)")
                    day += timedelta(days=1)
                    continue

                found, records = 0, []
                for code, track in TRACKS.items():
                    for rec in scrape_track_day(page, day, code, log=say):
                        found += 1
                        records.append((track, rec))

                def apply(store, records=records):
                    a = u = k = 0
                    for track, rec in records:
                        existing = store["races"].get(
                            data.race_key(track, rec["date"], rec["time"]))
                        if existing and existing.get("winner_odds"):
                            k += 1
                            continue
                        data.upsert(store, track, rec["date"], rec["time"],
                                    winner_odds=rec["winner_odds"],
                                    winner=rec["winner"], runners=rec["runners"],
                                    result_src="49s")
                        u += 1 if existing else 0
                        a += 0 if existing else 1
                    return a, u, k

                # Written under the store lock - the collectors write the same
                # file every couple of minutes.
                a, u, k = apply(data.load_store()) if dry_run else data.update_store(apply)
                totals["added"] += a
                totals["updated"] += u
                totals["already"] += k
                totals["days"] += 1
                say(f"[{idx}/{n_days}] {day}: {found} races  "
                    f"+{a} new, +{u} completed, {k} already had")
                day += timedelta(days=1)
        finally:
            browser.close()

    return totals


def main(argv=None):
    args = [a for a in (sys.argv[1:] if argv is None else argv)]
    dry = "--dry-run" in args
    dates = [a for a in args if not a.startswith("--")]
    if not dates:
        print(__doc__.strip().splitlines()[-4])
        return
    start = date.fromisoformat(dates[0])
    end = date.fromisoformat(dates[1]) if len(dates) > 1 else start

    before = len(data.load_store()["races"])
    log(f"store before: {before} races" + ("   [dry run]" if dry else ""))
    s = backfill(start, end, dry_run=dry)
    after = len(data.load_store()["races"])
    log(f"finished: {s['days']} days scraped, {s['skipped_days']} already done, "
        f"{s['added']} results-only added, {s['updated']} carded races completed, "
        f"{s['already']} already known")
    log(f"store after : {after} races  ({after - before:+d})")


if __name__ == "__main__":
    main()
