"""Every keyboard LightShow can drive, behind one object.

The engine talks to a single `kb`. With two keyboards on the desk (the
laptop's MSI MysticLight and a Turtle Beach KB7) that object fans every call
out to both, so effects, the theme watcher and the day/night schedule need no
idea how many boards there are. A board that errors on one call is skipped
for that call rather than taking the others down with it.
"""

import os
import sys
import time

from . import caps, kbd, kb7, leds, openrgb, via

# Every supported board: label, module (with present()), class, and how long to
# leave a newly seen board alone before opening it (a plug-time helper writes the
# KB7's tile labels on the same node for ~4 s; only one process may talk to it).
BOARDS = [
    ("MSI MysticLight", kbd, kbd.Keyboard, 0.0),
    ("Turtle Beach KB7", kb7, kb7.Keyboard, kb7.HOTPLUG_GRACE),
]
# Any keyboard backlight the kernel exposes (/sys/class/leds/*kbd_backlight*):
# white brightness-only ones and multicolor RGB ones alike. LIGHTSHOW_NO_LEDS=1
# leaves it out, for a laptop whose RGB controller is already driven natively
# and whose kernel LED is the same keys.
if os.environ.get("LIGHTSHOW_NO_LEDS") != "1":
    BOARDS.append(("Keyboard backlight", leds, leds.Backlight, 0.0))
# QMK keyboards with VIA (Keychron, most custom boards): found by their raw-HID
# usage, driven one colour at a time. One such board for now.
BOARDS.append(("QMK/VIA keyboard", via, via.Keyboard, 2.0))
# Every keyboard OpenRGB knows, if the openrgb package is installed: its server
# is started headless when none runs, asked once, and shut down again if it has
# no keyboard to offer. LIGHTSHOW_OPENRGB=0 leaves it out.
if os.environ.get("LIGHTSHOW_OPENRGB") != "0":
    BOARDS.append(("OpenRGB", openrgb, openrgb.Bridge, 15.0))


class Fanout:
    def __init__(self, boards, missing=()):
        self.boards = boards
        # boards not opened yet: {label: (module, cls, grace, first_seen, last_msg)}
        self.missing = {label: (mod, cls, grace, None, 0.0) for label, mod, cls, grace in missing}

    @property
    def node(self):
        return "  +  ".join(b.node for b in self.boards)

    def _each(self, method, *args, only=None, **kwargs):
        errors = []
        for b in self.boards:
            if only is not None and not only(b):
                continue
            try:
                getattr(b, method)(*args, **kwargs)
            except OSError as e:
                errors.append((b.node, e))
                print(f"lightshow: {b.node} {method} failed: {e}", file=sys.stderr, flush=True)
            except Exception as e:   # a driver bug must be visible, not silently kill a thread
                errors.append((b.node, e))
                print(f"lightshow: {b.node} {method} crashed: {type(e).__name__}: {e}",
                      file=sys.stderr, flush=True)
        if errors and len(errors) == len(self.boards):
            raise errors[0][1]   # nothing succeeded: let the caller back off

    def set(self, *args, **kwargs):
        self._each("set", *args, **kwargs)

    def solid(self, *args, **kwargs):
        self._each("solid", *args, **kwargs)

    def off(self, *args, **kwargs):
        self._each("off", *args, **kwargs)

    def frame(self, colors):
        # Only boards that can take a new set of colours many times a second;
        # the others keep the still look begin_software() gave them.
        self._each("frame", colors,
                   only=lambda b: getattr(b, "caps", None) is None or b.caps.streams)

    # -- what the boards can do ---------------------------------------------

    @staticmethod
    def _caps(b):
        return getattr(b, "caps", None) or caps.Caps(colour=caps.COLOUR_ZONES, streams=True,
                                                     modes=frozenset(caps.HARDWARE_EFFECTS))

    def boards_info(self):
        """One entry per connected board: name, node, capabilities, and the
        one-line notice about what it cannot do (None when it can do it all)."""
        out = []
        for b in self.boards:
            sub = getattr(b, "boards_info", None)
            if sub:
                out.extend(sub())
                continue
            c = self._caps(b)
            name = getattr(b, "name", b.node)
            out.append({"name": name, "node": b.node, "caps": c.as_dict(),
                        "notice": c.notice(name)})
        return out

    def fidelity(self, effect, software):
        """{board name: full|reduced|none} for one effect."""
        out = {}
        for b in self.boards:
            sub = getattr(b, "fidelity", None)
            if sub:
                out.update(sub(effect, software))
            else:
                out[getattr(b, "name", b.node)] = self._caps(b).fidelity(effect, software)
        return out

    def begin_software(self, name, colors):
        """A software effect is starting. Boards that cannot animate per frame
        (the KB7) paint one static look here; the others need nothing."""
        for b in self.boards:
            if hasattr(b, "begin_software"):
                try:
                    b.begin_software(name, colors)
                except Exception as e:
                    print(f"lightshow: {b.node} begin_software failed: {type(e).__name__}: {e}",
                          file=sys.stderr, flush=True)

    def poll(self):
        """True when a board wants the look re-applied: it came back after a
        replug, its active profile changed under us, or a board that was not
        there at start has been plugged in."""
        back = self._adopt_missing()
        for b in self.boards:
            poll = getattr(b, "poll", None)
            if not poll:
                continue
            try:
                back = poll() or back
            except Exception as e:
                print(f"lightshow: {b.node} poll crashed: {type(e).__name__}: {e}",
                      file=sys.stderr, flush=True)
        return back

    def _adopt_missing(self):
        """Open a board that was absent (or not yet accessible) at start, once
        it has been on the bus for its grace period. Returns True on adoption."""
        now = time.monotonic()
        for label, (mod, cls, grace, first_seen, last_msg) in list(self.missing.items()):
            if not mod.present():
                if first_seen is not None:
                    self.missing[label] = (mod, cls, grace, None, last_msg)
                continue
            if first_seen is None:
                self.missing[label] = (mod, cls, grace, now, last_msg)
                continue
            if now - first_seen < grace:
                continue
            try:
                board = cls()
            except kbd.DeviceError as e:
                if not mod.present():
                    # It answered and had nothing for us (OpenRGB with no keyboard):
                    # say so once and stop looking until the next start.
                    print(f"lightshow: {label}: {e}", file=sys.stderr, flush=True)
                    del self.missing[label]
                    continue
                if now - last_msg > 60:
                    print(f"lightshow: {label} is on the bus but cannot be opened: {e}; retrying",
                          file=sys.stderr, flush=True)
                    last_msg = now
                self.missing[label] = (mod, cls, grace, now, last_msg)   # wait another grace
                continue
            del self.missing[label]
            self.boards.append(board)
            print(f"lightshow: {label} connected on {board.node}", file=sys.stderr, flush=True)
            return True
        return False

    def close(self):
        for b in self.boards:
            b.close()


def open_all():
    """Open every supported keyboard that is present.

    Raises DeviceError only when none can be opened, with each board's reason.
    """
    boards, reasons, missing = [], [], []
    for label, mod, cls, grace in BOARDS:
        if mod is openrgb:
            # Never drive a keyboard twice: the bridge skips any device whose
            # location names a node a native driver already holds.
            openrgb.EXCLUDE_NODES = {b.node for b in boards} | {
                n for n in (kb7.find_control_node(), kb7.find_stream_node()) if n}
        try:
            boards.append(cls())
        except kbd.DeviceError as e:
            reasons.append(f"{label}: {e}")
            missing.append((label, mod, cls, grace))
    if not boards:
        raise kbd.DeviceError("; ".join(reasons) or "no supported keyboard found")
    for r in reasons:
        # Say so at start: a board that is missing now is adopted when it appears.
        print(f"lightshow: {r}; will keep looking for it", file=sys.stderr, flush=True)
    return Fanout(boards, missing)
