"""LightShow engine: one background thread owns the keyboards.

Only one thing may drive the LEDs at a time, so all state changes go through
Engine.apply(). It stops whatever is running, then either sets a persistent
hardware mode and lets the thread idle, or starts stepping a software
generator.

Config lives at ~/.config/omarchy/lightshow.json and holds the current look
plus the colour settings: whether looks follow the Omarchy theme palette
("theme", the default) or your own picks ("custom"), and the picks themselves,
one list per effect. Switching back to "theme" keeps the picks for later.
"""

import json
import os
import re
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

COLOR_SOURCES = ("theme", "custom")
DEFAULT_COLORS = {"source": "theme", "effects": {}}
MAX_COLORS = 8

DEFAULT_CONFIG = {"current": dict(DEFAULT_STATE), "colors": dict(DEFAULT_COLORS)}


def _clean_hex_list(v):
    """Validate a list of '#rrggbb' strings; drop anything else."""
    out = []
    for c in v if isinstance(v, list) else []:
        if isinstance(c, str) and re.fullmatch(r"#?[0-9a-fA-F]{6}", c.strip()):
            out.append("#" + c.strip().lstrip("#").lower())
    return out[:MAX_COLORS]


def _clean_colors(v):
    v = v if isinstance(v, dict) else {}
    src = v.get("source") if v.get("source") in COLOR_SOURCES else "theme"
    fx = v.get("effects") if isinstance(v.get("effects"), dict) else {}
    return {"source": src,
            "effects": {str(k): l for k, l in ((k, _clean_hex_list(x)) for k, x in fx.items()) if l}}


def _load():
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    # Only the current look survives. Older configs carried favourites, day/night
    # profiles, a schedule and custom colours; all of that is gone (2026-09-15).
    cur = cfg.get("current") if isinstance(cfg.get("current"), dict) else {}
    return {"current": {k: cur.get(k, v) for k, v in DEFAULT_STATE.items()},
            "colors": _clean_colors(cfg.get("colors"))}


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

    def theme_palette(self):
        """The Omarchy theme's palette, or the built-in fallback."""
        return kbd.theme_colors() or list(kbd.FALLBACK)

    def custom_hex(self, name):
        """The picks for one effect as hex: saved ones, else a starting set
        (the effect's own fixed colours, or the theme palette)."""
        saved = self.cfg["colors"]["effects"].get(name)
        if saved:
            return list(saved)
        if name in effects.FIXED_DEFAULTS:
            return list(effects.FIXED_DEFAULTS[name])
        n = effects.color_slots(name)[0] or 1
        return [rgb_hex(c) for c in self.theme_palette()[:n]]

    def colors_for(self, name=None):
        """The colours a look uses: the theme palette, or this effect's picks."""
        name = name or self.cfg["current"]["effect"]
        if self.cfg["colors"]["source"] == "custom":
            return [kbd.hex_rgb(c) for c in self.custom_hex(name)]
        return self.theme_palette()

    def _set_colors(self, state):
        """Colour keys an apply() may carry. Returns True if anything changed.

        color_source:  "theme" | "custom"
        effect_colors: {"<effect>": ["#rrggbb", ...]}   (empty list = forget picks)
        """
        changed = False
        col = self.cfg["colors"]
        src = state.get("color_source")
        if src is not None:
            if src not in COLOR_SOURCES:
                raise ValueError(f"color_source must be one of {COLOR_SOURCES}")
            changed |= col["source"] != src
            col["source"] = src
        fx = state.get("effect_colors")
        if isinstance(fx, dict):
            for name, lst in fx.items():
                if name not in effects.COLOR_SLOTS and name not in effects.SOFTWARE:
                    raise ValueError(f"unknown effect '{name}'")
                clean = _clean_hex_list(lst)
                if clean:
                    col["effects"][name] = clean
                else:
                    col["effects"].pop(name, None)
                changed = True
        return changed

    # -- control --------------------------------------------------------

    def apply(self, state, save=True):
        """Switch to a new look. Stops whatever was running first.

        Colours come from the Omarchy theme by default. With color_source set
        to "custom", each effect uses its own picks (effect_colors) instead;
        both keys may ride along on any apply(). See _set_colors().
        """
        colors_changed = self._set_colors(state)
        merged = dict(self.cfg["current"])
        merged.update({k: v for k, v in state.items() if k in DEFAULT_STATE})
        name = merged["effect"]
        colors = self.colors_for(name)
        custom = self.cfg["colors"]["source"] == "custom"
        speed = float(merged.get("speed", 1.0))
        bright = float(merged.get("brightness", 1.0))

        with self._lock:
            self._gen = None  # stop any software effect before touching hardware
            if name in effects.SOFTWARE:
                fn, _ = effects.SOFTWARE[name]
                kwargs = {}
                if name == "scroll":
                    kwargs["word"] = merged.get("word") or "OMARCHY"
                if custom and name in effects.FIXED:
                    kwargs["custom"] = True
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
        if save or colors_changed:
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
                # The colours the current look would use: with custom picks a
                # theme change resolves to the same thing and nothing restarts.
                now = tuple(self.colors_for())
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
        fidelity = getattr(self.kb, "fidelity", None)
        fx = []
        for e in effects.all_effects():
            fid = fidelity(e["name"], e["kind"] == "software") if fidelity else {}
            # Greyed only when no connected board can show anything for it.
            fx.append(dict(e, fidelity=fid,
                           greyed=bool(fid) and all(v == "none" for v in fid.values())))
        boards = self.kb.boards_info() if hasattr(self.kb, "boards_info") else []
        return {
            "current": cur,
            "resolved_colors": [rgb_hex(c) for c in self.colors_for()],
            "color_source": self.cfg["colors"]["source"],
            "theme_colors": [rgb_hex(c) for c in self.theme_palette()],
            "custom_colors": {n: self.custom_hex(n) for n in effects.COLOR_SLOTS},
            "custom_saved": sorted(self.cfg["colors"]["effects"]),
            "color_slots": {n: {"max": m, "hint": h} for n, (m, h) in effects.COLOR_SLOTS.items()},
            "frame": self.frame(),
            "theme": kbd.theme_name(),
            "theme_palette": {k: rgb_hex(v) for k, v in kbd.load_palette().items()},
            "effects": fx,
            "boards": boards,
            "zones": [{"name": n, "mask": m, "label": l} for n, m, l in kbd.ZONES],
            "device": self.kb.node,
        }

    def shutdown(self):
        self._stop.set()
