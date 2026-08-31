# VHR — STBET UK Virtual

Two things live here:

1. **The daily racecard workbook** — every race on the day's card with its odds,
   as an ODDS matrix per track, in `days/`.
2. **Gap telemetry** — how long each odds price goes between wins, per track,
   as a browser dashboard on `http://localhost:8770`.

## Running it

One Windows scheduled task, **VHR Collector**, starts `vhr_service.py` at
**04:25 daily** and at logon. That single process runs three things:

| Part | What it does |
|---|---|
| card collector | waits for the 04:30 card swap, then scrapes the whole 24-hour card into `days/VHR Racecards YYYY.MM.DD.xlsx` |
| results collector | every 2 minutes: merges the live card into the race store, then records which odds won each finished race |
| gap dashboard | serves the analysis at `http://localhost:8770` |

The results collector merges the card as well as the result, because a race needs
**both** halves — the odds offered and the odds that won — and only the state
files carry the first. Without that, every race from the next card on would have
a winner but no card, and would quietly drop out of the BY CHANCES analysis.

Task Scheduler runs a task's actions one after another and waits for each to
finish, so three separate actions would only ever start the first. Hence one
service process with three threads.

| Double-click | |
|---|---|
| `GAP DASHBOARD.bat` | open the dashboard (starts the server if it is not up) |
| `START VHR.bat` / `STOP VHR.bat` | start / stop everything |
| `RUN NOW.bat` | scrape whatever is on the card right now |
| `CHECK SITE.bat` | not-yet-run / total races per track |

To remove the automation:

    Unregister-ScheduledTask -TaskName "VHR Collector" -Confirm:$false

## How the card scrape knows when to run

STBET swaps the whole 24-hour card in one go, normally between **04:30 and
05:00** LK. Races are not added during the day, so the card only needs scraping
once — but only after the swap.

Until it happens the site is still serving the **finished** card: 70-odd races in
total, but almost none left to run. So readiness is measured in races that have
**not started yet** — more than `CARD_READY_MIN` (20) on every track.

This is what the old `race\days\auto_scheduler.py` got wrong. Its probe counted
*total* races:

    [2026-08-31 04:30:02] Site probe: Portman Park:76, SprintValley:75,
                          Steepledowns:70 (need 30+ on 2+ tracks) — READY

Those 76 were the dead card, so it scraped an already-finished day.

## The gap analysis

For every odds price on every track: how many races it has been seen in, how
often it won, and the distribution of gaps between wins.

Two ways of counting the same gap, both shown:

* **BY CHANCES** — races that actually *offered* that price. A race with no 25/1
  runner was never a chance for 25/1 to win.
* **BY RACES** — every race that ran, offered or not.

Each runs over its own consistent set: BY RACES over every race with a known
result, BY CHANCES over the races that also have a known card. Mixing them would
undercount, since about a quarter of the historical results have no card.

**The signal** is a percentile of that price's own history. If the current
drought is longer than 95% of its past droughts it reads `VERY HOT`; above 80%,
`HOT`; above 50%, `WARM`. Fewer than 3 completed gaps reads `NEW` — too little to
say anything.

> These are RNG virtual races and each one is independent. A long drought does
> not make the next race more likely to end it. The signal describes a price's
> history, not its future.

The **next race** panel at the top crosses the upcoming card against the table:
of the prices actually on offer in the next race, which are furthest into a
drought. Clicking a line there jumps to that price's record.

The table itself is deliberately five columns — **odds, now, avg, pctile,
signal** — so a glance is enough. Everything else lives one click away: clicking
a row opens the full record for that price, including the **last 8 gaps** with
their dates, the whole gap history as a chart, and what the other counting mode
says about the same price.

It defaults to **10/1 and up**, since that is the range worth watching; `ALL
ODDS` shows the short prices too.

## Watching it from anywhere

    https://vishwaaakash03-coder.github.io/vhr-gap/

Open it on a phone, on another machine, anywhere. Add it to the home screen and
it behaves like an app.

### Why the collecting still runs here

STBET refuses every request from outside Sri Lanka. A GitHub Actions runner in
Virginia gets **403 on the API and on the site's own home page**:

    runner ip : 57.154.232.209  (Microsoft, US)
    homepage  : status=403
    api       : status=403      server: awselb/2.0

So the collecting cannot be moved to a cloud runner, whatever the provider —
the same applies to Cloudflare Workers, Oracle, and anything else outside LK.
Only the finished page travels.

`vhr_publish.py` builds `index.html` + `data.json` and force-pushes them as a
single orphan commit to the `gh-pages` branch, every 10 minutes, from inside the
service. A year of updates never becomes a year of commits.

### What happens when this PC is off

The site stays up and stays usable — it just stops moving, showing the last
push. Since the card does not change during the day, that is still a working
page. It catches up on its own when the PC comes back.

The repo is public, so the collected data is public too.

## Data

| File | |
|---|---|
| `data/races.json` | one record per race: the odds offered, and the odds that won |
| `state/*.json` | the raw card per race day, as collected |
| `days/*.xlsx` | the daily workbook |
| `vhr.log` | what everything has been doing |

The store was reset on **2026-08-31** and holds only what this system collected
itself. The old `race\days` data is not used: that scraper fired on the previous
day's finished card, so its workbooks record races that never ran and miss races
that did.

Rebuild `data/races.json` from the collected cards at any time — safe to re-run,
it only adds what is missing:

    python vhr_data.py

Start over from nothing (the existing store is moved aside, not destroyed):

    python vhr_data.py --reset

The old system's files can still be pulled in, but only when asked for
explicitly, so they cannot creep back in by accident:

    python vhr_data.py --legacy

History is thin at first. A price needs 3 completed gaps before it gets a signal
at all; until then it reads `NEW`, and the dashboard says so under the table.

## Settings

| Setting | Where |
|---|---|
| Card check interval (5 min) | `vhr_collector.py` → `POLL_SECS` |
| Ready threshold (20 races) | `vhr_collector.py` → `CARD_READY_MIN` |
| Results poll (2 min) | `vhr_results.py` → `POLL_SECS` |
| Dashboard port (8770) | `vhr_dashboard.py` → `PORT` |
| Signal bands | `vhr_stats.py` → `SIGNAL_BANDS` |
| Card rollover (04:30) | `vhr_core.py` → `CARD_ROLLOVER` |
| Hidden odds columns | `vhr_core.py` → `ODDS_HIDE` |
| Track colours | `vhr_core.py` → `TRACK_COLORS` |

## Notes

* Odds do not move once published — two scrapes 95 minutes apart matched on all
  57 races they shared. So a race is priced once and left alone.
* Odds columns in the workbook sort by actual price, so `80/1` sits before
  `100/1` and `10/3` between `3/1` and `7/2`.
* Short prices (`5/4` … `10/3`) are hidden, not removed — right-click the column
  headers in Excel and choose Unhide.
* If the workbook is open in Excel when it is written, it goes to
  `... LIVE.xlsx` so nothing is lost.
