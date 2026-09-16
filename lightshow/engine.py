"""LightShow engine: one background thread owns the keyboards.

Only one thing may drive the LEDs at a time, so all state changes go through
Engine.apply(). It stops whatever is running, then either sets a persistent
hardware mode and lets the thread idle, or starts stepping a software
generator.

Config lives at ~/.config/omarchy/lightshow.json and holds the current look.
Colours are never stored: every look uses the Omarchy theme's palette.
"""

import json
import os
import threading
import time

from . import kbd, effects, devices
from .kbd import rgb_hex

CONFIG_DIR = os.path.expanduser("~/.config/omarchy")
CONFIG_PATH = os.path.join(CONFIG_DIR, "lightshow.json")

DEFAULT_STATE = {
    "effect": "breathe",      # Fred 2026-09-15: a breathe in the theme colours
    "speed": 1.0,
    "brightness": 1.0,
    "word": "OMARCHY",        # what the scroll effect spells
}

DEFAULT_CONFIG = {"current": dict(DEFAULT_STATE)}


def _load():
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    # Only the current look survives. Older configs carried favourites, day/night
    # profiles, a schedule and custom colours; all of that is gone (2026-09-15).
    cur = cfg.get("current") if isinstance(cfg.get("current"), dict) else {}
    return {"current": {k: cur.get(k, v) for k, v in DEFAULT_STATE.items()}}


def _save(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)  # atomic, never leaves a half-written config


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
        self._themewatch = threading.Thread(target=self._watch_theme, daemon=True)
        self._themewatch.start()
        self.apply(self.cfg["current"], save=False)

    # -- colours --------------------------------------------------------

    def colors_for(self, _state=None):
        """The palette every look uses: the Omarchy theme's, or the built-in
        fallback when there is no theme to read."""
        return kbd.theme_colors() or list(kbd.FALLBACK)

    # -- control --------------------------------------------------------

    def apply(self, state, save=True):
        """Switch to a new look. Stops whatever was running first.

        Colours always come from the Omarchy theme (Fred, 2026-09-15: "no
        matter what I select or pick, always the theme colours"). Anything a
        caller sends about colours is ignored.
        """
        merged = dict(self.cfg["current"])
        merged.update({k: v for k, v in state.items() if k in DEFAULT_STATE})
        name = merged["effect"]
        colors = self.colors_for()
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
                # Boards that also keep a still look (the KB7) get one now.
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
            try:
                now = tuple(kbd.theme_colors())
            except Exception:
                continue
            if last is not None and now != last:
                # Same look, freshly resolved colours.
                self.apply(self.cfg["current"], save=False)
            last = now

    # -- introspection --------------------------------------------------

    def frame(self):
        """The four zone colours on the keys right now, as hex."""
        return [rgb_hex(c) for c in self.last_frame]

    def snapshot(self):
        cur = self.cfg["current"]
        return {
            "current": cur,
            "resolved_colors": [rgb_hex(c) for c in self.colors_for()],
            "frame": self.frame(),
            "theme": kbd.theme_name(),
            "theme_palette": {k: rgb_hex(v) for k, v in kbd.load_palette().items()},
            "effects": effects.all_effects(),
            "zones": [{"name": n, "mask": m, "label": l} for n, m, l in kbd.ZONES],
            "device": self.kb.node,
        }

    def shutdown(self):
        self._stop.set()
