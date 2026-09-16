"""OpenRGB SDK bridge: every keyboard OpenRGB knows, as LightShow boards.

OpenRGB (openrgb.org) has drivers for hundreds of RGB devices and offers them
over a small TCP protocol on 127.0.0.1:6742. When the `openrgb` package is
installed, LightShow starts its server headless if none is running, asks it
for every device, keeps the keyboards, and drives each through whatever it
reports it can do:

  a mode with per-LED colour ("Direct" preferred: no flash writes)  -> streamed
      frames, LightShow's four zones spread over the device's LEDs
  fixed firmware modes only (Static, Breathing, Spectrum Cycle, Wave ...)
      -> the closest one, with the theme's colours where the mode takes any
  no colour at all (every mode colourless, brightness flag only)  -> brightness

Protocol version 3 is negotiated on purpose: it carries everything needed
(modes with flags, colour mode, brightness, zones, LED counts) and none of the
later additions, so the parser below is small and exact. Facts from
NetworkProtocol.h, RGBControllerInterface.h and RGBController.cpp (2026-09).
Nothing is ever saved to a device (SAVEMODE is never sent).

A device a native driver already owns (its location names a hidraw node
LightShow holds) is skipped, so nothing is driven twice.
"""

import os
import shutil
import socket
import struct
import subprocess
import sys
import time

from . import caps
from .kbd import DeviceError, MODE_OFF, MODE_STATIC, MODE_BREATHING, MODE_CYCLE, MODE_WAVE

HOST = os.environ.get("LIGHTSHOW_OPENRGB_HOST", "127.0.0.1")
PORT = int(os.environ.get("LIGHTSHOW_OPENRGB_PORT", "6742"))
PROTOCOL = 3
MAGIC = b"ORGB"
HEADER = struct.Struct("<4sIII")

# packet ids (NetworkProtocol.h)
REQUEST_CONTROLLER_COUNT = 0
REQUEST_CONTROLLER_DATA = 1
DEVICE_LIST_UPDATED = 100
REQUEST_PROTOCOL_VERSION = 40
SET_CLIENT_NAME = 50
RGBCONTROLLER_UPDATELEDS = 1050
RGBCONTROLLER_UPDATEMODE = 1101

# mode flags (RGBControllerInterface.h)
HAS_SPEED, HAS_BRIGHTNESS, HAS_PER_LED_COLOR, HAS_MODE_SPECIFIC_COLOR = 1 << 0, 1 << 4, 1 << 5, 1 << 6
COLORS_NONE, COLORS_PER_LED, COLORS_MODE_SPECIFIC, COLORS_RANDOM = 0, 1, 2, 3
DEVICE_KEYBOARD, DEVICE_KEYPAD, DEVICE_LAPTOP = 5, 18, 19
KEYBOARD_TYPES = {DEVICE_KEYBOARD, DEVICE_KEYPAD, DEVICE_LAPTOP}

FRAME_INTERVAL = 0.05          # 20 fps; some backends throttle around 30
SERVER_START_WAIT = 20.0       # detection probes i2c and USB; can take a while
EXCLUDE_NODES = set()          # hidraw nodes native drivers hold, set by devices.py

EFFECT_NAMES = {               # LightShow effect -> substrings of OpenRGB mode names
    "static": ("static", "direct", "custom"),
    "breathe": ("breath",),
    "cycle": ("spectrum", "cycle", "rainbow"),
    "wave": ("wave",),
    "off": ("off",),
}


def _port_open():
    try:
        with socket.create_connection((HOST, PORT), timeout=0.5):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------- wire

class _Reader:
    def __init__(self, data):
        self.b, self.i = data, 0

    def take(self, fmt):
        st = struct.Struct(fmt)
        v = st.unpack_from(self.b, self.i)
        self.i += st.size
        return v[0] if len(v) == 1 else v

    def string(self, fmt="<H"):
        n = self.take(fmt)
        s = self.b[self.i:self.i + n]
        self.i += n
        return s.rstrip(b"\x00").decode("utf-8", "replace")


def _str(s):
    b = s.encode() + b"\x00"
    return struct.pack("<H", len(b)) + b


def _color(rgb):
    r, g, b = (max(0, min(255, int(c))) for c in rgb)
    return struct.pack("<BBBB", r, g, b, 0)


def parse_mode(rd, version=PROTOCOL):
    m = {"name": rd.string()}
    if version < 6:
        m["value"] = rd.take("<i")
    m["flags"] = rd.take("<I")
    m["speed_min"], m["speed_max"] = rd.take("<I"), rd.take("<I")
    if version >= 3:
        m["brightness_min"], m["brightness_max"] = rd.take("<I"), rd.take("<I")
    m["colors_min"], m["colors_max"] = rd.take("<I"), rd.take("<I")
    m["speed"] = rd.take("<I")
    if version >= 3:
        m["brightness"] = rd.take("<I")
    m["direction"], m["color_mode"] = rd.take("<I"), rd.take("<I")
    n = rd.take("<H")
    m["colors"] = [rd.take("<I") for _ in range(n)]
    return m


def pack_mode(m, version=PROTOCOL):
    out = _str(m["name"])
    if version < 6:
        out += struct.pack("<i", m.get("value", 0))
    out += struct.pack("<III", m["flags"], m["speed_min"], m["speed_max"])
    if version >= 3:
        out += struct.pack("<II", m.get("brightness_min", 0), m.get("brightness_max", 0))
    out += struct.pack("<III", m["colors_min"], m["colors_max"], m["speed"])
    if version >= 3:
        out += struct.pack("<I", m.get("brightness", 0))
    out += struct.pack("<II", m["direction"], m["color_mode"])
    out += struct.pack("<H", len(m["colors"]))
    for c in m["colors"]:
        out += struct.pack("<I", c)
    return out


def parse_device(data, version=PROTOCOL):
    rd = _Reader(data)
    d = {"type": rd.take("<i"), "name": rd.string()}
    if version >= 1:
        d["vendor"] = rd.string()
    d["description"], d["version"], d["serial"], d["location"] = (rd.string(), rd.string(),
                                                                   rd.string(), rd.string())
    n_modes = rd.take("<H")
    d["active_mode"] = rd.take("<i")
    d["modes"] = [parse_mode(rd, version) for _ in range(n_modes)]
    n_zones = rd.take("<H")
    zones = []
    for _ in range(n_zones):
        z = {"name": rd.string(), "type": rd.take("<I"), "leds_min": rd.take("<I"),
             "leds_max": rd.take("<I"), "leds_count": rd.take("<I")}
        msize = rd.take("<H")
        if msize:
            h, w = rd.take("<I"), rd.take("<I")
            for _ in range(h * w):
                rd.take("<I")
        zones.append(z)
    d["zones"] = zones
    n_leds = rd.take("<H")
    leds = []
    for _ in range(n_leds):
        name = rd.string()
        if version < 6:
            rd.take("<I")
        leds.append(name)
    d["leds"] = leds
    n_colors = rd.take("<H")
    d["colors"] = [rd.take("<I") for _ in range(n_colors)]
    return d


class Client:
    """The few SDK calls LightShow needs, over one TCP connection."""

    def __init__(self, host=None, port=None, timeout=3.0):
        # Resolved at call time so tests and LIGHTSHOW_OPENRGB_* overrides apply.
        self.sock = socket.create_connection((host or HOST, port or PORT), timeout=timeout)
        self.sock.settimeout(timeout)
        self.list_dirty = False
        self.version = self._negotiate()
        self._send(SET_CLIENT_NAME, 0, b"LightShow\x00")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _send(self, pkt_id, dev_id, payload=b""):
        self.sock.sendall(HEADER.pack(MAGIC, dev_id, pkt_id, len(payload)) + payload)

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise OSError("OpenRGB server closed the connection")
            buf += chunk
        return buf

    def _read(self, want_id):
        while True:
            magic, dev_id, pkt_id, size = HEADER.unpack(self._recv(HEADER.size))
            if magic != MAGIC:
                raise OSError("OpenRGB: bad packet magic")
            payload = self._recv(size) if size else b""
            if pkt_id == DEVICE_LIST_UPDATED:
                self.list_dirty = True      # unsolicited: keep waiting for our reply
                continue
            if pkt_id == want_id:
                return dev_id, payload
            # anything else: not ours, keep reading

    def _negotiate(self):
        self._send(REQUEST_PROTOCOL_VERSION, 0, struct.pack("<I", PROTOCOL))
        try:
            _, payload = self._read(REQUEST_PROTOCOL_VERSION)
        except (socket.timeout, OSError):
            return 0
        server = struct.unpack("<I", payload)[0]
        return min(PROTOCOL, server)

    def count(self):
        self._send(REQUEST_CONTROLLER_COUNT, 0)
        _, payload = self._read(REQUEST_CONTROLLER_COUNT)
        return struct.unpack("<I", payload[:4])[0]

    def device(self, idx):
        payload = struct.pack("<I", self.version) if self.version > 0 else b""
        self._send(REQUEST_CONTROLLER_DATA, idx, payload)
        _, data = self._read(REQUEST_CONTROLLER_DATA)
        d = parse_device(data, self.version)
        d["index"] = idx
        return d

    def update_leds(self, idx, colors):
        body = struct.pack("<H", len(colors)) + b"".join(_color(c) for c in colors)
        self._send(RGBCONTROLLER_UPDATELEDS, idx, struct.pack("<I", 4 + len(body)) + body)

    def update_mode(self, idx, mode_idx, mode):
        body = struct.pack("<i", mode_idx) + pack_mode(mode, self.version)
        self._send(RGBCONTROLLER_UPDATEMODE, idx, struct.pack("<I", 4 + len(body)) + body)

    def poll_dirty(self):
        """Drain unsolicited packets without blocking; True if the list changed."""
        self.sock.settimeout(0.0)
        try:
            while True:
                head = self.sock.recv(HEADER.size, socket.MSG_PEEK)
                if len(head) < HEADER.size:
                    break
                _, _, pkt_id, size = HEADER.unpack(head)
                self._recv(HEADER.size + size)
                if pkt_id == DEVICE_LIST_UPDATED:
                    self.list_dirty = True
        except (BlockingIOError, socket.timeout):
            pass
        finally:
            self.sock.settimeout(3.0)
        dirty, self.list_dirty = self.list_dirty, False
        return dirty


# ---------------------------------------------------------------- devices

EFFECT_EXCLUDE = {"cycle": ("wave",)}    # "Rainbow Wave" is a wave, not a colour cycle


def _find_mode(modes, effect):
    for i, m in enumerate(modes):
        low = m["name"].lower()
        if any(s in low for s in EFFECT_EXCLUDE.get(effect, ())):
            continue
        if any(s in low for s in EFFECT_NAMES.get(effect, ())):
            return i
    return None


def _to_rgb(c):
    return (c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF)


class Device:
    """One OpenRGB keyboard, its capabilities read from its mode list."""

    def __init__(self, client, d):
        self.client, self.d = client, d
        self.index = d["index"]
        self.name = d["name"].strip() or f"OpenRGB device {self.index}"
        self.node = d.get("location") or f"openrgb:{self.index}"
        modes = d["modes"]
        self.n_leds = len(d["leds"]) or sum(z["leds_count"] for z in d["zones"])
        # the mode to stream in: "Direct" first (no flash writes), else any per-LED mode
        self.direct = next((i for i, m in enumerate(modes)
                            if "direct" in m["name"].lower() and m["flags"] & HAS_PER_LED_COLOR), None)
        if self.direct is None:
            self.direct = next((i for i, m in enumerate(modes) if m["color_mode"] == COLORS_PER_LED
                                or m["flags"] & HAS_PER_LED_COLOR), None)
        colourful = any(m["color_mode"] in (COLORS_PER_LED, COLORS_MODE_SPECIFIC)
                        or m["flags"] & (HAS_PER_LED_COLOR | HAS_MODE_SPECIFIC_COLOR) for m in modes)
        self.brightness_mode = any(m["flags"] & HAS_BRIGHTNESS for m in modes)
        if self.direct is not None and self.n_leds:
            colour = caps.COLOUR_PER_KEY if self.n_leds > 8 else caps.COLOUR_ZONES
        elif colourful:
            colour = caps.COLOUR_ONE
        else:
            colour = caps.COLOUR_NONE
        fw = {e for e in ("static", "breathe", "cycle", "wave") if _find_mode(modes, e) is not None}
        self.caps = caps.Caps(colour=colour, brightness=True, streams=self.direct is not None,
                              modes=frozenset(fw), zones=4 if self.direct is not None else 1)
        self._last_frame = 0.0
        self._mode_set = None

    # -- helpers --------------------------------------------------------

    def _apply_mode(self, mode_idx, colors=(), brightness01=1.0):
        m = dict(self.d["modes"][mode_idx])
        m = dict(m, colors=list(m["colors"]))
        if m["color_mode"] == COLORS_MODE_SPECIFIC or m["flags"] & HAS_MODE_SPECIFIC_COLOR:
            want = max(m["colors_min"], min(m["colors_max"] or len(colors) or 1, len(colors) or 1))
            cols = [colors[i % len(colors)] if colors else (0, 0, 0) for i in range(want)]
            m["colors"] = [r | g << 8 | b << 16 for r, g, b in cols]
            if m["color_mode"] != COLORS_PER_LED:
                m["color_mode"] = COLORS_MODE_SPECIFIC
        if m["flags"] & HAS_BRIGHTNESS:
            lo, hi = m.get("brightness_min", 0), m.get("brightness_max", 100) or 100
            m["brightness"] = int(round(lo + (hi - lo) * max(0.0, min(1.0, brightness01))))
        if m["flags"] & HAS_SPEED and m["speed_max"] > m["speed_min"]:
            m["speed"] = (m["speed_min"] + m["speed_max"]) // 2
        self.client.update_mode(self.index, mode_idx, m)
        self._mode_set = mode_idx

    def _luma01(self, rgb):
        r, g, b = rgb
        return max(0.15, (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0)

    # -- the board surface ------------------------------------------------

    def set(self, zone_mask, mode, keyframes, cycle_cs=100, wave_dir=None):
        modes = self.d["modes"]
        peak = max(keyframes, key=lambda f: sum(f[1:4]))[1:4] if keyframes else (0, 0, 0)
        rgb = tuple(int(c) for c in peak)
        palette = []
        for f in keyframes or ():
            c = tuple(int(v) for v in f[1:4])
            if sum(c) * 5 >= sum(rgb) and c not in palette:
                palette.append(c)
        palette = palette or [rgb]
        if mode == MODE_OFF:
            i = _find_mode(modes, "off")
            if i is not None:
                self._apply_mode(i)
            elif self.direct is not None:
                self._apply_mode(self.direct)
                self.client.update_leds(self.index, [(0, 0, 0)] * self.n_leds)
            else:
                self._apply_mode(_find_mode(modes, "static") or 0, [(0, 0, 0)], 0.0)
            return
        want = {MODE_STATIC: "static", MODE_BREATHING: "breathe",
                MODE_CYCLE: "cycle", MODE_WAVE: "wave"}.get(mode, "static")
        i = _find_mode(modes, want)
        if i is None and want != "static":
            i = _find_mode(modes, "static")
        if i is None:
            i = self.direct if self.direct is not None else 0
        level = self._luma01(rgb) if not self.caps.has_colour else 1.0
        self._apply_mode(i, palette, level)
        if i == self.direct:
            self.client.update_leds(self.index, self._spread(palette))

    def _spread(self, cols):
        """LightShow's four zones (or a palette) over this device's LEDs."""
        n = max(1, self.n_leds)
        return [cols[int(k * len(cols) / n) % len(cols)] for k in range(n)]

    def solid(self, zone_mask, rgb):
        self.set(zone_mask, MODE_STATIC, [(0, *rgb)])

    def off(self, zone_mask=None):
        self.set(zone_mask or 15, MODE_OFF, [(0, 0, 0, 0)])

    def begin_software(self, name, colors):
        if self.direct is not None:
            self._apply_mode(self.direct)

    def frame(self, colors):
        if self.direct is None:
            return
        now = time.monotonic()
        if now - self._last_frame < FRAME_INTERVAL:
            return
        self._last_frame = now
        cols = [tuple(int(c) for c in rgb) for rgb in colors] or [(0, 0, 0)]
        self.client.update_leds(self.index, self._spread(cols))


_server_proc = None            # the `openrgb --server` LightShow started, if any
_gave_up = False               # OpenRGB answered and had no keyboard: stop looking


def present():
    if _gave_up:
        return False
    return shutil.which("openrgb") is not None or _port_open()


def _stop_server():
    global _server_proc
    if _server_proc is not None and _server_proc.poll() is None:
        _server_proc.terminate()
    _server_proc = None


def _ensure_server():
    """Start a headless OpenRGB server in the background if none answers.
    Returns at once; the caller retries the connection later (its detection
    probes USB and i2c, which takes seconds)."""
    global _server_proc
    if _server_proc is not None and _server_proc.poll() is None:
        return
    exe = shutil.which("openrgb")
    if not exe:
        raise DeviceError("openrgb is not installed (pacman -S openrgb)")
    _server_proc = subprocess.Popen(
        [exe, "--server", "--server-host", HOST, "--server-port", str(PORT), "--noautoconnect"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    print(f"lightshow: started openrgb --server on {HOST}:{PORT}, connecting shortly",
          file=sys.stderr, flush=True)


class Bridge:
    """One LightShow board that fans out to every keyboard OpenRGB offers."""

    def __init__(self):
        self.name = "OpenRGB"
        self.node = f"openrgb://{HOST}:{PORT}"
        if not _port_open():
            _ensure_server()
            raise DeviceError("OpenRGB server starting; connecting on the next look")
        try:
            self.client = Client()
        except OSError as e:
            raise DeviceError(f"OpenRGB server not reachable at {HOST}:{PORT}: {e}") from e
        self.devices = []
        self._enumerate()
        if not self.devices:
            # Nothing to drive: do not keep a server alive for it, and do not
            # keep asking. A keyboard plugged in later is found at the next start.
            global _gave_up
            _gave_up = True
            self.client.close()
            _stop_server()
            raise DeviceError("OpenRGB knows no keyboard on this machine; not retrying")

    def _enumerate(self):
        found = []
        for idx in range(self.client.count()):
            try:
                d = self.client.device(idx)
            except (OSError, struct.error) as e:
                print(f"lightshow: OpenRGB device {idx}: cannot read description: {e}",
                      file=sys.stderr, flush=True)
                continue
            if d["type"] not in KEYBOARD_TYPES:
                continue
            loc = (d.get("location") or "")
            if any(n and n in loc for n in EXCLUDE_NODES):
                print(f"lightshow: OpenRGB {d['name']!r} is driven natively; skipped",
                      file=sys.stderr, flush=True)
                continue
            found.append(Device(self.client, d))
        self.devices = found
        for dv in found:
            print(f"lightshow: OpenRGB keyboard {dv.name!r}: {dv.caps.describe()}",
                  file=sys.stderr, flush=True)

    # -- fan-out ----------------------------------------------------------

    def _each(self, method, *args):
        for dv in list(self.devices):
            try:
                getattr(dv, method)(*args)
            except (OSError, struct.error) as e:
                print(f"lightshow: OpenRGB {dv.name!r} {method} failed: {e}",
                      file=sys.stderr, flush=True)

    def set(self, *a, **k):
        self._each("set", *a)

    def solid(self, *a):
        self._each("solid", *a)

    def off(self, *a):
        self._each("off", *a)

    def begin_software(self, name, colors):
        self._each("begin_software", name, colors)

    def frame(self, colors):
        self._each("frame", colors)

    def poll(self):
        try:
            if self.client.poll_dirty():
                self._enumerate()
                return True
        except OSError:
            pass
        return False

    def boards_info(self):
        return [{"name": dv.name, "node": dv.node, "caps": dv.caps.as_dict(),
                 "notice": dv.caps.notice(dv.name)} for dv in self.devices]

    def fidelity(self, effect, software):
        return {dv.name: dv.caps.fidelity(effect, software) for dv in self.devices}

    def close(self):
        self.client.close()
        _stop_server()                     # only the server LightShow itself started
