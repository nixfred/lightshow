"""Kernel LED-class keyboard backlights, as one LightShow board.

Most laptops expose their keyboard backlight through the kernel with no vendor
software at all:

  /sys/class/leds/<vendor>::kbd_backlight/{brightness,max_brightness}
      a white backlight with a few levels (ThinkPad, Dell, ASUS, HP, Chromebook)
  /sys/class/leds/rgb:kbd_backlight*/{brightness,max_brightness,multi_index,multi_intensity}
      the multicolor class: one entry per colour group, or one per key (TUXEDO)

All matching entries are driven together as one board. Colour groups are
spread over LightShow's four zones by index; a white backlight follows the
brightness of what the colour boards show, so a breathe still breathes and an
animation still moves, in light and dark.

Writes to sysfs need permission: LightShow's udev rule opens the files to the
`input` group (a sysfs file cannot take the uaccess ACL a hidraw node gets).
When the files are not writable, brightness goes through UPower's D-Bus
interface, which any logged-in user may call. Colour has no such fallback.
"""

import glob
import os
import subprocess
import sys
import threading
import time

from . import caps
from .kbd import (DeviceError, MODE_OFF, MODE_STATIC, MODE_BREATHING, MODE_CYCLE,
                  MODE_WAVE, ZONE_MASKS)

LEDS_ROOT = os.environ.get("LIGHTSHOW_LEDS_ROOT", "/sys/class/leds")
WRITE_INTERVAL = 0.2        # sysfs is slow and the kernel serialises it: 5 Hz is plenty
PULSE_PERIOD = 3.5          # seconds per breath for a software brightness pulse


def _read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def _luma(rgb):
    r, g, b = rgb
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


class _Led:
    """One sysfs LED entry."""

    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.max = int(_read(os.path.join(path, "max_brightness"), "1") or 1)
        self.channels = (_read(os.path.join(path, "multi_index"), "") or "").split()
        self.colour = bool(self.channels)
        self._last_b = None
        self._last_c = None

    def writable(self):
        return os.access(os.path.join(self.path, "brightness"), os.W_OK)

    def set_brightness(self, level):
        level = max(0, min(self.max, int(round(level))))
        if level == self._last_b:
            return
        with open(os.path.join(self.path, "brightness"), "w") as f:
            f.write(str(level))
        self._last_b = level

    def set_colour(self, rgb):
        if not self.colour:
            return
        by = {"red": rgb[0], "green": rgb[1], "blue": rgb[2]}
        vals = [max(0, min(255, int(by.get(ch, 0)))) for ch in self.channels]
        if vals == self._last_c:
            return
        with open(os.path.join(self.path, "multi_intensity"), "w") as f:
            f.write(" ".join(str(v) for v in vals))
        self._last_c = vals


def find_leds():
    """Every keyboard-backlight LED entry, white ones first."""
    found = []
    for path in sorted(glob.glob(os.path.join(LEDS_ROOT, "*"))):
        if "kbd_backlight" in os.path.basename(path).lower():
            found.append(path)
    return found


def present():
    return bool(find_leds())


class Backlight:
    """All kernel keyboard-backlight LEDs on the host, driven as one board."""

    def __init__(self):
        paths = find_leds()
        if not paths:
            raise DeviceError("no kernel keyboard backlight (/sys/class/leds/*kbd_backlight*)")
        self.leds = [_Led(p) for p in paths]
        self.whites = [l for l in self.leds if not l.colour]
        self.colours = [l for l in self.leds if l.colour]
        self.node = ", ".join(l.name for l in self.leds)
        self.name = "Keyboard backlight"
        n = len(self.colours)
        colour = (caps.COLOUR_NONE if n == 0 else caps.COLOUR_ONE if n == 1
                  else caps.COLOUR_ZONES if n <= 8 else caps.COLOUR_PER_KEY)
        self.caps = caps.Caps(colour=colour,
                              brightness=any(l.max > 1 for l in self.leds),
                              streams=n <= 8,       # per-key trees take ~100 writes a frame
                              modes=frozenset(), zones=min(4, max(1, n)))
        self._writable = all(l.writable() for l in self.leds)
        self._upower_ok = None
        self._last_write = 0.0
        self._pulse = None
        self._pulse_stop = threading.Event()
        self._level = 1.0                 # 0..1 brightness the look asked for
        self._lock = threading.Lock()
        if not self._writable:
            print(f"lightshow: {self.node}: sysfs not writable, brightness via UPower "
                  f"(install the udev rule for colour)", file=sys.stderr, flush=True)

    # -- writing --------------------------------------------------------

    def _brightness_upower(self, level01):
        """UPower: brightness only, needs no permissions. Returns True on success."""
        if self._upower_ok is False:
            return False
        try:
            mx = int(subprocess.run(
                ["busctl", "call", "org.freedesktop.UPower", "/org/freedesktop/UPower/KbdBacklight",
                 "org.freedesktop.UPower.KbdBacklight", "GetMaxBrightness"],
                capture_output=True, text=True, timeout=2, check=True).stdout.split()[-1])
            subprocess.run(
                ["busctl", "call", "org.freedesktop.UPower", "/org/freedesktop/UPower/KbdBacklight",
                 "org.freedesktop.UPower.KbdBacklight", "SetBrightness", "i",
                 str(int(round(level01 * mx)))],
                capture_output=True, timeout=2, check=True)
            self._upower_ok = True
            return True
        except Exception as e:
            if self._upower_ok is None:
                print(f"lightshow: UPower keyboard backlight unavailable: {e}",
                      file=sys.stderr, flush=True)
            self._upower_ok = False
            return False

    def _paint(self, zone_rgb, level01):
        """Write colours (per zone, spread over the colour LEDs) and brightness."""
        with self._lock:
            if self._writable:
                for i, led in enumerate(self.colours):
                    z = int(i * 4 / max(1, len(self.colours)))
                    led.set_colour(zone_rgb[z % len(zone_rgb)])
                    led.set_brightness(level01 * led.max)
                for led in self.whites:
                    led.set_brightness(level01 * led.max)
            else:
                self._brightness_upower(level01)
            self._last_write = time.monotonic()

    # -- the board surface ------------------------------------------------

    def set(self, zone_mask, mode, keyframes, cycle_cs=100, wave_dir=None):
        self._stop_pulse()
        peak = max(keyframes, key=lambda f: sum(f[1:4]))[1:4] if keyframes else (0, 0, 0)
        rgb = tuple(int(c) for c in peak)
        if mode == MODE_OFF:
            self._level = 0.0
            self._paint([(0, 0, 0)] * 4, 0.0)
            return
        # Brightness follows the colour's own lightness so a dim theme colour is
        # a dim key, and a white backlight tracks the same value.
        self._level = max(0.15, _luma(rgb)) if self.caps.has_colour else 1.0
        self._paint([rgb] * 4, self._level)
        if mode in (MODE_BREATHING,) or (mode in (MODE_CYCLE, MODE_WAVE) and not self.caps.has_colour):
            # No firmware effects here: breathe is a software pulse of brightness.
            # Cycle and wave on a white backlight can only be a pulse too.
            self._start_pulse(rgb)

    def solid(self, zone_mask, rgb):
        self.set(zone_mask, MODE_STATIC, [(0, *rgb)])

    def off(self, zone_mask=None):
        self.set(zone_mask or 15, MODE_OFF, [(0, 0, 0, 0)])

    def begin_software(self, name, colors):
        self._stop_pulse()

    def frame(self, colors):
        """A streamed frame: colours per zone, at most 5 a second for sysfs."""
        now = time.monotonic()
        if now - self._last_write < WRITE_INTERVAL:
            return
        zone_rgb = [tuple(int(c) for c in rgb) for rgb in colors] or [(0, 0, 0)] * 4
        level = max(_luma(c) for c in zone_rgb) if zone_rgb else 0.0
        self._paint(zone_rgb, level)

    def poll(self):
        return False

    def close(self):
        self._stop_pulse()

    # -- software brightness pulse ------------------------------------------

    def _start_pulse(self, rgb):
        self._pulse_stop.clear()
        self._pulse = threading.Thread(target=self._run_pulse, args=(rgb,), daemon=True)
        self._pulse.start()

    def _stop_pulse(self):
        self._pulse_stop.set()
        t = self._pulse
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1.0)
        self._pulse = None

    def _run_pulse(self, rgb):
        import math
        t0 = time.monotonic()
        top = self._level
        while not self._pulse_stop.is_set():
            phase = (time.monotonic() - t0) % PULSE_PERIOD / PULSE_PERIOD
            level = top * (0.08 + 0.92 * (0.5 - 0.5 * math.cos(2 * math.pi * phase)))
            self._paint([rgb] * 4, level)
            self._pulse_stop.wait(WRITE_INTERVAL)
