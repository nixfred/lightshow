"""Effects for LightShow.

Two families, and the difference matters:

HARDWARE effects (static, breathe, cycle, wave) hand the controller a set of
keyframes and let its own microcontroller interpolate forever. Zero CPU, they
survive the app exiting, and they apply ONE animation to every zone in lockstep.

SOFTWARE effects drive each zone to a different colour on every frame from a
Python loop. That is the only way to get motion ACROSS the keyboard, but it
needs a live process and stops when the app stops.

Each software effect is a generator yielding (frame, delay):
frame is a list of 4 RGB tuples in zone order [wasd, alpha, nav, numpad].
Yielding lets the runner stop cleanly mid-effect without a thread kill.
"""

import math
import random

from . import kbd
from .kbd import ZONE_ALL, mix, scale, hsv_rgb

N = 4  # addressable zones


# ---------------------------------------------------------------- hardware


def apply_hardware(kb, name, colors, speed=1.0, brightness=1.0):
    """Set a persistent hardware mode. Returns True if handled."""
    cols = [scale(c, brightness) for c in (colors or kbd.FALLBACK)]

    if name == "off":
        kb.off()
    elif name == "static":
        kb.set(ZONE_ALL, kbd.MODE_STATIC, [(0, *cols[0])])
    elif name == "breathe":
        kb.set(ZONE_ALL, kbd.MODE_BREATHING,
               [(0, *scale(cols[0], 0.05)), (50, *cols[0])],
               cycle_cs=int(350 / max(0.1, speed)))
    elif name == "cycle":
        pts = cols[:8]
        step = 100 // max(1, len(pts))
        kb.set(ZONE_ALL, kbd.MODE_CYCLE,
               [(i * step, *c) for i, c in enumerate(pts)],
               cycle_cs=int(900 / max(0.1, speed)))
    elif name == "wave":
        pts = cols[:6]
        step = 100 // max(1, len(pts))
        kb.set(ZONE_ALL, kbd.MODE_WAVE,
               [(i * step, *c) for i, c in enumerate(pts)],
               cycle_cs=int(500 / max(0.1, speed)), wave_dir=kbd.WAVE_LTR)
    else:
        return False
    return True


HARDWARE = ["off", "static", "breathe", "cycle", "wave"]


# ---------------------------------------------------------------- software


def fx_fireworks(colors, speed=1.0):
    """Dark sky, sudden bursts, slow sparkling decay.

    Each shell picks a zone and a colour, flashes near-white at the moment of
    detonation, then decays through its colour while neighbours catch a faint
    reflected glow.
    """
    lvl = [0.0] * N
    hue = [random.choice(colors) for _ in range(N)]
    fuse = 0.0
    while True:
        fuse -= 1
        if fuse <= 0:
            z = random.randrange(N)
            lvl[z] = 1.0
            hue[z] = random.choice(colors)
            # Neighbours pick up the flash, weaker.
            for nb in (z - 1, z + 1):
                if 0 <= nb < N:
                    lvl[nb] = max(lvl[nb], 0.35)
                    hue[nb] = hue[z]
            fuse = random.randint(4, 14)
        frame = []
        for i in range(N):
            # Detonation whites out briefly, then falls back to the shell colour.
            c = mix(hue[i], (255, 255, 255), max(0.0, lvl[i] - 0.75) * 3)
            frame.append(scale(c, lvl[i] ** 1.7))
            lvl[i] *= 0.80 + random.random() * 0.06  # flicker in the decay
        yield frame, 0.055 / max(0.1, speed)


def fx_waterfall(colors, speed=1.0):
    """A continuous cascade pouring left to right with a trailing mist."""
    pos = 0.0
    while True:
        frame = []
        for i in range(N):
            # Distance behind the falling head, wrapped.
            d = (pos - i) % (N + 1.5)
            intensity = math.exp(-d * 1.15)       # sharp head, long tail
            base = colors[int(pos) % len(colors)]
            foam = mix(base, (255, 255, 255), min(1.0, intensity * 0.55))
            frame.append(scale(foam, min(1.0, intensity + 0.05)))
        pos = (pos + 0.16 * speed) % (N + 1.5)
        yield frame, 0.04


def fx_aurora(colors, speed=1.0):
    """Slow independent drift, like northern lights across the board."""
    phase = [random.random() * len(colors) for _ in range(N)]
    rate = [0.018 + 0.010 * i for i in range(N)]
    while True:
        frame = []
        for i in range(N):
            phase[i] = (phase[i] + rate[i] * speed) % len(colors)
            a = colors[int(phase[i]) % len(colors)]
            b = colors[(int(phase[i]) + 1) % len(colors)]
            frame.append(mix(a, b, phase[i] - int(phase[i])))
        yield frame, 0.05


def fx_ember(colors, speed=1.0):
    """Banked coals. Deep reds breathing, with the odd bright flare."""
    heat = [random.random() for _ in range(N)]
    COOL, HOT = (60, 6, 0), (255, 150, 20)
    while True:
        frame = []
        for i in range(N):
            heat[i] += (random.random() - 0.45) * 0.14 * speed
            heat[i] = max(0.12, min(1.0, heat[i]))
            if random.random() < 0.02:
                heat[i] = 1.0  # a log shifts and it flares
            frame.append(scale(mix(COOL, HOT, heat[i]), 0.35 + heat[i] * 0.65))
        yield frame, 0.075


def fx_matrix(colors, speed=1.0):
    """Green rain. Bright head, decaying trail, random re-seeding."""
    trail = [0.0] * N
    HEAD, TAIL = (200, 255, 200), (0, 255, 70)
    drop = 0.0
    while True:
        drop += 0.22 * speed
        if drop >= 1:
            drop = 0
            trail[random.randrange(N)] = 1.0
        frame = []
        for i in range(N):
            c = mix(TAIL, HEAD, max(0.0, trail[i] - 0.8) * 5)
            frame.append(scale(c, trail[i]))
            trail[i] *= 0.86
        yield frame, 0.05


def fx_police(colors, speed=1.0):
    """Red and blue strobe, split across the board. Obnoxious on purpose."""
    RED, BLUE = (255, 0, 0), (0, 40, 255)
    t = 0
    while True:
        t += 1
        burst = (t // 2) % 2 == 0      # rapid double-flash
        side = (t // 8) % 2 == 0       # which half is live
        on = RED if side else BLUE
        frame = []
        for i in range(N):
            live = (i < N // 2) if side else (i >= N // 2)
            frame.append(on if (live and burst) else (0, 0, 0))
        yield frame, 0.06 / max(0.1, speed)


def fx_tide(colors, speed=1.0):
    """A slow swell washing in and back out. Calm, good for evenings."""
    t = 0.0
    deep = colors[0]
    shallow = mix(colors[0], (255, 255, 255), 0.5)
    while True:
        t += 0.030 * speed
        frame = []
        for i in range(N):
            # Sine wave travelling along the board, never fully dark.
            v = (math.sin(t * 2 - i * 0.9) + 1) / 2
            frame.append(scale(mix(deep, shallow, v), 0.25 + v * 0.75))
        yield frame, 0.05


def fx_rainbow_road(colors, speed=1.0):
    """Full-spectrum chase. Ignores the theme by design: it is the whole point."""
    h = 0.0
    while True:
        h = (h + 0.012 * speed) % 1.0
        yield [hsv_rgb((h + i * 0.13) % 1.0, 1.0, 1.0) for i in range(N)], 0.04


def fx_heartbeat(colors, speed=1.0):
    """Lub-dub, pause. Two pulses then rest, in the theme accent."""
    c = colors[0]
    # One full beat as amplitude steps: thump, small gap, thump, long rest.
    beat = ([1.0, 0.7, 0.35, 0.12] + [0.05] * 2 +
            [0.9, 0.6, 0.3, 0.10] + [0.04] * 12)
    i = 0
    while True:
        a = beat[i % len(beat)]
        i += 1
        yield [scale(c, a)] * N, 0.045 / max(0.1, speed)


def fx_vapor(colors, speed=1.0):
    """Vaporwave: hot pink and cyan sliding over each other."""
    PINK, CYAN, PURPLE = (255, 60, 180), (60, 240, 255), (150, 60, 255)
    pal = [PINK, PURPLE, CYAN, PURPLE]
    t = 0.0
    while True:
        t += 0.02 * speed
        frame = []
        for i in range(N):
            f = (t + i * 0.25) % len(pal)
            frame.append(mix(pal[int(f)], pal[(int(f) + 1) % len(pal)], f - int(f)))
        yield frame, 0.045


def fx_scanner(colors, speed=1.0):
    """Knight Rider. A single eye sweeping back and forth with a trail."""
    c = colors[0]
    pos, direction = 0.0, 1
    while True:
        pos += 0.18 * speed * direction
        if pos >= N - 1:
            pos, direction = N - 1, -1
        elif pos <= 0:
            pos, direction = 0, 1
        frame = []
        for i in range(N):
            d = abs(i - pos)
            frame.append(scale(c, max(0.0, 1.0 - d * 0.75) ** 2))
        yield frame, 0.04


def fx_lightning(colors, speed=1.0):
    """Dark, then a sudden multi-strike flash and a slow afterglow."""
    glow = 0.0
    wait = 0
    strike = 0
    while True:
        if strike > 0:
            strike -= 1
            lit = random.random() < 0.6
            yield [(255, 255, 255) if lit else scale(colors[0], 0.1)] * N, 0.035
            continue
        wait -= 1
        if wait <= 0:
            strike = random.randint(2, 6)
            glow = 1.0
            wait = random.randint(25, 70)
        glow *= 0.90
        yield [scale(mix((10, 12, 30), colors[0], glow), 0.15 + glow * 0.85)] * N, 0.05


def fx_scroll(colors, speed=1.0, word="OMARCHY", trail=0.28):
    """Scroll a word across the zones as a marquee.

    Four zones is a four-cell display, so letters cannot be drawn as glyphs.
    Each letter is a travelling pulse in its own colour, marching left to right,
    with a dimmed tail so the motion reads as direction rather than a blink.
    """
    word = (word or "OMARCHY").upper()
    letters = [colors[i % len(colors)] for i in range(len(word))]
    while True:
        for head in range(-len(letters), N + 1):
            frame = []
            for ci in range(N):
                idx = head - ci
                if 0 <= idx < len(letters):
                    frame.append(letters[idx])
                else:
                    nearest = min(
                        (abs(head - ci - i) for i in range(len(letters))), default=99
                    )
                    frame.append(scale(colors[0], trail / (nearest + 1)))
            yield frame, 0.16 / max(0.1, speed)
        yield [(0, 0, 0)] * N, 0.32 / max(0.1, speed)


def fx_gamer(colors, speed=1.0):
    """Gaming keys only: WASD and Nav lit, main block and numpad dark.

    This is as close as the hardware gets to per-key control. The MS-1801
    exposes four zone GROUPS, so individual keys (Q, R, F, numbers, Left Shift,
    SUPER) cannot be addressed. Numbers sit in the Alpha zone and stay dark.
    """
    c = colors[0]
    t = 0.0
    while True:
        t += 0.03 * speed
        pulse = 0.72 + 0.28 * (math.sin(t * 2) + 1) / 2
        # [wasd, alpha, nav, numpad]
        yield [scale(c, pulse), (0, 0, 0), scale(c, pulse * 0.8), (0, 0, 0)], 0.05


def fx_smatter(colors, speed=1.0):
    """Smatter: every zone wearing a DIFFERENT theme colour at once.

    The point is to show the whole palette simultaneously rather than one
    colour at a time, so the keyboard reads as the active theme rather than as
    its accent. Each zone walks the palette at its own slightly different rate,
    so the combination keeps reshuffling instead of marching in step.

    The highlight group (zone 1 - Left Shift, SUPER, Q, W, R, F, arrows and the
    number row on the verified hardware) is kept at full brightness while the
    rest sit back, so those keys still stand out inside the spread.
    """
    pts = colors if len(colors) > 1 else (colors * 4)
    n = len(pts)
    # Start the zones spread across the palette, not stacked on one colour.
    phase = [(i * n / float(N)) for i in range(N)]
    # Deliberately non-harmonic rates so the pattern never repeats visibly.
    rate = [0.013, 0.009, 0.017, 0.011]
    # Highlight group full, the rest set back so Fred's keys still lead.
    level = [1.0, 0.55, 0.70, 0.45]
    while True:
        frame = []
        for i in range(N):
            phase[i] = (phase[i] + rate[i] * speed) % n
            a = pts[int(phase[i]) % n]
            b = pts[(int(phase[i]) + 1) % n]
            frame.append(scale(mix(a, b, phase[i] - int(phase[i])), level[i]))
        yield frame, 0.05


def fx_codedark(colors, speed=1.0):
    """Codedark: your keys drift through the theme palette, the rest stays down.

    Zone 1 covers exactly the keys Fred asked for (Left Shift, SUPER, Q, W, R,
    F, the arrows and the number row) - MSI built it as the gaming highlight
    group, verified on this hardware 2026-09-11.

    The highlight blends smoothly between every vivid colour in the active
    Omarchy theme rather than sitting on one, so it reads as the system's
    palette rather than a single accent. The engine re-reads the palette when
    the theme changes, so this follows a theme switch on its own.

    KNOWN LIMIT: lighting any zone also raises a dim base backlight across the
    whole board. Static black, per-zone MODE_OFF, both write orders and the
    unused zone-bitmask bits were all tested; none reduce it. Only a whole-board
    OFF reaches true black. The other three zones are held at black regardless,
    so nothing is ADDED to that floor.
    """
    pts = colors if len(colors) > 1 else (colors * 2)
    phase = 0.0
    while True:
        phase = (phase + 0.010 * speed) % len(pts)
        a = pts[int(phase) % len(pts)]
        b = pts[(int(phase) + 1) % len(pts)]
        hi = mix(a, b, phase - int(phase))
        # [highlight group, alpha, nav, numpad]
        yield [hi, (0, 0, 0), (0, 0, 0), (0, 0, 0)], 0.06


SOFTWARE = {
    "smatter":      (fx_smatter,      "Every zone a different theme colour, always reshuffling"),
    "fireworks":    (fx_fireworks,    "Bursts flash and sparkle down to dark"),
    "waterfall":    (fx_waterfall,    "A cascade pouring across with foam and mist"),
    "aurora":       (fx_aurora,       "Slow independent drift, northern lights"),
    "ember":        (fx_ember,        "Banked coals breathing, with sudden flares"),
    "matrix":       (fx_matrix,       "Green rain with bright heads and decay"),
    "police":       (fx_police,       "Red and blue strobe. Obnoxious on purpose"),
    "tide":         (fx_tide,         "A slow swell washing in and back out"),
    "rainbow":      (fx_rainbow_road, "Full-spectrum chase, ignores the theme"),
    "heartbeat":    (fx_heartbeat,    "Lub-dub, pause, in the theme accent"),
    "vapor":        (fx_vapor,        "Hot pink and cyan sliding over each other"),
    "scanner":      (fx_scanner,      "A single eye sweeping back and forth"),
    "lightning":    (fx_lightning,    "Dark, then multi-strike flash and afterglow"),
    "scroll":       (fx_scroll,       "Scroll any word across the keyboard"),
    "gamer":        (fx_gamer,        "Gaming keys only: WASD and arrows, breathing"),
    "codedark":     (fx_codedark,     "Your keys drifting through the theme palette, rest down"),
}

HARDWARE_INFO = {
    "wave":    "Hardware wave across the zones. Zero CPU",
    "breathe": "Hardware breathing on the accent. Zero CPU",
    "cycle":   "Hardware cycle through the palette. Zero CPU",
    "static":  "One solid colour",
    "off":     "All zones dark",
}


def all_effects():
    out = []
    for n in HARDWARE:
        out.append({"name": n, "kind": "hardware", "desc": HARDWARE_INFO.get(n, "")})
    for n, (_, d) in SOFTWARE.items():
        out.append({"name": n, "kind": "software", "desc": d})
    return out
