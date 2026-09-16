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

import contextlib
import errno
import fcntl
import glob
import json
import os
import select
import sys
import threading
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

# Only modes captured from Swarm writing this board. Wave (0x0a) was seen in the
# factory record but never written by Swarm; a LightShow wave write on
# 2026-09-14 was the last write before the board went dark, so wave falls back
# to static until it is captured.
KB7_MODE = {MODE_STATIC: 0x01, MODE_BREATHING: 0x07}
B7_CUSTOM = 0x0B

# Software-effect frames are throttled to one record write per this many
# seconds. Applying a look (static, breathe, wave, off) is never throttled.
FRAME_MIN_INTERVAL = 5.0

# Live effect stream on USB interface 1 (vendor page 0xFF00, 64-byte IN/OUT,
# no report IDs), captured from Swarm II on firmware 1.37 (2026-09-15):
#   one frame = 360 colour bytes; key position p (same p as the 0x11 record)
#   has R at 4+p, G at 4+p+16, B at 4+p+32
#   sent as 6 packets: a1 <seq 1..6> <len16 LE: 68 01 on seq 1, 00 00 after> <60 bytes>
#   the keyboard acks every packet with 32 <seq> on the IN endpoint
#   Swarm ran it at ~20 fps. It is not the persistent record: no flash wear.
STREAM_INTERFACE = 1
STREAM_BODY = 360
STREAM_OFFSET = 4
STREAM_CHUNK = 60
STREAM_MIN_INTERVAL = 0.05   # cap at 20 fps, what Swarm used
STREAM_ACK_TIMEOUT = 0.1     # seconds to wait for each 32 <seq> ack
# The first packet of a stream is acked slowly: Swarm's own a1 01 on fw 1.37
# took 230 ms (the board switching into live-lighting mode), every later ack
# about 2 ms. A stream counts as starting after STREAM_IDLE_RESTART of silence.
STREAM_FIRST_ACK_TIMEOUT = 1.0
STREAM_IDLE_RESTART = 1.0
STREAM_BACKOFF = 10.0        # stop streaming this long after a missed ack
# Shared with kb7ctl (turtle.beach.keyboard): its screen-image upload uses the
# same interface-1 node, so both sides take this flock around their writes.
STREAM_LOCK = os.path.join(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
                           "kb7-iface1.lock")
# Same idea for the control interface: kb7ctl's status board rewrites the screen
# labels every few seconds on the same node. Whoever talks to it holds this for
# one transaction (a GET; a SET plus its settle; a whole select+poll+read), never
# across an idle wait. Two processes can then never land a GET inside the other's
# settle window, which is exactly what wedged the fw 1.22 handler.
CONTROL_LOCK = os.path.join(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
                            "kb7-control.lock")
CONTROL_LOCK_WAIT = 5.0      # the other writer's longest transaction is ~1 s
# Backstop read of the active profile (GET 0x06) every this many seconds; the
# profile key's own report triggers it at once, so this only catches a missed
# press. 0 = never poll (events still work). Keep control traffic low: key
# repeats were seen 2026-09-15 while another tool wrote labels every 5 s.
PROFILE_BACKSTOP = float(os.environ.get("LIGHTSHOW_KB7_PROFILE_POLL", "20"))
TYPING_IDLE = 2.0            # no key reports for this long before a read or write
TYPING_WAIT_MAX = 10.0       # a write waits at most this long for silence, then goes

SETTLE_AFTER_SET = 0.3       # seconds between any SET and the next request
# After a replug, leave the fresh board alone this long before reopening it:
# a plug-time helper (kb7-plug.service writes the tile labels, ~4 s) must be
# done with the control node first. Only one process may talk to it.
HOTPLUG_GRACE = 10.0
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


def _instance(node):
    """The HID instance behind a hidraw node (e.g. 0003:10F5:5038.003B); it changes
    on every enumeration, so it tells a replugged board from the one we opened."""
    try:
        return os.path.basename(os.readlink(f"/sys/class/hidraw/{os.path.basename(node)}/device"))
    except OSError:
        return None


def present():
    """Is a KB7 control interface on the bus right now?"""
    return find_control_node() is not None


def find_stream_node():
    """hidraw node for the KB7's effect-stream interface (USB interface 1), or None."""
    want = f"HID_ID=0003:0000{VID}:0000{PID}"
    for uevent in sorted(glob.glob("/sys/class/hidraw/hidraw*/device/uevent")):
        try:
            txt = open(uevent).read()
        except OSError:
            continue
        if want not in txt.upper():
            continue
        phys = [l for l in txt.splitlines() if l.startswith("HID_PHYS=")]
        if phys and phys[0].endswith(f"/input{STREAM_INTERFACE}"):
            return "/dev/" + uevent.split("/")[4]
    return None


def stream_packets(body):
    """Split a 360-byte frame into Swarm's six 64-byte a1 packets."""
    assert len(body) == STREAM_BODY
    packets = []
    for i in range(STREAM_BODY // STREAM_CHUNK):
        seq = i + 1
        length = STREAM_BODY if seq == 1 else 0
        chunk = body[i * STREAM_CHUNK:(i + 1) * STREAM_CHUNK]
        # little-endian: Swarm's first packet starts a1 01 68 01 (0x0168 = 360)
        packets.append(bytes([0xA1, seq, length & 0xFF, length >> 8]) + chunk)
    return packets


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
        self._instance = _instance(node)
        self._gone = False         # a request hit ENODEV: the board was unplugged
        self._seen_new = None      # (instance, when) of a board waiting out HOTPLUG_GRACE
        self._io = threading.RLock()  # one transaction at a time in this process
        self._txn_depth = 0
        self._txn_owner = None        # thread ident holding the outermost transaction
        self._ev_lock = threading.Lock()
        self._pending = set()         # button events drained but not yet consumed
        self._ctl_lock_fd = None      # CONTROL_LOCK, opened on first use
        self._ctl_lock_warned = False
        self._reset_state()

    def _reset_state(self):
        self._template = None      # the board's own record, read once per profile
        self._profile = None
        self._last_written = None
        self._last_frame_write = 0.0
        self._last_frame_palette = None
        self._brightness_before_off = None
        self._last_set = 0.0
        self._backoff_until = 0.0
        self._stream_fd = None
        self._stream_node = None
        self._last_stream = 0.0
        self._stream_backoff_until = 0.0
        self._stream_announced = False
        self._direct = False          # 0E 05 01 active: the board accepts streamed frames
        self._yielded = False         # direct mode dropped while kb7ctl holds interface 1
        self._frames_without_ack = 0
        self._last_profile_check = 0.0
        self._last_key_report = 0.0   # any non-button report on the control node = typing

    # -- hot-plug ----------------------------------------------------------

    def _mark_gone(self):
        if not self._gone:
            self._gone = True
            print("kb7: board unplugged, waiting for it to come back", file=sys.stderr, flush=True)

    def poll_hotplug(self):
        """Called every few seconds. Reopens the board after a replug and returns
        True once, so the caller can re-apply the current look. The fresh board
        is left alone for HOTPLUG_GRACE first (see the constant)."""
        node = find_control_node()
        inst = _instance(node) if node else None
        if inst is None:
            self._mark_gone()
            self._seen_new = None
            return False
        if inst == self._instance and not self._gone:
            return False
        now = time.monotonic()
        if self._seen_new is None or self._seen_new[0] != inst:
            self._seen_new = (inst, now)
            return False
        if now - self._seen_new[1] < HOTPLUG_GRACE:
            return False
        for fd in (self.fd, self._stream_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.fd = self._stream_fd = None
        try:
            self.fd = os.open(node, os.O_RDWR)
        except OSError as e:
            print(f"kb7: back on {node} but cannot open it: {e}; retrying in {HOTPLUG_GRACE:.0f}s",
                  file=sys.stderr, flush=True)
            self._seen_new = (inst, now)
            return False
        self.node, self._instance, self._gone, self._seen_new = node, inst, False, None
        self._reset_state()
        print(f"kb7: back on {node}, reopened", file=sys.stderr, flush=True)
        return True

    def _drain_events(self):
        """Input reports queued on the control node, never blocking. The four
        top buttons report `03 00 02 <80..83> <01 down | 00 up>`; button 2
        (0x81) is the firmware profile switch and is followed by `03 00 31 ..`;
        button 4 (0x83) dims the lights in firmware. Decoded from counted
        presses on fw 1.37, 2026-09-15."""
        if self.fd is None or self._gone:
            return
        with self._ev_lock:
            self._drain_events_locked()

    def _take_events(self):
        with self._ev_lock:
            seen, self._pending = self._pending, set()
        return seen

    def _drain_events_locked(self):
        seen = self._pending
        for _ in range(64):
            try:
                ready, _, _ = select.select([self.fd], [], [], 0)
            except (OSError, ValueError):
                break
            if not ready:
                break
            try:
                rep = os.read(self.fd, 64)
            except BlockingIOError:
                break
            except OSError as e:
                if e.errno in (errno.ENODEV, errno.ENXIO):
                    self._mark_gone()
                break
            if len(rep) < 5 or rep[0] != 0x03 or rep[1] != 0x00:
                self._last_key_report = time.monotonic()   # keys, media, wheel: Fred is typing
                continue
            # Only the button's own press report. `03 00 31 ..` follows it too, but
            # the board sends 31 after ANY config write (label writes, our own
            # 0x11), so it is not a profile signal (peer finding, 2026-09-15 14:47).
            if rep[2] == 0x02 and rep[3] == 0x81 and rep[4] == 0x01:
                seen.add("profile")
            elif rep[2] == 0x02 and rep[3] == 0x83 and rep[4] == 0x01:
                seen.add("dim")

    def poll_profile(self):
        """True when the board's active profile is no longer the one the look
        was written to: the top-row profile key switched it, and the keys now
        show that profile's own lighting. The caller re-applies the look; the
        next write reads the new profile's record and lands there.

        Called every second. The profile key's own report triggers the check
        at once; otherwise the active profile is read every PROFILE_BACKSTOP
        seconds (0 = never) as a backstop.
        """
        if self._gone or self._profile is None or self._direct:
            return False
        self._drain_events()
        events = self._take_events()
        if "dim" in events:
            # Firmware changed the brightness: the cached record is stale, so the
            # next write re-reads the board's record instead of restoring the old level.
            self._template = None
        now = time.monotonic()
        if "profile" not in events:
            if PROFILE_BACKSTOP <= 0 or now - self._last_profile_check < PROFILE_BACKSTOP:
                return False
            if now - self._last_key_report < TYPING_IDLE:
                return False              # never poll under Fred's fingers; try next second
        if now < self._backoff_until:
            return False
        self._last_profile_check = now
        # After the profile key the firmware takes a moment: a single read 300 ms
        # after the press still showed the old profile twice (2026-09-15 14:26),
        # so keep reading every 300 ms for up to 3 s until it has moved.
        tries = 10 if "profile" in events else 1
        active = self._profile
        for _ in range(tries):
            if tries > 1:
                time.sleep(0.3)
            try:
                active = self._active_profile()
            except OSError:
                return False
            if active != self._profile:
                break
        if active == self._profile:
            return False
        print(f"kb7: active profile changed {self._profile} -> {active}, giving it the look",
              file=sys.stderr, flush=True)
        return True

    def poll(self):
        """Every few seconds from the engine: True when the look must be re-applied."""
        return self.poll_hotplug() or self.poll_profile()

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

    def _ctl_flock(self, on):
        """Cross-process half of the transaction lock; degrades to in-process
        only (with one warning) if the lock file cannot be used."""
        if self._ctl_lock_fd is None:
            try:
                self._ctl_lock_fd = os.open(CONTROL_LOCK, os.O_RDWR | os.O_CREAT, 0o644)
            except OSError as e:
                if not self._ctl_lock_warned:
                    self._ctl_lock_warned = True
                    print(f"kb7: no control lock {CONTROL_LOCK} ({e}); other writers are not fenced",
                          file=sys.stderr, flush=True)
                return
        if not on:
            fcntl.flock(self._ctl_lock_fd, fcntl.LOCK_UN)
            return
        end = time.monotonic() + CONTROL_LOCK_WAIT
        while True:
            try:
                fcntl.flock(self._ctl_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() > end:
                    raise OSError(f"KB7 control node busy: {CONTROL_LOCK} held for over "
                                  f"{CONTROL_LOCK_WAIT:.0f}s by another process")
                time.sleep(0.02)

    @contextlib.contextmanager
    def _transaction(self):
        """One control-node transaction: re-entrant within a thread, exclusive
        across threads and processes."""
        with self._io:
            if self._txn_depth == 0:
                self._ctl_flock(True)
                self._txn_owner = threading.get_ident()
            self._txn_depth += 1
            try:
                yield
            finally:
                self._txn_depth -= 1
                if self._txn_depth == 0:
                    self._txn_owner = None
                    self._ctl_flock(False)

    def _wait_typing_idle(self):
        """Block until the board has sent no key reports for TYPING_IDLE, or
        TYPING_WAIT_MAX has passed. Called before a transaction, never inside
        one: the lock is not held while waiting."""
        end = time.monotonic() + TYPING_WAIT_MAX
        while True:
            self._drain_events()
            idle = time.monotonic() - self._last_key_report
            if idle >= TYPING_IDLE or time.monotonic() >= end:
                return
            time.sleep(min(0.1, TYPING_IDLE - idle))

    def _get(self, rid, n):
        with self._transaction():
            return self._get_locked(rid, n)

    def _get_locked(self, rid, n):
        self._pace()
        buf = bytearray(n + 1)
        buf[0] = rid
        try:
            if self.fd is None:
                raise OSError(errno.ENODEV, "KB7 unplugged")
            fcntl.ioctl(self.fd, _ioc(3, "H", 0x07, n + 1), buf, True)
        except OSError as e:
            if e.errno in (errno.ENODEV, errno.ENXIO):
                self._mark_gone()
            self._backoff_until = time.monotonic() + BACKOFF_AFTER_FAILURE
            print(f"kb7: GET 0x{rid:02x} failed: {e} (backing off {BACKOFF_AFTER_FAILURE:.0f}s)",
                  file=sys.stderr, flush=True)
            raise
        return bytes(buf)

    def _set(self, data):
        # A write while Fred types delays a key release and the host repeats the
        # key (seen 2026-09-15 with another tool's writes); wait for silence first,
        # unless this thread is already inside a transaction (its caller waited).
        if self._txn_owner != threading.get_ident():
            self._wait_typing_idle()
        with self._transaction():
            self._set_locked(data)
            time.sleep(SETTLE_AFTER_SET)   # the settle is part of the transaction

    def _set_locked(self, data):
        self._pace()
        buf = bytearray(data)
        try:
            if self.fd is None:
                raise OSError(errno.ENODEV, "KB7 unplugged")
            fcntl.ioctl(self.fd, _ioc(3, "H", 0x06, len(buf)), buf, True)
        except OSError as e:
            if e.errno in (errno.ENODEV, errno.ENXIO):
                self._mark_gone()
            self._backoff_until = time.monotonic() + BACKOFF_AFTER_FAILURE
            print(f"kb7: SET 0x{buf[0]:02x} failed: {e} (backing off {BACKOFF_AFTER_FAILURE:.0f}s)",
                  file=sys.stderr, flush=True)
            raise
        finally:
            self._last_set = time.monotonic()

    # -- board state ----------------------------------------------------

    def _active_profile(self):
        return self._get(PROFILE_REPORT, 3)[2]

    def _read_light_record(self, profile):
        # One transaction: the select is board state another process could clobber.
        if self._txn_owner != threading.get_ident():
            self._wait_typing_idle()
        with self._transaction():
            return self._read_light_record_locked(profile)

    def _read_light_record_locked(self, profile):
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
        w = self.positions.get(47, 0) + LIGHT_HDR
        print(f"kb7: write 0x11 profile={profile} mode={mode_byte:#04x} "
              f"bright={out[6]} W=({out[w]},{out[w + 16]},{out[w + 32]})",
              file=sys.stderr, flush=True)
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
        # Breathe carries the theme palette as alternating dim/peak keyframes;
        # the firmware breathes the whole record, so give each zone its own peak
        # colour (dim troughs are 5% of a colour, far under a fifth of the peak).
        palette = []
        if mode == MODE_BREATHING and keyframes:
            for f in keyframes:
                c = tuple(int(v) for v in f[1:4])
                if sum(c) * 5 >= peak and c not in palette:
                    palette.append(c)
        masks = [m for m in ZONE_MASKS if zone_mask & m]
        # A hardware look owns the keys again: leave direct (streamed) mode first.
        self._yielded = False
        if self._direct:
            try:
                self._direct_mode(False)
            except OSError as e:
                print(f"kb7: could not leave direct mode: {e}", file=sys.stderr, flush=True)
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
        if len(palette) > 1:
            zone_colours = {m: palette[i % len(palette)] for i, m in enumerate(masks)}
        else:
            zone_colours = {m: rgb for m in masks}
        self._write(zone_colours, mode_byte,
                    brightness=self._restore_brightness())

    def solid(self, zone_mask, rgb):
        self.set(zone_mask, MODE_STATIC, [(0, *rgb)])

    def off(self, zone_mask=None):
        self.set(zone_mask or 15, MODE_OFF, [(0, 0, 0, 0)])

    def begin_software(self, name, colors):
        """A software effect is starting. The KB7 cannot animate through its
        persistent record, so it gets ONE static record for the whole effect:
        the effect's palette spread across the four zones at full colour, every
        zone lit. Nothing more is written until the look or theme changes.

        (2026-09-15: writing frames instead froze "gamer" on the KB7 with only
        WASD and arrows lit at a dim breathing level, 90 keys black, and cost
        a record write every 5 s.)
        """
        cols = [tuple(int(c) for c in rgb) for rgb in (colors or [(122, 162, 247)])]
        bright = [c for c in cols if sum(c) > 90] or cols
        zone_colours = {m: bright[i % len(bright)] for i, m in enumerate(ZONE_MASKS)}
        self._write(zone_colours, KB7_MODE[MODE_STATIC],
                    brightness=self._restore_brightness())
        # Then hand the keys to the live stream (OpenRGB's EnableDirect + WaitUntilReady).
        self._yielded = False
        if os.environ.get("LIGHTSHOW_KB7_STREAM", "1") != "0":
            self._direct_mode(True)

    def _direct_mode(self, on):
        """0E 05 01 = direct (host-streamed) lighting, 0E 05 00 = off.

        Captured working on fw 1.37 (2026-09-15): direct on, poll GET 0x05
        until byte 1 == 1, then stream; 1329 of 1362 packets acked. With it
        off the board takes the frames but never answers or shows them.
        On fw 1.22 direct mode also switched on the analog key-travel stream,
        which made a resting finger repeat a key, so it is only on while a
        software effect runs.
        """
        if on == self._direct:
            return
        self._set(bytes([0x0E, 0x05, 0x01 if on else 0x00, 0x00, 0x00]))
        self._direct = on
        print(f"kb7: direct mode {'ON' if on else 'OFF'}", file=sys.stderr, flush=True)
        if on:
            end = time.monotonic() + 5.0
            while time.monotonic() < end:
                time.sleep(SELECT_POLL)
                if self._get(SELECT_REPORT, 3)[1] == 0x01:
                    break
            self._frames_without_ack = 0
            self._stream_backoff_until = 0.0
            self._last_stream = 0.0

    def _open_stream(self):
        if self._stream_fd is not None:
            return True
        node = find_stream_node()
        if not node:
            return False
        try:
            self._stream_fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            print(f"kb7: cannot open stream node {node}: {e}", file=sys.stderr, flush=True)
            self._stream_backoff_until = time.monotonic() + STREAM_BACKOFF
            return False
        self._stream_node = node
        return True

    def _stream_drain(self):
        """Discard anything already waiting on the stream IN endpoint, so a late
        ack from an earlier timed-out packet cannot pass for the next one."""
        while True:
            try:
                if not os.read(self._stream_fd, 65):
                    return
            except (BlockingIOError, OSError):
                return

    def _stream_ack(self, seq, timeout=STREAM_ACK_TIMEOUT):
        """Wait for the keyboard's 32 <seq> ack. True if it arrived."""
        import select
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return False
            r, _, _ = select.select([self._stream_fd], [], [], left)
            if not r:
                return False
            try:
                data = os.read(self._stream_fd, 65)
            except BlockingIOError:
                continue
            if len(data) >= 2 and data[0] == 0x32 and data[1] == seq:
                return True
            # anything else (another report) is not our ack; keep waiting

    def frame(self, colors):
        """One software-effect frame, streamed live on interface 1.

        Never touches the persistent 0x11 record. Capped at 20 fps; a missing
        ack stops streaming for STREAM_BACKOFF seconds and the board keeps the
        still look begin_software() saved.
        """
        # LIGHTSHOW_KB7_STREAM=0 keeps interface 1 silent (e.g. while a screen
        # image upload uses the same pipe); the still look stays on the keys.
        if os.environ.get("LIGHTSHOW_KB7_STREAM", "1") == "0" or not (self._direct or self._yielded):
            return
        if self._gone:
            return
        now = time.monotonic()
        if now < self._stream_backoff_until or now - self._last_stream < STREAM_MIN_INTERVAL:
            return
        if now - self._last_set < SETTLE_AFTER_SET:   # never straight after a record write
            return
        if not self._open_stream():
            return
        body = bytearray(STREAM_BODY)
        for mask, rgb in zip(ZONE_MASKS, colors):
            r, g, b = (max(0, min(255, int(c))) for c in rgb)
            for key in self.zones.get(mask, ()):
                p = self.positions.get(key)
                if p is None:
                    continue
                base = STREAM_OFFSET + p
                body[base], body[base + 16], body[base + 32] = r, g, b
        if not self._stream_lock_take():
            return
        try:
            self._stream_frame(body, now)
        finally:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

    def _stream_lock_take(self):
        """Non-blocking per-frame flock; skip the frame while kb7ctl uploads."""
        try:
            if getattr(self, "_lock_fd", None) is None:
                self._lock_fd = os.open(STREAM_LOCK, os.O_RDWR | os.O_CREAT, 0o644)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not getattr(self, "_lock_waiting", False):
                self._lock_waiting = True
                print("kb7: interface 1 locked by another writer, pausing frames",
                      file=sys.stderr, flush=True)
            # After streaming, the board ignores kb7ctl's `a5 00` upload start
            # while direct mode stays on; Swarm always ends a stream with 0E 05 00.
            if self._direct:
                try:
                    self._direct_mode(False)
                    self._yielded = True
                except OSError as e:
                    print(f"kb7: could not leave direct mode for the other writer: {e}",
                          file=sys.stderr, flush=True)
            return False
        except OSError as e:
            print(f"kb7: stream lock {STREAM_LOCK} failed: {e} (backing off {STREAM_BACKOFF:.0f}s)",
                  file=sys.stderr, flush=True)
            self._stream_backoff_until = time.monotonic() + STREAM_BACKOFF
            return False
        if getattr(self, "_lock_waiting", False):
            self._lock_waiting = False
            print("kb7: interface 1 free again, resuming frames", file=sys.stderr, flush=True)
        if self._yielded:
            self._yielded = False
            try:
                self._direct_mode(True)
            except OSError as e:
                print(f"kb7: could not re-enter direct mode: {e}", file=sys.stderr, flush=True)
        return True

    def _stream_frame(self, body, now):
        starting = now - self._last_stream > STREAM_IDLE_RESTART
        self._last_stream = now
        self._stream_drain()
        acked = False
        for pkt in stream_packets(bytes(body)):
            try:
                os.write(self._stream_fd, b"\x00" + pkt)   # report ID 0: interface has none
            except OSError as e:
                print(f"kb7: stream write failed: {e} (backing off {STREAM_BACKOFF:.0f}s)",
                      file=sys.stderr, flush=True)
                self._stream_backoff_until = time.monotonic() + STREAM_BACKOFF
                return
            # Acks are counted, not required: OpenRGB's SendColors ignores them,
            # and the board acked ~98% in the working capture (the first after ~0.9 s).
            wait = STREAM_FIRST_ACK_TIMEOUT if (starting and pkt[1] == 1) else 0.02
            acked = self._stream_ack(pkt[1], wait)
            if acked:
                self._frames_without_ack = 0
        if not acked:
            self._frames_without_ack += 1
            if self._frames_without_ack >= int(3.0 / STREAM_MIN_INTERVAL):
                print(f"kb7: no stream acks for 3 s, the board is not taking frames "
                      f"(backing off {STREAM_BACKOFF:.0f}s)", file=sys.stderr, flush=True)
                self._stream_backoff_until = time.monotonic() + STREAM_BACKOFF
                self._frames_without_ack = 0
                return
        if not self._stream_announced:
            self._stream_announced = True
            print(f"kb7: streaming effect frames on {self._stream_node}", file=sys.stderr, flush=True)

    def close(self):
        # Never leave the board in direct mode: on fw 1.22 it repeated resting keys.
        if self._direct:
            try:
                self._direct_mode(False)
            except OSError:
                pass
        for fd in (self.fd, self._stream_fd, getattr(self, "_lock_fd", None), self._ctl_lock_fd):
            if fd is None:
                continue
            try:
                os.close(fd)
            except OSError:
                pass
