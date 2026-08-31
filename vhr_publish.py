"""
VHR publish - push the dashboard to GitHub Pages, so it can be read anywhere.

STBET refuses every request from outside Sri Lanka: a GitHub Actions runner in
Virginia gets 403 on the API *and* on the site's own home page. So the
collecting has to happen here, on a Sri Lankan connection, and only the finished
page travels to GitHub.

    https://<user>.github.io/<repo>/

The page is static - the analysis is baked into data.json at publish time - so
it keeps working while this PC is off. It simply stops moving until the PC comes
back and pushes again. Since the card does not change during the day, a stale
page is still a usable one.

Each publish force-pushes a single orphan commit to gh-pages, so a year of
10-minute updates never becomes a year of commits.

    python vhr_publish.py           publish once
    python vhr_publish.py --loop    publish every 10 minutes
"""

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

import vhr_core as core
import vhr_data as data
import vhr_report as report

PUBLISH_EVERY = 600                     # 10 minutes
BRANCH = "gh-pages"
OUT_DIR = os.path.join(core.BASE_DIR, "publish")
LOG_PATH = os.path.join(core.BASE_DIR, "vhr.log")


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] publish: {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def remote_url():
    """The origin of the repo this folder sits in."""
    try:
        out = subprocess.run(["git", "remote", "get-url", "origin"],
                             cwd=core.BASE_DIR, capture_output=True, text=True,
                             timeout=20)
        return out.stdout.strip() or None
    except Exception:
        return None


def build_site(out_dir):
    """dashboard.html + the payload it reads. Returns the payload."""
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)

    shutil.copy(os.path.join(core.BASE_DIR, "dashboard.html"),
                os.path.join(out_dir, "index.html"))

    payload = report.build_payload(data.load_store())
    with open(os.path.join(out_dir, "data.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))

    # Without this, Pages runs the files through Jekyll and drops some of them.
    open(os.path.join(out_dir, ".nojekyll"), "w").close()
    return payload


def git(args, cwd, check=True):
    r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                       text=True, timeout=180)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])}: {r.stderr.strip()[:200]}")
    return r


def publish_once():
    url = remote_url()
    if not url:
        log("no git remote 'origin' - nothing to publish to")
        return False

    payload = build_site(OUT_DIR)

    # A fresh repo each time keeps exactly one commit on the branch.
    shutil.rmtree(os.path.join(OUT_DIR, ".git"), ignore_errors=True)
    git(["init", "-q"], OUT_DIR)
    git(["checkout", "-q", "-b", BRANCH], OUT_DIR)
    git(["add", "-A"], OUT_DIR)
    git(["-c", "user.name=vhr", "-c", "user.email=vhr@local",
         "commit", "-q", "-m", f"vhr {datetime.now():%Y-%m-%d %H:%M}"], OUT_DIR)
    git(["push", "-q", "--force", url, BRANCH], OUT_DIR)

    cov = payload["coverage"]
    size = os.path.getsize(os.path.join(OUT_DIR, "data.json")) / 1024
    log(f"pushed {cov['with_winner']} results over {cov['days']} days "
        f"({size:.0f} KB)")
    return True


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv

    if "--loop" not in args:
        publish_once()
        return

    log(f"publisher started, every {PUBLISH_EVERY // 60} min")
    while True:
        try:
            publish_once()
        except Exception as e:
            log(f"failed: {type(e).__name__}: {e}")
        time.sleep(PUBLISH_EVERY)


if __name__ == "__main__":
    main()
