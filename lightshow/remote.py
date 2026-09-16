"""RemoteEngine - drives a running LightShow daemon over its HTTP API.

Only one process may own the LED controller. When the daemon is running it
holds the device, so the desktop app must not open it too or the two will fight
and the effects will stutter.

This exposes the same surface the GUI uses from Engine, so the window does not
care which one it got. Attribute names and method signatures are kept identical
on purpose.
"""

import json
import threading
import time
import urllib.error
import urllib.request

from .kbd import hex_rgb

TIMEOUT = 4
FRAME_POLL = 0.15   # seconds between /api/frame reads for the live preview


class RemoteEngine:
    def __init__(self, port=8787):
        self.base = f"http://127.0.0.1:{port}"
        snap = self._get("/api/state")          # raises if no daemon is up
        self.cfg = {"current": snap["current"]}
        self._snap = snap
        self.node = snap.get("device", "")
        # What is on the keys right now, kept fresh by a background poll so the
        # window's preview mirrors the daemon rather than guessing from the palette.
        self.last_frame = [hex_rgb(c) for c in snap.get("frame") or ["#000000"] * 4]
        self._poll = threading.Thread(target=self._poll_frames, daemon=True)
        self._poll.start()

    # -- transport ------------------------------------------------------

    def _get(self, path, timeout=TIMEOUT):
        with urllib.request.urlopen(self.base + path, timeout=timeout) as r:
            return json.load(r)

    def _post(self, path, body):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r)

    def _sync(self):
        try:
            snap = self._get("/api/state")
        except Exception:
            return
        self._snap = snap
        self.cfg["current"] = snap["current"]

    def _poll_frames(self):
        while True:
            try:
                frame = self._get("/api/frame", timeout=1)["frame"]
                self.last_frame = [hex_rgb(c) for c in frame]
            except Exception:
                pass                             # daemon busy or gone: keep the last frame
            time.sleep(FRAME_POLL)

    # -- the Engine surface the GUI uses --------------------------------

    class _KB:
        def __init__(self, node):
            self.node = node

    @property
    def kb(self):
        return RemoteEngine._KB(self.node)

    def apply(self, state, save=True):
        try:
            self._post("/api/apply", state)
        except Exception:
            pass
        self._sync()
        return self.cfg["current"]

    def shutdown(self):
        pass  # the daemon outlives the window; leave it running


def connect(port=8787):
    """Return a RemoteEngine if a daemon answers, else None."""
    try:
        return RemoteEngine(port)
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return None
