"""
VHR WhatsApp - send the day's workbook to a contact, once, when it is complete.

    python vhr_whatsapp.py --login        link this PC to WhatsApp (scan the QR once)
    python vhr_whatsapp.py --send         send today's workbook now, even if already sent
    python vhr_whatsapp.py --send --day 2026-09-08
    python vhr_whatsapp.py --status       linked? sent today?

The card collector calls `send_if_due` after every cycle; the first time the
card is complete for the day, the workbook goes to CONTACT and the day is marked
in data/whatsapp_sent.json so it is never sent twice. If the send fails it is
retried on the next cycle, up to MAX_ATTEMPTS a day.

WhatsApp Web keeps its login in the browser profile, so this drives a Chrome
of its own with a persistent profile under `whatsapp_profile/` - separate from
the user's everyday Chrome, so nothing there is touched and the two never fight
over the profile lock. Log in once with a visible window; after that the window
is parked off-screen like every other browser this system runs. WhatsApp only
drops a linked device after two weeks without use, and this one is used daily.

The profile directory IS the WhatsApp session. It is gitignored; keep it so.
"""

import ctypes
import json
import os
import sys
import threading
import time
from datetime import datetime

import vhr_core as core

CONTACT      = "Akash"           # exactly as the chat is named in WhatsApp
ENABLED      = True
MAX_ATTEMPTS = 6                 # per day, before waiting for a manual send

PROFILE_DIR = os.path.join(core.BASE_DIR, "whatsapp_profile")
SENT_PATH   = os.path.join(core.BASE_DIR, "data", "whatsapp_sent.json")
LOG_PATH    = os.path.join(core.BASE_DIR, "vhr.log")
WA_URL      = "https://web.whatsapp.com/"

OFFSCREEN_ARGS = [
    "--window-position=-32000,-32000",
    "--window-size=1280,900",
    "--disable-blink-features=AutomationControlled",
    "--no-first-run", "--no-default-browser-check",
    "--disable-extensions", "--mute-audio",
]
VISIBLE_ARGS = [a for a in OFFSCREEN_ARGS if not a.startswith("--window-position")]

# WhatsApp Web renames its icons every few months, so every hook here is a list
# of alternatives. Playwright takes them as one comma-separated CSS selector.
SEL_LOGGED_IN = '#pane-side, [aria-label="Chat list"]'
SEL_QR        = 'canvas[aria-label], [data-ref], canvas'
SEL_SEARCH    = ('div[contenteditable="true"][data-tab="3"], '
                 '[aria-label="Search input textbox"], '
                 '[aria-placeholder="Search or start a new chat"], '
                 'div[contenteditable="true"][aria-label*="Search" i]')
SEL_ATTACH    = ('#main button[title="Attach"], #main [aria-label="Attach"], '
                 '#main span[data-icon="plus"], #main span[data-icon="clip"], '
                 '#main span[data-icon="attach-menu-plus"], '
                 '#main span[data-icon="plus-rounded"]')
SEL_CAPTION   = ('div[contenteditable="true"][aria-label*="caption" i], '
                 'div[contenteditable="true"][aria-placeholder*="caption" i]')
SEL_SEND      = ('[aria-label="Send"], span[data-icon="send"], '
                 'span[data-icon="wds-ic-send-filled"]')
SEL_PENDING   = '#main span[data-icon="msg-time"]'


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] whatsapp: {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# -- the sent record ------------------------------------------------------------

def load_sent():
    try:
        with open(SENT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_sent(sent):
    os.makedirs(os.path.dirname(SENT_PATH), exist_ok=True)
    tmp = SENT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sent, f, indent=1, ensure_ascii=False)
    os.replace(tmp, SENT_PATH)


def workbook_for(day):
    """The day's workbook - the LIVE copy if Excel had the main one open and
    the LIVE one is newer."""
    main = core.workbook_path(day)
    live = main[:-5] + " LIVE.xlsx"
    cands = [p for p in (main, live) if os.path.exists(p)]
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)


# -- the browser -----------------------------------------------------------------

class NotLoggedIn(Exception):
    pass


def _open(p, visible=False):
    ctx = p.chromium.launch_persistent_context(
        PROFILE_DIR, channel="chrome", headless=False, no_viewport=True,
        args=VISIBLE_ARGS if visible else OFFSCREEN_ARGS)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(WA_URL, wait_until="domcontentloaded", timeout=60000)
    return ctx, page


def _wait_logged_in(page, timeout_s):
    """True once the chat list is up; False if the QR is still showing."""
    page.wait_for_selector(f"{SEL_LOGGED_IN}, {SEL_QR}", timeout=90000)
    end = time.time() + timeout_s
    while time.time() < end:
        if page.locator(SEL_LOGGED_IN).count():
            return True
        page.wait_for_timeout(1000)
    return page.locator(SEL_LOGGED_IN).count() > 0


def _open_chat(page, contact):
    box = page.locator(SEL_SEARCH).first
    box.wait_for(state="visible", timeout=30000)
    box.click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    page.keyboard.type(contact, delay=40)
    # Click the exact-titled result rather than pressing Enter, which would
    # happily open whatever came first.
    hit = page.locator(f'#pane-side span[title="{contact}"]').first
    hit.wait_for(state="visible", timeout=20000)
    hit.click()
    page.locator(f'#main header span[title="{contact}"]').first.wait_for(
        state="visible", timeout=20000)


def _attach(page, path):
    page.locator(SEL_ATTACH).first.click()
    page.wait_for_timeout(600)
    # Prefer the "Document" entry: it takes any file type. The plain file input
    # WhatsApp renders is the fallback, picking the one that accepts anything.
    doc = page.get_by_text("Document", exact=True)
    if doc.count():
        with page.expect_file_chooser(timeout=15000) as fc:
            doc.first.click()
        fc.value.set_files(path)
        return
    inputs = page.locator('input[type="file"]')
    inputs.first.wait_for(state="attached", timeout=15000)
    chosen = inputs.last
    for i in range(inputs.count()):
        if "*" in (inputs.nth(i).get_attribute("accept") or ""):
            chosen = inputs.nth(i)
            break
    chosen.set_input_files(path)


def _send_preview(page, caption):
    send = page.locator(SEL_SEND).last
    send.wait_for(state="visible", timeout=30000)
    if caption:
        cap = page.locator(SEL_CAPTION)
        if cap.count():
            cap.first.click()
            page.keyboard.type(caption, delay=10)
    send.click()


def _wait_delivered(page, basename, timeout_s=90):
    """The file shows in the chat as an outgoing message with no pending clock."""
    end = time.time() + timeout_s
    while time.time() < end:
        page.wait_for_timeout(1000)
        outgoing = page.locator("#main .message-out").filter(has_text=basename)
        if outgoing.count() and not page.locator(SEL_PENDING).count():
            return True
    return False


def send_file(path, caption="", contact=CONTACT, visible=False, log=log):
    """Send one file to the contact. Raises NotLoggedIn if the QR is showing."""
    from playwright.sync_api import sync_playwright

    basename = os.path.basename(path)
    with sync_playwright() as p:
        ctx, page = _open(p, visible=visible)
        try:
            if not _wait_logged_in(page, timeout_s=25):
                raise NotLoggedIn("WhatsApp Web is showing the QR code")
            _open_chat(page, contact)
            _attach(page, path)
            _send_preview(page, caption)
            if not _wait_delivered(page, basename):
                raise RuntimeError("the message did not leave the pending state")
            page.wait_for_timeout(1500)      # let the upload finish flushing
        finally:
            ctx.close()
    log(f"sent {basename} to {contact}")


# -- what the collector calls ------------------------------------------------------

def caption_for(day, counts):
    total = sum(counts.values())
    parts = ", ".join(f"{t.split()[0]} {n}" for t, n in counts.items())
    return f"VHR Racecards {day:%Y.%m.%d} - {total} races ({parts})"


_alerted = set()

def alert_login_lost():
    """A single message box, once per day, since a lost login is the one thing
    that genuinely needs a person. Runs in its own thread so nothing waits."""
    key = datetime.now().date()
    if key in _alerted or os.name != "nt":
        return
    _alerted.add(key)
    text = (f"WhatsApp is not linked any more, so today's racecard could not be "
            f"sent to {CONTACT}.\n\nDouble-click  WHATSAPP LOGIN.bat  and scan the "
            f"QR code once, then  SEND TO WHATSAPP.bat.")
    threading.Thread(
        target=lambda: ctypes.windll.user32.MessageBoxW(
            0, text, "VHR - WhatsApp", 0x30 | 0x1000 | 0x40000),
        daemon=True).start()


def send_if_due(day, counts, log=log):
    """Send the day's workbook the first time the card is complete. Safe to call
    every cycle: a day already sent, or out of attempts, returns at once."""
    if not ENABLED:
        return False
    key = day.isoformat()
    sent = load_sent()
    rec = sent.get(key, {})
    if rec.get("sent_at"):
        return False
    if rec.get("attempts", 0) >= MAX_ATTEMPTS:
        return False
    path = workbook_for(day)
    if not path:
        return False

    rec["attempts"] = rec.get("attempts", 0) + 1
    try:
        send_file(path, caption_for(day, counts), log=log)
    except NotLoggedIn as e:
        rec["last_error"] = str(e)
        sent[key] = rec
        save_sent(sent)
        log(f"not linked - run WHATSAPP LOGIN.bat ({rec['attempts']}/{MAX_ATTEMPTS})")
        alert_login_lost()
        return False
    except Exception as e:
        rec["last_error"] = f"{type(e).__name__}: {e}"
        sent[key] = rec
        save_sent(sent)
        log(f"send failed ({rec['attempts']}/{MAX_ATTEMPTS}): {rec['last_error']}")
        return False

    rec.update(sent_at=datetime.now().isoformat(timespec="seconds"),
               file=os.path.basename(path), contact=CONTACT,
               races=sum(counts.values()))
    rec.pop("last_error", None)
    sent[key] = rec
    save_sent(sent)
    return True


# -- command line ------------------------------------------------------------------

def login():
    """Show the QR in a visible window and wait for the scan."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx, page = _open(p, visible=True)
        try:
            if _wait_logged_in(page, timeout_s=5):
                print("Already linked - nothing to scan.")
                return True
            print("Scan the QR code with WhatsApp on the phone:")
            print("  WhatsApp > Linked devices > Link a device")
            ok = _wait_logged_in(page, timeout_s=240)
            if ok:
                page.wait_for_timeout(4000)      # let the session settle and save
                print("Linked. This PC can now send on its own.")
                log("linked to WhatsApp")
            else:
                print("No scan within 4 minutes.")
            return ok
        finally:
            ctx.close()


def status():
    from playwright.sync_api import sync_playwright
    day = core.card_day()
    rec = load_sent().get(day.isoformat(), {})
    print(f"contact  : {CONTACT}")
    print(f"today    : {day}  " + (f"sent at {rec['sent_at']} ({rec['file']})"
                                  if rec.get("sent_at") else
                                  f"not sent  attempts {rec.get('attempts', 0)}"
                                  + (f"  last error: {rec['last_error']}"
                                     if rec.get("last_error") else "")))
    with sync_playwright() as p:
        ctx, page = _open(p)
        try:
            print("linked   :", "yes" if _wait_logged_in(page, 20) else "NO - run WHATSAPP LOGIN.bat")
        finally:
            ctx.close()


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if "--login" in args:
        sys.exit(0 if login() else 1)
    if "--status" in args:
        status()
        return
    if "--send" in args:
        day = core.card_day()
        if "--day" in args:
            day = datetime.strptime(args[args.index("--day") + 1], "%Y-%m-%d").date()
        path = workbook_for(day)
        if not path:
            print(f"No workbook for {day} yet.")
            sys.exit(1)
        import vhr_cards as cards
        import vhr_data as data
        counts = cards.card_counts(data.load_store(), day)
        print(f"Sending {os.path.basename(path)} to {CONTACT} ...")
        try:
            send_file(path, caption_for(day, counts))
        except NotLoggedIn:
            print("Not linked - run WHATSAPP LOGIN.bat first.")
            sys.exit(2)
        sent = load_sent()
        rec = sent.get(day.isoformat(), {})
        rec.update(sent_at=datetime.now().isoformat(timespec="seconds"),
                   file=os.path.basename(path), contact=CONTACT, manual=True)
        sent[day.isoformat()] = rec
        save_sent(sent)
        print("Sent.")
        return
    print(__doc__.strip().split("\n\n")[1])


if __name__ == "__main__":
    main()
