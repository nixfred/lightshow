"""RemoteEngine - drives a running LightShow daemon over its HTTP API.

Only one process may own the LED controller. When the daemon is running it
holds the device, so the desktop app must not open it too or the two will fight
and the effects will stutter.

This exposes the same surface the GUI uses from Engine, so the window does not
care which one it got. Attribute names and method signatures are kept identical
on purpose.
"""

import json
import urllib.error
import urllib.request

TIMEOUT = 4


class RemoteEngine:
    def __init__(self, port=8787):
        self.base = f"http://127.0.0.1:{port}"
        snap = self._get("/api/state")          # raises if no daemon is up
        self.cfg = {
            "current": snap["current"],
            "favorites": snap["favorites"],
            "profiles": snap["profiles"],
            "schedule": snap["schedule"],
        }
        self._snap = snap
        self.node = snap.get("device", "")
        # The daemon paints frames far faster than polling could follow, so the
        # preview mirrors the resolved palette rather than every frame.
        self.last_frame = [(0, 0, 0)] * 4
        self._refresh_frame()

    # -- transport ------------------------------------------------------

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=TIMEOUT) as r:
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
        self.cfg["favorites"] = snap["favorites"]
        self.cfg["profiles"] = snap["profiles"]
        self.cfg["schedule"] = snap["schedule"]
        self._refresh_frame()

    def _refresh_frame(self):
        cols = self._snap.get("resolved_colors") or ["#7aa2f7"]
        from .kbd import hex_rgb
        self.last_frame = [hex_rgb(cols[i % len(cols)]) for i in range(4)]

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

    def save_favorite(self, name):
        self._post("/api/favorite/save", {"name": name})
        self._sync()
        return name

    def delete_favorite(self, name):
        self._post("/api/favorite/delete", {"name": name})
        self._sync()

    def load_favorite(self, name):
        self._post("/api/favorite/load", {"name": name})
        self._sync()

    def save_profile(self, which):
        self._post("/api/profile/save", {"which": which})
        self._sync()

    def load_profile(self, which):
        self._post("/api/profile/load", {"which": which})
        self._sync()

    def set_schedule(self, enabled, day_at, night_at):
        self._post("/api/schedule", {"enabled": enabled, "day_at": day_at,
                                     "night_at": night_at})
        self._sync()

    def active_profile(self):
        return self._snap.get("active_profile")

    def shutdown(self):
        pass  # the daemon outlives the window; leave it running


def connect(port=8787):
    """Return a RemoteEngine if a daemon answers, else None."""
    try:
        return RemoteEngine(port)
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return None
