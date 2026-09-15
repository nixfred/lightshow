"""Driver for the Turtle Beach Command Series KB7 (USB 10f5:5038).

The KB7 is not a zone controller like the MSI MysticLight board. It has 101
individually lit keys, and its whole backlight lives in ONE persistent
per-profile record, HID feature report 0x11 on the control interface (USB
interface 2). Protocol, as captured from Turtle Beach Swarm II on firmware 1.22
(2026-09-14) and reproduced byte for byte offline:

    [0x11][len16 LE = 0x015c][profile][mode][speed][brightness 0-100][b7][b8][b9]
    [7 x 48 colour bytes: R x16, G x16, B x16]   a key at position p has
                                                 R at 10+p, G at 10+p+16, B at 10+p+32
    [sum16 LE of every byte before]

    mode:  0x01 static, 0x07 breathe, 0x0a wave. Effects run on the keyboard's
           own microcontroller; the host sends nothing per frame.
    b7:    0x0b for custom colours (what Swarm writes for a static picker colour)

The record is written with a plain SET_REPORT and no select. Reading it back
needs a select first: SET 0x05 = [05][profile][0xb0][00], poll GET 0x05 until
byte 1 == 0x01, then GET 0x11. Report 0x06 byte 2 is the active profile, and
only the active profile's record is visible on the keys.

Flash wear: 0x11 is persistent storage. Whether the firmware commits every
SET to flash or caches it is not known, so this driver writes a record only
when the look changes, and rate-limits software-effect frames hard. Animated
effects that need per-frame colour belong on the interface-1 PWM stream, which
is not decoded yet.
"""

import fcntl
import glob
import json
import os
import time

from .kbd import (DeviceError, MODE_OFF, MODE_STATIC, MODE_BREATHING,
                  MODE_CYCLE, MODE_WAVE, Z_WASD, Z_ALPHA, Z_NAV, Z_NUMPAD,
                  ZONE_MASKS)

VID, PID = "10F5", "5038"
CONTROL_INTERFACE = 2

LIGHT_REPORT = 0x11
LIGHT_LEN = 348
LIGHT_HDR = 10
SELECT_REPORT = 0x05
SELECT_LIGHT = 0xB0
PROFILE_REPORT = 0x06

KB7_MODE = {MODE_STATIC: 0x01, MODE_BREATHING: 0x07, MODE_WAVE: 0x0A}
B7_CUSTOM = 0x0B

# Software-effect frames are throttled to one record write per this many
# seconds. Applying a look (static, breathe, wave, off) is never throttled.
FRAME_MIN_INTERVAL = 5.0

SETTLE_AFTER_SET = 0.3       # seconds between any SET and the next request
SELECT_POLL = 0.15           # Swarm's own select polling cadence
BACKOFF_AFTER_FAILURE = 10.0 # leave a wedged handler alone to recover

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "kb7_led_positions.json")


def _ioc(direction, typ, nr, size):
    return (direction << 30) | (size << 16) | (ord(typ) << 8) | nr


def find_control_node():
    """hidraw node for the KB7's control interface, or None."""
    override = os.environ.get("LIGHTSHOW_KB7_DEVICE")
    if override:
        return override if os.path.exists(override) else None
    want = f"HID_ID=0003:0000{VID}:0000{PID}"
    for uevent in sorted(glob.glob("/sys/class/hidraw/hidraw*/device/uevent")):
        try:
            txt = open(uevent).read()
        except OSError:
            continue
        if want not in txt.upper():
            continue
        phys = [l for l in txt.splitlines() if l.startswith("HID_PHYS=")]
        if phys and phys[0].endswith(f"/input{CONTROL_INTERFACE}"):
            return "/dev/" + uevent.split("/")[4]
    return None


def load_positions():
    """{key_id: position} for every key that has an LED, plus the zone map."""
    with open(DATA) as f:
        data = json.load(f)
    positions = {int(k): p for k, p in data["positions"].items() if p >= 0}
    zones = {int(mask): [int(k) for k in keys] for mask, keys in data["zones"].items()}
    return positions, zones


def checksummed(record):
    b = bytearray(record)
    s = sum(b[:-2]) & 0xFFFF
    b[-2], b[-1] = s & 0xFF, s >> 8
    return bytes(b)


class Keyboard:
    """One open handle to the KB7 control interface.

    Same surface as kbd.Keyboard, so effects and the engine drive it the same
    way. The 4 LightShow zones map onto KB7 key groups (see the data file).
    """

    name = "Turtle Beach Command Series KB7"

    def __init__(self, node=None):
        node = node or find_control_node()
        if not node:
            raise DeviceError(f"no Turtle Beach KB7 found ({VID}:{PID})")
        try:
            self.fd = os.open(node, os.O_RDWR)
        except PermissionError as e:
            raise DeviceError(
                f"cannot open {node}: {e}. Install "
                "/etc/udev/rules.d/60-turtle-beach-kb7.rules") from e
        self.node = node
        self.positions, self.zones = load_positions()
        self._template = None      # the board's own record, read once per profile
        self._profile = None
        self._last_written = None
        self._last_frame_write = 0.0
        self._last_frame_palette = None
        self._brightness_before_off = None
        self._last_set = 0.0
        self._backoff_until = 0.0

    # -- raw feature reports --------------------------------------------

    # Pacing, learned the hard way on firmware 1.22 (2026-09-14): a GET that
    # follows a SET within a few milliseconds wedges the firmware's
    # feature-report handler (ETIMEDOUT, then stale buffers). Swarm never reads
    # straight after a write and polls its select slowly. Hammering a wedged
    # handler with more requests keeps it wedged; left alone it recovers in
    # seconds. So: settle after every SET, and back off after any failure.

    def _pace(self):
        now = time.monotonic()
        if now < self._backoff_until:
            raise OSError(f"KB7 control interface backing off for "
                          f"{self._backoff_until - now:.1f}s after a failed request")
        wait = self._last_set + SETTLE_AFTER_SET - now
        if wait > 0:
            time.sleep(wait)

    def _get(self, rid, n):
        self._pace()
        buf = bytearray(n + 1)
        buf[0] = rid
        try:
            fcntl.ioctl(self.fd, _ioc(3, "H", 0x07, n + 1), buf, True)
        except OSError:
            self._backoff_until = time.monotonic() + BACKOFF_AFTER_FAILURE
            raise
        return bytes(buf)

    def _set(self, data):
        self._pace()
        buf = bytearray(data)
        try:
            fcntl.ioctl(self.fd, _ioc(3, "H", 0x06, len(buf)), buf, True)
        except OSError:
            self._backoff_until = time.monotonic() + BACKOFF_AFTER_FAILURE
            raise
        finally:
            self._last_set = time.monotonic()

    # -- board state ----------------------------------------------------

    def _active_profile(self):
        return self._get(PROFILE_REPORT, 3)[2]

    def _read_light_record(self, profile):
        self._set(bytes([SELECT_REPORT, profile & 0xFF, SELECT_LIGHT, 0x00]))
        for _ in range(20):
            time.sleep(SELECT_POLL)
            if self._get(SELECT_REPORT, 3)[1] == 0x01:
                break
        else:
            raise DeviceError("KB7 never became ready to read the lighting record")
        rec = self._get(LIGHT_REPORT, LIGHT_LEN - 1)
        if rec[0] != LIGHT_REPORT or len(rec) != LIGHT_LEN:
            raise DeviceError("KB7 returned a malformed lighting record")
        if (rec[-2] | rec[-1] << 8) != (sum(rec[:-2]) & 0xFFFF):
            raise DeviceError("KB7 lighting record failed its checksum")
        return rec

    def _template_for_active(self):
        profile = self._active_profile()
        if self._template is None or profile != self._profile:
            # Never invent a template: the unmapped slots and the speed must be
            # exactly what the board already holds.
            self._template = self._read_light_record(profile)
            self._profile = profile
            self._last_written = None
        return bytearray(self._template), profile

    # -- writing --------------------------------------------------------

    def _write(self, zone_colours, mode_byte, brightness=None):
        """zone_colours: {zone_mask: (r,g,b)}. Keys outside the given zones
        keep the colour the board already has."""
        rec, profile = self._template_for_active()
        rec[3] = profile
        rec[4] = mode_byte
        if brightness is not None:
            rec[6] = max(0, min(100, int(brightness)))
        if mode_byte == KB7_MODE[MODE_STATIC]:
            rec[7] = B7_CUSTOM
        for mask, rgb in zone_colours.items():
            r, g, b = (max(0, min(255, int(c))) for c in rgb)
            for key in self.zones.get(mask, ()):
                p = self.positions.get(key)
                if p is None:
                    continue
                base = LIGHT_HDR + p
                rec[base], rec[base + 16], rec[base + 32] = r, g, b
        out = checksummed(rec)
        if out == self._last_written:
            return False
        self._set(out)
        self._last_written = out
        self._template = bytearray(out)   # the board now holds this record
        return True

    def _restore_brightness(self):
        """Brightness to write when leaving "off", else None (keep the board's).

        Reads the board's record first if we have not yet, so a board that was
        already dark when LightShow started still comes back on.
        """
        if self._template is None:
            self._template_for_active()
        if self._template[6] == 0:
            return self._brightness_before_off or 100
        return None

    def set(self, zone_mask, mode, keyframes, cycle_cs=100, wave_dir=None):
        """Same call as kbd.Keyboard.set. keyframes: [(t, r, g, b), ...]."""
        # The KB7 animates on its own microcontroller, so it wants one colour:
        # the first keyframe that is at least half as bright as the brightest.
        # That is the palette's first colour (the theme accent) for static,
        # wave and cycle, and the peak rather than the dim trough for breathe.
        if keyframes:
            peak = max(sum(f[1:4]) for f in keyframes)
            rgb = next(tuple(f[1:4]) for f in keyframes if sum(f[1:4]) * 2 >= peak)
        else:
            rgb = (0, 0, 0)
        masks = [m for m in ZONE_MASKS if zone_mask & m]
        if mode == MODE_OFF:
            # No captured "off" mode byte: static at brightness 0 is a field
            # we have seen the board honour. Remember the level to come back to.
            if self._template is not None and self._template[6] != 0:
                self._brightness_before_off = self._template[6]
            elif self._template is None:
                self._template_for_active()
                if self._template[6] != 0:
                    self._brightness_before_off = self._template[6]
            self._write({}, KB7_MODE[MODE_STATIC], brightness=0)
            return
        # The KB7 has no keyframe cycle; cycle falls back to static.
        mode_byte = KB7_MODE.get(mode, KB7_MODE[MODE_STATIC])
        self._write({m: rgb for m in masks}, mode_byte,
                    brightness=self._restore_brightness())

    def solid(self, zone_mask, rgb):
        self.set(zone_mask, MODE_STATIC, [(0, *rgb)])

    def off(self, zone_mask=None):
        self.set(zone_mask or 15, MODE_OFF, [(0, 0, 0, 0)])

    def frame(self, colors):
        """One software-effect frame: 4 zone colours in one record.

        Effects like smatter reshuffle the same palette many times a second.
        The KB7 cannot take that as record writes (flash wear), so a frame is
        written only when the SET of colours in it changes, and then no more
        than once per FRAME_MIN_INTERVAL. The board keeps the first
        arrangement it was given until the palette itself changes.
        """
        palette = frozenset(tuple(int(c) for c in rgb) for rgb in colors)
        if palette == self._last_frame_palette:
            return
        now = time.monotonic()
        if now - self._last_frame_write < FRAME_MIN_INTERVAL:
            return
        wrote = self._write(dict(zip(ZONE_MASKS, colors)), KB7_MODE[MODE_STATIC],
                            brightness=self._restore_brightness())
        self._last_frame_palette = palette
        if wrote:
            self._last_frame_write = now

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass
