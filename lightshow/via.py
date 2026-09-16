"""QMK keyboards with VIA support (Keychron and most custom boards), as a
LightShow board, over their raw-HID interface.

Detection needs no vendor list: a VIA keyboard advertises a raw-HID interface
with usage page 0xFF60 / usage 0x61 in its HID report descriptor, which the
kernel exposes read-only under /sys/class/hidraw. Talking to it needs the
hidraw node to be writable: `lightshow status` prints the one udev line to add
for a keyboard it can see but not open.

Protocol (quantum/via.h and via.c in qmk_firmware, protocol 0x000D):
  every report is exactly 32 bytes; on Linux the write carries a leading
  report-id byte of 0 first. Byte 0 command, byte 1 channel, byte 2 value id,
  bytes 3.. data. Replies echo the command in byte 0; an unknown command comes
  back as 0xFF (id_unhandled).
  0x01 get_protocol_version -> bytes 1..2 = version, big-endian
  0x07 custom_set_value / 0x08 custom_get_value on a channel:
      3 rgb_matrix, 2 rgblight: value 1 brightness (0..255), 2 effect (0 = off,
      1 = solid colour, higher = firmware effects whose numbering differs per
      build), 3 speed, 4 colour = (hue, saturation) bytes; 1 backlight: value 1
      brightness, 2 effect (white backlight, no colour).
  0x09 custom_save flushes to EEPROM; LightShow never sends it, so nothing
  wears and the keyboard's own saved look returns when LightShow stops.
Protocol versions below 0x000C reused command 0x07 for a different lighting
scheme, so older firmware is reported and left alone.
"""

import colorsys
import glob
import os
import select
import sys
import time

from . import caps
from .kbd import DeviceError, MODE_OFF, MODE_STATIC, MODE_BREATHING
from .pulse import BrightnessPulse

USAGE_PAGE = 0xFF60
USAGE = 0x61
REPORT = 32
MIN_PROTOCOL = 0x000C
CMD_VERSION, CMD_SET, CMD_GET, CMD_UNHANDLED = 0x01, 0x07, 0x08, 0xFF
CH_BACKLIGHT, CH_RGBLIGHT, CH_RGB_MATRIX = 1, 2, 3
V_BRIGHTNESS, V_EFFECT, V_SPEED, V_COLOR = 1, 2, 3, 4
EFFECT_OFF, EFFECT_SOLID = 0, 1
WRITE_INTERVAL = 0.1         # 10 updates a second is plenty for a one-colour board
HIDRAW_ROOT = os.environ.get("LIGHTSHOW_HIDRAW_ROOT", "/sys/class/hidraw")


def descriptor_has_via(desc):
    """True when a HID report descriptor declares the VIA raw-HID usage."""
    i, page = 0, None
    while i < len(desc):
        b = desc[i]
        i += 1
        if b == 0xFE:                      # long item: skip
            n = desc[i] if i < len(desc) else 0
            i += 2 + n
            continue
        size = (0, 1, 2, 4)[b & 3]
        typ, tag = (b >> 2) & 3, b >> 4
        data = int.from_bytes(desc[i:i + size], "little")
        i += size
        if typ == 1 and tag == 0:          # global: Usage Page
            page = data
        elif typ == 2 and tag == 0:        # local: Usage (4-byte form carries the page)
            up, u = (data >> 16, data & 0xFFFF) if size == 4 else (page, data)
            if up == USAGE_PAGE and u == USAGE:
                return True
    return False


def find_nodes():
    """[(hidraw node, name, 'VVVV:PPPP')] for every VIA raw-HID interface."""
    out = []
    for dev in sorted(glob.glob(os.path.join(HIDRAW_ROOT, "hidraw*"))):
        try:
            with open(os.path.join(dev, "device", "report_descriptor"), "rb") as f:
                desc = f.read()
            uevent = open(os.path.join(dev, "device", "uevent")).read()
        except OSError:
            continue
        if not descriptor_has_via(desc):
            continue
        name = ident = ""
        for line in uevent.splitlines():
            if line.startswith("HID_NAME="):
                name = line[9:]
            elif line.startswith("HID_ID="):
                parts = line[7:].split(":")
                if len(parts) == 3:
                    ident = f"{parts[1][-4:]}:{parts[2][-4:]}".lower()
        out.append(("/dev/" + os.path.basename(dev), name or "QMK/VIA keyboard", ident))
    return out


def present():
    return bool(find_nodes())


def udev_hint(ident):
    vid, pid = (ident.split(":") + ["", ""])[:2]
    return (f'KERNEL=="hidraw*", ATTRS{{idVendor}}=="{vid}", ATTRS{{idProduct}}=="{pid}", '
            f'TAG+="uaccess"')


def _hsv255(rgb):
    h, s, v = colorsys.rgb_to_hsv(*(c / 255.0 for c in rgb))
    return int(h * 255), int(s * 255), int(v * 255)


class Keyboard:
    def __init__(self, node=None, fd=None):
        if fd is None:
            found = find_nodes()
            if node:
                found = [f for f in found if f[0] == node]
            if not found:
                raise DeviceError("no QMK/VIA keyboard (raw-HID usage 0xFF60/0x61)")
            node, self.name, self.ident = found[0]
            try:
                fd = os.open(node, os.O_RDWR)
            except PermissionError as e:
                raise DeviceError(f"cannot open {node} ({self.name}): {e}. Add a udev rule: "
                                  f"{udev_hint(self.ident)}") from e
        else:
            self.name, self.ident = "QMK/VIA keyboard", ""
        self.fd = fd
        self.node = node or "fd"
        self.version = self._handshake()
        if self.version < MIN_PROTOCOL:
            os.close(self.fd)
            raise DeviceError(f"{self.name}: VIA protocol {self.version:#06x} is older than "
                              f"{MIN_PROTOCOL:#06x} (VIA v3, QMK 0.18+); update the firmware")
        self.channel = self._find_channel()
        if self.channel is None:
            os.close(self.fd)
            raise DeviceError(f"{self.name}: no lighting channel answers (no RGB matrix, "
                              f"RGB light or backlight in this firmware)")
        white = self.channel == CH_BACKLIGHT
        self.caps = caps.Caps(colour=caps.COLOUR_NONE if white else caps.COLOUR_ONE,
                              brightness=True, streams=True,
                              modes=frozenset({"static"}), zones=1)
        self.name = f"{self.name} (VIA)"
        self._last_write = 0.0
        self._pulse = BrightnessPulse(self._paint_level, interval=WRITE_INTERVAL)
        self._hs = (0, 0)
        self._top = 1.0

    # -- transport ------------------------------------------------------

    def _xfer(self, cmd, payload=b"", timeout=1.0):
        pkt = bytes([cmd]) + bytes(payload)
        pkt = pkt + bytes(REPORT - len(pkt))
        os.write(self.fd, b"\x00" + pkt)            # report id 0: the interface has none
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise OSError(f"VIA: no reply to command {cmd:#04x}")
            r, _, _ = select.select([self.fd], [], [], left)
            if not r:
                continue
            rep = os.read(self.fd, 64)
            if rep and rep[0] == cmd:
                return rep
            if rep and rep[0] == CMD_UNHANDLED:
                return rep

    def _handshake(self):
        rep = self._xfer(CMD_VERSION)
        if rep[0] != CMD_VERSION:
            raise DeviceError(f"{self.name}: not a VIA keyboard (bad version reply)")
        return (rep[1] << 8) | rep[2]

    def _find_channel(self):
        for ch in (CH_RGB_MATRIX, CH_RGBLIGHT, CH_BACKLIGHT):
            try:
                rep = self._xfer(CMD_GET, bytes([ch, V_BRIGHTNESS]))
            except OSError:
                continue
            if rep[0] == CMD_GET:
                return ch
        return None

    def _set(self, value_id, *data):
        self._xfer(CMD_SET, bytes([self.channel, value_id, *data]))

    # -- painting ---------------------------------------------------------

    def _paint(self, rgb, level01):
        h, s, v = _hsv255(rgb)
        if self.caps.has_colour and (h, s) != self._hs:
            self._set(V_COLOR, h, s)
            self._hs = (h, s)
        self._set(V_BRIGHTNESS, max(0, min(255, int(round(level01 * 255)))))
        self._last_write = time.monotonic()

    def _paint_level(self, level01):
        self._set(V_BRIGHTNESS, max(0, min(255, int(round(level01 * 255)))))

    # -- the board surface ------------------------------------------------

    def set(self, zone_mask, mode, keyframes, cycle_cs=100, wave_dir=None):
        self._pulse.stop()
        if mode == MODE_OFF:
            self._set(V_EFFECT, EFFECT_OFF)
            return
        peak = max(keyframes, key=lambda f: sum(f[1:4]))[1:4] if keyframes else (255, 255, 255)
        rgb = tuple(int(c) for c in peak)
        self._set(V_EFFECT, EFFECT_SOLID)
        self._top = _hsv255(rgb)[2] / 255.0 if self.caps.has_colour else 1.0
        self._paint(rgb, self._top)
        if mode == MODE_BREATHING:
            # The firmware's own breathing exists but its index differs per build.
            self._pulse.start(top=self._top)

    def solid(self, zone_mask, rgb):
        self.set(zone_mask, MODE_STATIC, [(0, *rgb)])

    def off(self, zone_mask=None):
        self.set(zone_mask or 15, MODE_OFF, [(0, 0, 0, 0)])

    def begin_software(self, name, colors):
        self._pulse.stop()
        self._set(V_EFFECT, EFFECT_SOLID)

    def frame(self, colors):
        """One colour for the board: the brightest zone's colour, 10 times a second."""
        now = time.monotonic()
        if now - self._last_write < WRITE_INTERVAL:
            return
        cols = [tuple(int(c) for c in rgb) for rgb in colors] or [(0, 0, 0)]
        rgb = max(cols, key=sum)
        self._paint(rgb, _hsv255(rgb)[2] / 255.0)

    def poll(self):
        return False

    def close(self):
        self._pulse.stop()
        try:
            os.close(self.fd)
        except OSError:
            pass
