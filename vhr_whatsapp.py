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
DELETE_AFTER = True              # take the sheet off our own side once it lands
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
# WhatsApp turned the search box from a contenteditable div into a real <input>
# in late 2026, which is why only the data-tab and the fuzzy label match survive.
SEL_SEARCH    = ('input[data-tab="3"], div[contenteditable="true"][data-tab="3"], '
                 'input[aria-label*="Search" i], input[placeholder*="Search" i], '
                 '[aria-label="Search input textbox"], '
                 'div[contenteditable="true"][aria-label*="Search" i]')
SEL_ATTACH    = ('footer button[aria-label="Attach"], #main [aria-label="Attach"], '
                 '#main button[title="Attach"], '
                 'span[data-icon="ic-attach-file"], #main span[data-icon="clip"], '
                 '#main span[data-icon="plus"], '
                 '#main span[data-icon="attach-menu-plus"], '
                 '#main span[data-icon="plus-rounded"]')
SEL_CAPTION   = ('div[contenteditable="true"][aria-label*="caption" i], '
                 'div[contenteditable="true"][aria-placeholder*="caption" i]')
SEL_SEND      = ('[aria-label="Send"], span[data-icon="send"], '
                 'span[data-icon="wds-ic-send-filled"]')
SEL_PENDING   = '#main span[data-icon="msg-time"]'

# WhatsApp Web greets a long-idle session with a "What's new" modal, which sits
# over the chat list and swallows every click. Anything that only dismisses an
# announcement is safe to press; nothing here agrees to or changes anything.
DISMISS_WORDS = ("Continue", "Close", "OK", "Got it", "Not now", "Later",
                 "Maybe later", "Dismiss")

# Deleting a sent file: right-click the row, Delete, then the bin in the
# selection bar, then the one confirmation button that says exactly
# "Delete for me". Both Deletes carry the same aria-label and are told apart
# only by their role.
SEL_MENU_DELETE = '[role="menuitem"][aria-label="Delete"]'
SEL_BAR_DELETE  = '#main button[aria-label="Delete"]:not([role="menuitem"])'
DELETE_FOR_ME   = "Delete for me"


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


def _click(page, locator, timeout=30000):
    """Click, with WhatsApp's hover tooltips moved out of the way first.

    Tooltips are injected into #wa-popovers-bucket wherever the pointer happens
    to rest, and one sitting over the target is enough to make Playwright's
    actionability check spin until it times out. Parking the mouse in the
    corner clears them; the bucket is empty whenever nothing is hovered.
    """
    locator.wait_for(state="visible", timeout=timeout)
    for attempt in range(3):
        page.mouse.move(4, 4)
        page.wait_for_timeout(250)
        try:
            locator.click(timeout=10000)
            return
        except Exception:
            if attempt == 2:
                locator.click(timeout=10000, force=True)


def _dismiss_dialogs(page, rounds=3):
    """Clear any announcement modal standing over the chat list."""
    for _ in range(rounds):
        dialogs = page.locator('[role="dialog"]')
        if not dialogs.count():
            return
        clicked = False
        for word in DISMISS_WORDS:
            btn = dialogs.first.get_by_role("button", name=word, exact=True)
            if btn.count():
                btn.first.click()
                clicked = True
                break
        if not clicked:
            page.keyboard.press("Escape")
        page.wait_for_timeout(800)


def _open_chat(page, contact):
    _dismiss_dialogs(page)
    box = page.locator(SEL_SEARCH).first
    _click(page, box)
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    page.keyboard.type(contact, delay=40)
    page.wait_for_timeout(1200)          # let the result list settle
    # Click the exact-titled result rather than pressing Enter, which would
    # happily open whatever came first.
    hit = page.locator(f'#pane-side span[title="{contact}"]').first
    _click(page, hit, timeout=20000)
    # The header used to carry title="<contact>"; it is a bare span now, so the
    # chat is confirmed by what the header reads instead of by an attribute.
    header = page.locator("#main header").first
    header.wait_for(state="visible", timeout=20000)
    for _ in range(20):
        if contact.lower() in (header.inner_text() or "").lower():
            return
        page.wait_for_timeout(500)
    raise RuntimeError(f"opened a chat, but its header does not read {contact!r}")


def _attach(page, path):
    _click(page, page.locator(SEL_ATTACH).first)
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
    page.mouse.move(4, 4)
    if caption:
        cap = page.locator(SEL_CAPTION)
        if cap.count():
            cap.first.click()
            page.keyboard.type(caption, delay=10)
    _click(page, send)


# An outgoing row is the one with the tail-out bubble or a "You:" label, and it
# carries its own status as an aria-label. The old .message-out class is gone.
JS_DELIVERED = r"""
(name) => {
  const rows = [...document.querySelectorAll('#main [role="row"]')];
  for (let i = rows.length - 1; i >= 0 && i > rows.length - 12; i--) {
    const r = rows[i];
    if (!(r.innerText || '').includes(name)) continue;
    const mine = r.querySelector('[data-icon="tail-out"]')
              || [...r.querySelectorAll('[aria-label]')].some(
                   e => /^\s*you:/i.test(e.getAttribute('aria-label') || ''));
    if (!mine) continue;
    if (r.querySelector('[data-icon="msg-time"]')) return 'pending';
    const labels = [...r.querySelectorAll('[aria-label]')]
      .map(e => (e.getAttribute('aria-label') || '').trim().toLowerCase());
    if (labels.some(l => ['sent', 'delivered', 'read', 'played'].includes(l)))
      return 'done';
    return 'unknown';
  }
  return 'missing';
}
"""


def _wait_delivered(page, basename, timeout_s=120):
    """'done' once the row shows a sent/delivered/read status.

    'unknown' means the row is there and not pending, but WhatsApp has renamed
    its status labels again - the file is in the chat, so that is not a failure
    and must not trigger a resend; a resend is the worse mistake. It is not
    good enough to delete on, though.
    """
    end = time.time() + timeout_s
    state = "missing"
    while time.time() < end:
        page.wait_for_timeout(1000)
        state = page.evaluate(JS_DELIVERED, basename)
        if state == "done":
            return state
    return state


def _row_for(page, basename):
    return page.locator('#main [role="row"]').filter(has_text=basename).last


def delete_for_me(page, basename, log=log):
    """Take the sent file off this account's own side, leaving the contact's.

    "Delete for me" removes it from everywhere this account reads - this PC and
    the phone, which are one account - while the copy already delivered to the
    contact stays. "Delete for everyone" would take the sheet back from the
    person it was sent to, which is the opposite of the point, so the
    confirmation button is matched on its exact text and anything mentioning
    'everyone' is refused outright rather than clicked.
    """
    row = _row_for(page, basename)
    if not row.count():
        log(f"nothing to delete - {basename} is not in the chat")
        return False
    try:
        row.scroll_into_view_if_needed()
        row.hover()
        page.wait_for_timeout(500)
        row.click(button="right")
        page.wait_for_timeout(800)
        _click(page, page.locator(SEL_MENU_DELETE).first, timeout=15000)
        page.wait_for_timeout(800)
        _click(page, page.locator(SEL_BAR_DELETE).first, timeout=15000)

        dialog = page.locator('[role="dialog"]').first
        dialog.wait_for(state="visible", timeout=15000)
        btn = dialog.get_by_role("button", name=DELETE_FOR_ME, exact=True).first
        if not btn.count():
            btn = dialog.locator("button").filter(has_text=DELETE_FOR_ME).first
        label = (btn.inner_text() or "").strip()
        if label.casefold() != DELETE_FOR_ME.casefold():
            raise RuntimeError(f"refusing to press {label!r} - expected {DELETE_FOR_ME!r}")
        _click(page, btn, timeout=15000)

        for _ in range(20):
            page.wait_for_timeout(500)
            if not _row_for(page, basename).count():
                log(f"deleted {basename} from our side (the contact keeps it)")
                return True
        raise RuntimeError("the message is still in the chat")
    except Exception as e:
        log(f"delete failed: {type(e).__name__}: {e}")
        # Never leave the chat sitting in selection mode.
        for _ in range(3):
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        return False


def send_file(path, caption="", contact=CONTACT, visible=False, log=log):
    """Send one file to the contact. Raises NotLoggedIn if the QR is showing."""
    from playwright.sync_api import sync_playwright

    basename = os.path.basename(path)
    deleted = False
    with sync_playwright() as p:
        ctx, page = _open(p, visible=visible)
        try:
            if not _wait_logged_in(page, timeout_s=25):
                raise NotLoggedIn("WhatsApp Web is showing the QR code")
            _open_chat(page, contact)
            _attach(page, path)
            _send_preview(page, caption)
            state = _wait_delivered(page, basename)
            if state not in ("done", "unknown"):
                raise RuntimeError("the message did not leave the pending state")
            page.wait_for_timeout(1500)      # let the upload finish flushing
            log(f"sent {basename} to {contact}")
            # Only once it is definitely delivered: deleting a file that has not
            # left yet would cancel it instead of tidying up after it.
            if DELETE_AFTER and state == "done":
                deleted = delete_for_me(page, basename, log=log)
        finally:
            ctx.close()
    return deleted


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
