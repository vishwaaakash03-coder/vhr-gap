"""
VHR gap telemetry - local dashboard server.

    python vhr_dashboard.py            serve on http://localhost:8770
    python vhr_dashboard.py --port N   serve on another port
    python vhr_dashboard.py --lan      also answer on the local network

Serves dashboard.html and one payload at /data.json - the same file the GitHub
Actions build writes, so the page works identically here and on the published
site.
"""

import json
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import vhr_core as core
import vhr_data as data
import vhr_report as report

PORT = 8770
HTML_PATH = os.path.join(core.BASE_DIR, "dashboard.html")

_cache = {"stamp": None, "body": None}
_lock = threading.Lock()


def payload_json():
    """Recompute when the store changes, and at least once a minute.

    The store only changes when a result lands, but the next race and its
    countdown move on their own - so the cache ages out too.
    """
    try:
        mtime = os.path.getmtime(data.STORE)
    except OSError:
        mtime = None
    stamp = (mtime, int(time.time() // 60))
    with _lock:
        if _cache["body"] is None or stamp != _cache["stamp"]:
            _cache["body"] = json.dumps(report.build_payload(data.load_store()))
            _cache["stamp"] = stamp
        return _cache["body"]


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, ctype, code=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.partition("?")[0]
        try:
            if path in ("/", "/index.html"):
                with open(HTML_PATH, "r", encoding="utf-8") as f:
                    return self._send(f.read(), "text/html; charset=utf-8")
            if path in ("/data.json", "/api/data"):
                return self._send(payload_json(), "application/json")
            self._send("not found", "text/plain", 404)
        except Exception as e:
            self._send(json.dumps({"error": str(e)}), "application/json", 500)

    def log_message(self, *args):
        pass          # the collectors own vhr.log; keep request noise out of it


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    port = PORT
    if "--port" in args:
        port = int(args[args.index("--port") + 1])
    host = "0.0.0.0" if "--lan" in args else "127.0.0.1"

    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://localhost:{port}"
    print(f"VHR gap telemetry running at {url}   (Ctrl+C to stop)")
    if host == "0.0.0.0":
        print("  reachable from other devices on this network on port", port)
    if "--no-open" not in args:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
