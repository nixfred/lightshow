"""LightShow engine: one background thread owns the keyboard.

Only one thing may drive the LEDs at a time, so all state changes go through
Engine.apply(). It stops whatever is running, then either sets a persistent
hardware mode and lets the thread idle, or starts stepping a software
generator.

Config lives at ~/.config/omarchy/lightshow.json and holds the current look,
named favourites, the day/night profiles, and the auto-switch schedule.
"""

import json
import os
import threading
import time
import datetime

from . import kbd, effects, devices
from .kbd import hex_rgb, rgb_hex

CONFIG_DIR = os.path.expanduser("~/.config/omarchy")
CONFIG_PATH = os.path.join(CONFIG_DIR, "lightshow.json")

DEFAULT_STATE = {
    "effect": "breathe",      # Fred 2026-09-15: a breathe in the theme colours
    "use_theme": True,
    "colors": ["#7aa2f7"],
    "speed": 1.0,
    "brightness": 1.0,
    "word": "OMARCHY",
}

DEFAULT_CONFIG = {
    "current": dict(DEFAULT_STATE),
    "favorites": {},
    "profiles": {
        "day": dict(DEFAULT_STATE, effect="wave", brightness=1.0),
        "night": dict(DEFAULT_STATE, effect="breathe", brightness=0.35),
    },
    "schedule": {"enabled": False, "day_at": "07:00", "night_at": "20:00"},
}


def _load():
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    # Merge so a config written by an older version keeps working.
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, json.loads(json.dumps(v)))
    for k, v in DEFAULT_STATE.items():
        cfg["current"].setdefault(k, v)
    return cfg


def _save(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)  # atomic, never leaves a half-written config


def _hhmm(s, fallback):
    try:
        h, m = s.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return fallback


class Engine:
    def __init__(self):
        self.cfg = _load()
        # Every supported keyboard that is plugged in (MSI MysticLight, KB7).
        self.kb = devices.open_all()
        # Last frame actually written to the hardware, so a UI can mirror it.
        self.last_frame = [(0, 0, 0)] * 4
        self._stop = threading.Event()
        self._gen = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._sched = threading.Thread(target=self._scheduler, daemon=True)
        self._sched.start()
        self._themewatch = threading.Thread(target=self._watch_theme, daemon=True)
        self._themewatch.start()
        self.apply(self.cfg["current"], save=False)

    # -- colours --------------------------------------------------------

    def colors_for(self, state):
        if state.get("use_theme", True):
            cols = kbd.theme_colors()
        else:
            cols = [hex_rgb(c) for c in state.get("colors") or ["#7aa2f7"]]
        return cols or list(kbd.FALLBACK)

    # -- control --------------------------------------------------------

    def apply(self, state, save=True):
        """Switch to a new look. Stops whatever was running first."""
        merged = dict(self.cfg["current"])
        # Colours always come from the Omarchy theme (Fred, 2026-09-15: "no
        # matter what I select or pick, always the theme colours"; then "remove
        # the custom colours, not needed"). Whatever a caller sends for colours
        # or use_theme is ignored, so no front end, favourite, profile or API
        # call can ever switch the theme off.
        merged.update({k: v for k, v in state.items()
                       if k in DEFAULT_STATE and k not in ("use_theme", "colors")})
        merged["use_theme"] = True
        name = merged["effect"]
        colors = self.colors_for(merged)
        speed = float(merged.get("speed", 1.0))
        bright = float(merged.get("brightness", 1.0))

        with self._lock:
            self._gen = None  # stop any software effect before touching hardware
            if name in effects.SOFTWARE:
                fn, _ = effects.SOFTWARE[name]
                kwargs = {}
                if name == "scroll":
                    kwargs["word"] = merged.get("word") or "OMARCHY"
                gen = fn(colors, speed=speed, **kwargs)
                self._bright = bright
                self._gen = gen
                # Boards that cannot animate per frame (the KB7) get one look now.
                begin = getattr(self.kb, "begin_software", None)
                if begin:
                    begin(name, [kbd.scale(c, bright) for c in colors])
            else:
                effects.apply_hardware(self.kb, name, colors, speed, bright)
                cols = [kbd.scale(c, bright) for c in colors]
                self.last_frame = [cols[i % len(cols)] for i in range(4)]

        self.cfg["current"] = merged
        if save:
            _save(self.cfg)
        return merged

    def _run(self):
        """Step whichever software generator is current. Idle when none."""
        while not self._stop.is_set():
            with self._lock:
                gen = self._gen
                bright = getattr(self, "_bright", 1.0)
            if gen is None:
                time.sleep(0.08)
                continue
            try:
                frame, delay = next(gen)
            except StopIteration:
                with self._lock:
                    if self._gen is gen:
                        self._gen = None
                continue
            except Exception:
                with self._lock:
                    if self._gen is gen:
                        self._gen = None
                continue
            try:
                # Re-check: apply() may have swapped effects mid-frame.
                with self._lock:
                    if self._gen is not gen:
                        continue
                    painted = [kbd.scale(c, bright) for c in frame]
                    self.kb.frame(painted)
                    self.last_frame = painted
            except OSError:
                time.sleep(0.2)
            time.sleep(max(0.01, delay))

    # -- theme following -------------------------------------------------

    def _watch_theme(self):
        """Re-apply when the Omarchy theme changes, so colours follow it.

        Polls the resolved palette rather than watching the directory: the
        theme symlink, the file and its contents can all change independently,
        and what we actually care about is whether the colours we would use
        came out different. Cheap enough at 4 second intervals.
        """
        last = None
        tick = 0
        while not self._stop.is_set():
            time.sleep(1)
            # A replugged board comes back blank, and a profile switch shows the
            # board's own lighting: either way, give it the look back. Every
            # second, so a button press is answered within one.
            poll = getattr(self.kb, "poll", None)
            if poll and poll():
                self.apply(self.cfg["current"], save=False)
            tick += 1
            if tick % 4:
                continue
            if not self.cfg["current"].get("use_theme", True):
                last = None
                continue
            try:
                now = tuple(kbd.theme_colors())
            except Exception:
                continue
            if last is not None and now != last:
                # Same look, freshly resolved colours.
                self.apply(self.cfg["current"], save=False)
            last = now

    # -- day / night ----------------------------------------------------

    def _scheduler(self):
        """Flip between the day and night profiles at the configured times."""
        last = None
        while not self._stop.is_set():
            time.sleep(20)
            s = self.cfg.get("schedule", {})
            if not s.get("enabled"):
                last = None
                continue
            now = datetime.datetime.now()
            mins = now.hour * 60 + now.minute
            day_at = _hhmm(s.get("day_at", "07:00"), 420)
            night_at = _hhmm(s.get("night_at", "20:00"), 1200)
            if day_at <= night_at:
                want = "day" if day_at <= mins < night_at else "night"
            else:  # night window wraps past midnight
                want = "night" if mins >= night_at or mins < day_at else "day"
            if want != last:
                prof = self.cfg["profiles"].get(want)
                if prof:
                    self.apply(prof)
                last = want

    def active_profile(self):
        s = self.cfg.get("schedule", {})
        if not s.get("enabled"):
            return None
        now = datetime.datetime.now()
        mins = now.hour * 60 + now.minute
        day_at = _hhmm(s.get("day_at", "07:00"), 420)
        night_at = _hhmm(s.get("night_at", "20:00"), 1200)
        if day_at <= night_at:
            return "day" if day_at <= mins < night_at else "night"
        return "night" if mins >= night_at or mins < day_at else "day"

    # -- persistence ----------------------------------------------------

    def save_favorite(self, name):
        name = (name or "").strip()
        if not name:
            raise ValueError("favourite needs a name")
        self.cfg["favorites"][name] = dict(self.cfg["current"])
        _save(self.cfg)
        return name

    def delete_favorite(self, name):
        self.cfg["favorites"].pop(name, None)
        _save(self.cfg)

    def load_favorite(self, name):
        fav = self.cfg["favorites"].get(name)
        if not fav:
            raise KeyError(name)
        return self.apply(fav)

    def save_profile(self, which):
        if which not in ("day", "night"):
            raise ValueError("profile must be day or night")
        self.cfg["profiles"][which] = dict(self.cfg["current"])
        _save(self.cfg)

    def load_profile(self, which):
        prof = self.cfg["profiles"].get(which)
        if not prof:
            raise KeyError(which)
        return self.apply(prof)

    def set_schedule(self, enabled, day_at, night_at):
        self.cfg["schedule"] = {
            "enabled": bool(enabled),
            "day_at": day_at or "07:00",
            "night_at": night_at or "20:00",
        }
        _save(self.cfg)

    # -- introspection --------------------------------------------------

    def snapshot(self):
        cur = self.cfg["current"]
        return {
            "current": cur,
            "resolved_colors": [rgb_hex(c) for c in self.colors_for(cur)],
            "favorites": self.cfg["favorites"],
            "profiles": self.cfg["profiles"],
            "schedule": self.cfg["schedule"],
            "active_profile": self.active_profile(),
            "theme": kbd.theme_name(),
            "theme_palette": {k: rgb_hex(v) for k, v in kbd.load_palette().items()},
            "effects": effects.all_effects(),
            "zones": [{"name": n, "mask": m, "label": l} for n, m, l in kbd.ZONES],
            "device": self.kb.node,
        }

    def shutdown(self):
        self._stop.set()
