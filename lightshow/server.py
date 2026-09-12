"""LightShow HTTP server.

Binds to 127.0.0.1 only. The UI is a single page served from ui.html; the API
is a handful of JSON endpoints over the Engine.

Standard library only, so there is nothing to install and nothing to keep
updated. ThreadingHTTPServer because the browser opens several requests at once
and a single-threaded server would serialise them behind the effect loop.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine import Engine

HERE = os.path.dirname(os.path.abspath(__file__))
UI_PATH = os.path.join(HERE, "ui.html")

engine = None  # set by serve()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass  # a request log per animation poll is just noise

    # -- helpers --------------------------------------------------------

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            return {}

    # -- routes ---------------------------------------------------------

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                with open(UI_PATH, "rb") as f:
                    return self._send(200, f.read().decode(), "text/html; charset=utf-8")
            except OSError:
                return self._send(500, {"error": "ui.html missing"})
        if self.path == "/api/state":
            return self._send(200, engine.snapshot())
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        body = self._body()
        try:
            if self.path == "/api/apply":
                return self._send(200, {"ok": True, "current": engine.apply(body)})

            if self.path == "/api/favorite/save":
                name = engine.save_favorite(body.get("name"))
                return self._send(200, {"ok": True, "name": name,
                                        "favorites": engine.cfg["favorites"]})

            if self.path == "/api/favorite/load":
                engine.load_favorite(body.get("name"))
                return self._send(200, {"ok": True, "current": engine.cfg["current"]})

            if self.path == "/api/favorite/delete":
                engine.delete_favorite(body.get("name"))
                return self._send(200, {"ok": True,
                                        "favorites": engine.cfg["favorites"]})

            if self.path == "/api/profile/save":
                engine.save_profile(body.get("which"))
                return self._send(200, {"ok": True,
                                        "profiles": engine.cfg["profiles"]})

            if self.path == "/api/profile/load":
                engine.load_profile(body.get("which"))
                return self._send(200, {"ok": True, "current": engine.cfg["current"]})

            if self.path == "/api/schedule":
                engine.set_schedule(body.get("enabled"), body.get("day_at"),
                                    body.get("night_at"))
                return self._send(200, {"ok": True,
                                        "schedule": engine.cfg["schedule"]})

            if self.path == "/api/quit":
                self._send(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return

        except (ValueError, KeyError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:  # never take the server down over one bad request
            return self._send(500, {"error": repr(e)})

        return self._send(404, {"error": "not found"})


def serve(host="127.0.0.1", port=8787):
    global engine
    engine = Engine()
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd, engine
