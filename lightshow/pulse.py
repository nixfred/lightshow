"""A software breathe for boards whose firmware has none: a thread that
paints a brightness level up and down. Used by the kernel-LED and QMK/VIA
boards, which take a level a few times a second and nothing faster."""

import math
import threading
import time


class BrightnessPulse:
    def __init__(self, paint, period=3.5, interval=0.2, floor=0.08):
        """paint(level01) is called every `interval` seconds with 0..1."""
        self._paint = paint
        self.period = period
        self.interval = interval
        self.floor = floor
        self._stop = threading.Event()
        self._thread = None

    def start(self, top=1.0):
        self.stop()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(top,), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1.0)
        self._thread = None

    def _run(self, top):
        t0 = time.monotonic()
        while not self._stop.is_set():
            phase = (time.monotonic() - t0) % self.period / self.period
            level = top * (self.floor + (1 - self.floor) * (0.5 - 0.5 * math.cos(2 * math.pi * phase)))
            try:
                self._paint(level)
            except OSError:
                pass
            self._stop.wait(self.interval)
