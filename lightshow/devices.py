"""Every keyboard LightShow can drive, behind one object.

The engine talks to a single `kb`. With two keyboards on the desk (the
laptop's MSI MysticLight and a Turtle Beach KB7) that object fans every call
out to both, so effects, the theme watcher and the day/night schedule need no
idea how many boards there are. A board that errors on one call is skipped
for that call rather than taking the others down with it.
"""

import sys

from . import kbd, kb7


class Fanout:
    def __init__(self, boards):
        self.boards = boards

    @property
    def node(self):
        return "  +  ".join(b.node for b in self.boards)

    def _each(self, method, *args, **kwargs):
        errors = []
        for b in self.boards:
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
        self._each("frame", colors)

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

    def close(self):
        for b in self.boards:
            b.close()


def open_all():
    """Open every supported keyboard that is present.

    Raises DeviceError only when none can be opened, with each board's reason.
    """
    boards, reasons = [], []
    for label, cls in (("MSI MysticLight", kbd.Keyboard), ("Turtle Beach KB7", kb7.Keyboard)):
        try:
            boards.append(cls())
        except kbd.DeviceError as e:
            reasons.append(f"{label}: {e}")
    if not boards:
        raise kbd.DeviceError("; ".join(reasons) or "no supported keyboard found")
    return Fanout(boards)
