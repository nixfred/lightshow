"""Low-level driver for the MSI MysticLight keyboard LED controller.

Hardware: MSI MysticLight MS-1801 (USB 0db0:1801), MSI Crosshair 16 Max HX.
OpenRGB has no entry for this VID:PID, so we speak the protocol directly.

Protocol (ported from OpenRGB's MSIKeyboard1565Controller, verified on this
hardware 2026-09-11): two 64-byte HID feature reports per update, sent with the
HIDIOCSFEATURE ioctl.

    1. zone select  [0x02][0x01][zone_bitmask][0 x61]
    2. mode/colour  [0x02][0x02][mode][speed_lo][speed_hi][0][0][0x0F][0x01]
                    [wave_dir][ up to 10 x (time_frame, R, G, B) ][pad]

time_frame is 0-100 (percent through the animation cycle); speed is the cycle
duration in hundredths of a second, little endian. The controller interpolates
between keyframes on its own microcontroller, so the hardware modes animate
forever at zero CPU once set.
"""

import fcntl
import glob
import os

# MSI ships these controllers under two vendor IDs. Rather than chase product
# IDs model by model, match the HID product string: every controller in this
# family reports itself as "MysticLight", which covers models nobody has
# catalogued yet. The PID list below is only used to rank candidates and to
# report what was found.
VENDORS = ("0DB0", "1462")
PRODUCT_MARKER = "MYSTICLIGHT"

# Known members of this protocol family. VERIFIED means tested on real
# hardware; REPORTED means documented working elsewhere with this same packet
# format but not tested here.
KNOWN = {
    "0DB0:1801": ("MSI Crosshair 16 Max HX (MS-1801)", "VERIFIED"),
    "0DB0:1606": ("MSI Cyborg 15 (MS-1606)", "REPORTED"),
    "0DB0:1605": ("MSI Vector A18 HX (MS-1605)", "REPORTED"),
    "1462:1601": ("MSI Katana 15 HX (MS-1565)", "REPORTED"),
    "1462:1562": ("MSI Delta 15 (MS-1562)", "REPORTED"),
    "1462:1563": ("MSI Alpha 15/17 (MS-1563)", "REPORTED"),
    "1462:1564": ("MSI (MS-1564)", "REPORTED"),
}

# Kept for the udev rule and for status output.
VID, PID = "0DB0", "1801"
HIDIOCSFEATURE_64 = 0xC0404806

# Zone bitmask bits, in rough left-to-right physical order.
# These are key GROUPS, not columns, and not individual keys.
Z_WASD, Z_ALPHA, Z_NAV, Z_NUMPAD = 1, 2, 4, 8
ZONE_ALL = 15
ZONES = [
    ("wasd", Z_WASD, "WASD cluster"),
    ("alpha", Z_ALPHA, "Main letter block"),
    ("nav", Z_NAV, "Nav / arrows"),
    ("numpad", Z_NUMPAD, "Numpad"),
]
ZONE_MASKS = [z[1] for z in ZONES]

MODE_OFF, MODE_STATIC, MODE_BREATHING, MODE_CYCLE, MODE_WAVE = 0, 1, 2, 3, 4
WAVE_RTL, WAVE_LTR = 0, 1

MAX_KEYFRAMES = 10


class DeviceError(RuntimeError):
    pass


def scan_devices():
    """Every MysticLight-family controller on this machine.

    Returns [(node, "VID:PID", hid_name)]. Matches on the product string first
    so unlisted models still work, and falls back to the vendor + known-PID
    pair in case a controller ever reports a different name.
    """
    found = []
    for path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            uevent = open(os.path.join(path, "device", "uevent")).read()
        except OSError:
            continue
        up = uevent.upper()

        ident, name = "", ""
        for line in uevent.splitlines():
            if line.startswith("HID_ID="):
                bits = line.split("=", 1)[1].split(":")
                if len(bits) == 3:
                    ident = f"{bits[1][-4:].upper()}:{bits[2][-4:].upper()}"
            elif line.startswith("HID_NAME="):
                name = line.split("=", 1)[1].strip()

        vendor_ok = any(v in up for v in VENDORS)
        if not (PRODUCT_MARKER in up.replace(" ", "") and vendor_ok):
            if ident not in KNOWN:
                continue

        node = "/dev/" + os.path.basename(path)
        if os.path.exists(node):
            found.append((node, ident, name))
    return found


def find_device():
    """Pick a controller to drive.

    LIGHTSHOW_DEVICE overrides everything, so an unsupported or oddly named
    controller can still be driven by pointing straight at its hidraw node.
    """
    override = os.environ.get("LIGHTSHOW_DEVICE")
    if override:
        return override if os.path.exists(override) else None
    devs = scan_devices()
    if not devs:
        return None
    # Prefer a device we have actually verified, then anything catalogued.
    devs.sort(key=lambda d: (KNOWN.get(d[1], ("", "UNKNOWN"))[1] != "VERIFIED",
                             d[1] not in KNOWN))
    return devs[0][0]


def describe_device(node=None):
    """Human-readable line about whatever we are driving."""
    for n, ident, name in scan_devices():
        if node is None or n == node:
            model, status = KNOWN.get(ident, (name or "unknown model", "UNTESTED"))
            return f"{n}  {ident}  {model}  [{status}]"
    if node:
        return f"{node}  (forced via LIGHTSHOW_DEVICE, identity unknown)"
    return "no MysticLight controller found"


class Keyboard:
    """One open handle to the LED controller."""

    def __init__(self, node=None):
        node = node or find_device()
        if not node:
            raise DeviceError(f"no MysticLight controller found ({VID}:{PID})")
        try:
            self.fd = os.open(node, os.O_RDWR)
        except PermissionError as e:
            raise DeviceError(
                f"cannot open {node}: {e}. Install "
                "/etc/udev/rules.d/99-msi-mysticlight.rules"
            ) from e
        self.node = node
        self._zone = None

    # -- raw ------------------------------------------------------------

    def _feature(self, data: bytes):
        fcntl.ioctl(self.fd, HIDIOCSFEATURE_64, data.ljust(64, b"\x00"))

    def select(self, zone_mask: int):
        # The controller remembers the selection; skip redundant writes.
        if self._zone != zone_mask:
            self._feature(bytes([0x02, 0x01, zone_mask]))
            self._zone = zone_mask

    def set(self, zone_mask, mode, keyframes, cycle_cs=100, wave_dir=WAVE_LTR):
        """keyframes: list of (time_frame 0-100, r, g, b)."""
        self.select(zone_mask)
        p = bytearray(64)
        p[0], p[1], p[2] = 0x02, 0x02, mode
        p[3] = cycle_cs & 0xFF
        p[4] = (cycle_cs >> 8) & 0xFF
        p[7], p[8] = 0x0F, 0x01  # constants observed in the OEM traffic
        p[9] = wave_dir

        frames = keyframes[: MAX_KEYFRAMES - 1]
        for i, (t, r, g, b) in enumerate(frames):
            p[10 + i * 4 : 14 + i * 4] = bytes(
                [int(t) & 0xFF, int(r) & 0xFF, int(g) & 0xFF, int(b) & 0xFF]
            )
        # Close the loop so the cycle wraps smoothly to the first colour.
        if frames:
            i = len(frames)
            f0 = frames[0]
            p[10 + i * 4 : 14 + i * 4] = bytes(
                [100, int(f0[1]) & 0xFF, int(f0[2]) & 0xFF, int(f0[3]) & 0xFF]
            )
        self._feature(bytes(p))

    # -- convenience ----------------------------------------------------

    def solid(self, zone_mask, rgb):
        self.set(zone_mask, MODE_STATIC, [(0, *rgb)])

    def off(self, zone_mask=ZONE_ALL):
        """True off, not static black.

        Static black renders as "display the colour black" and leaves the
        keyboard's base illumination lit. MODE_OFF actually darkens it.
        """
        self.set(zone_mask, MODE_OFF, [(0, 0, 0, 0)])

    def frame(self, colors):
        """Paint one frame: colors is a list of 4 RGB tuples, zone order."""
        for rgb, mask in zip(colors, ZONE_MASKS):
            self.solid(mask, rgb)

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


# -- colour helpers -----------------------------------------------------


def hex_rgb(s):
    s = str(s).lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def rgb_hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(c))) for c in rgb)


def scale(rgb, f):
    return tuple(max(0, min(255, int(c * f))) for c in rgb)


def mix(a, b, f):
    """Linear blend; f=0 gives a, f=1 gives b."""
    return tuple(int(a[i] * (1 - f) + b[i] * f) for i in range(3))


def hsv_rgb(h, s, v):
    """h in [0,1). Cheap HSV so effects can sweep hue without a dependency."""
    i = int(h * 6) % 6
    f = h * 6 - int(h * 6)
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    r, g, b = [(v, t, p), (q, v, p), (p, v, t),
               (p, q, v), (t, p, v), (v, p, q)][i]
    return (int(r * 255), int(g * 255), int(b * 255))


# -- Omarchy theme ------------------------------------------------------

THEME_DIR = os.path.expanduser("~/.local/state/omarchy/current/theme")


def theme_name():
    try:
        import subprocess

        return subprocess.run(
            ["omarchy-theme-current"], capture_output=True, text=True, timeout=5
        ).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def load_palette():
    """Parse the active theme's colors.toml into {name: (r,g,b)}."""
    palette = {}
    try:
        for line in open(os.path.join(THEME_DIR, "colors.toml")):
            line = line.strip()
            if "=" not in line or line.startswith("#"):
                continue
            key, _, val = line.partition("=")
            val = val.strip().strip('"').strip("'")
            if val.startswith("#") and len(val) == 7:
                palette[key.strip()] = hex_rgb(val)
    except OSError:
        pass
    return palette


# Dark backgrounds are excluded on purpose: on a keyboard they read as "off"
# and make an animation look broken rather than subtle.
PREFERRED = [
    "accent", "blue", "magenta", "cyan", "green", "yellow", "orange", "red",
    "bright_blue", "bright_magenta", "bright_cyan",
    "bright_green", "bright_yellow", "bright_red",
]
FALLBACK = [(122, 162, 247)]  # Tokyo Night accent


def theme_colors():
    p = load_palette()
    out, seen = [], set()
    for name in PREFERRED:
        rgb = p.get(name)
        if rgb and rgb not in seen and sum(rgb) > 90:
            out.append(rgb)
            seen.add(rgb)
    return out or list(FALLBACK)


def accent():
    return theme_colors()[0]
