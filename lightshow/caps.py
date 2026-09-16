"""What a lit keyboard can actually do, read from what its hardware advertises.

Every mature lighting project (OpenRGB, OpenRazer, Solaar, Piper) ended up
here: a set of independent capabilities per device, never one "supported"
boolean and never a guess from the model name. LightShow uses the same idea
so an effect can be shown on any board at the best fidelity that board has,
and so the window can say, in one line, what a board cannot do.

Fidelity of an effect on a board:
  "full"     the board shows the effect as designed
  "reduced"  something sensible is shown (a still look, a brightness pulse)
  "none"     nothing meaningful can be shown
"""

from dataclasses import dataclass, field

COLOUR_NONE = "none"        # white backlight: brightness only
COLOUR_ONE = "one"          # one colour for the whole board
COLOUR_ZONES = "zones"      # a few independently coloured groups
COLOUR_PER_KEY = "per-key"  # every key its own colour

HARDWARE_EFFECTS = ("off", "static", "breathe", "cycle", "wave")


@dataclass(frozen=True)
class Caps:
    colour: str = COLOUR_NONE
    brightness: bool = True         # has levels, not just on/off
    streams: bool = False           # takes a new set of colours many times a second
    modes: frozenset = field(default_factory=frozenset)   # effects its firmware runs itself
    zones: int = 0                  # independently addressable groups (0 = one)

    @property
    def has_colour(self):
        return self.colour != COLOUR_NONE

    def fidelity(self, effect, software):
        """How well this board can show `effect` (a name; software=True for a
        frame-stepped effect)."""
        if effect == "off":
            return "full"
        if not self.has_colour:
            # A white backlight shows light and dark, nothing else.
            if effect in ("static", "breathe"):
                return "full" if self.brightness or effect == "static" else "reduced"
            return "reduced" if self.brightness else "none"
        if software:
            return "full" if self.streams else "reduced"
        if effect in self.modes:
            return "full"
        if effect == "static":
            return "full"
        return "reduced"          # firmware lacks it: shown as static / a still look

    def notice(self, name):
        """One line for the window about what this board cannot do, or None
        when it can do everything LightShow offers."""
        if not self.has_colour:
            what = "brightness, breathe and off" if self.brightness else "on and off"
            return f"{name} has no colour control: {what} only."
        gaps = []
        if not self.streams:
            gaps.append("animated effects show as a still look")
        missing = [m for m in ("breathe", "cycle", "wave") if m not in self.modes]
        if missing:
            gaps.append(f"no firmware {'/'.join(missing)}, shown as the nearest effect it has")
        if self.colour == COLOUR_ONE:
            gaps.append("one colour for the whole board")
        if not gaps:
            return None
        return f"{name}: " + "; ".join(gaps) + "."

    def describe(self):
        colour = {COLOUR_NONE: "white, no colour", COLOUR_ONE: "one colour",
                  COLOUR_ZONES: f"{self.zones} colour zones",
                  COLOUR_PER_KEY: "per-key colour"}[self.colour]
        bits = [colour]
        if self.streams:
            bits.append("streams")
        if self.modes:
            bits.append("firmware " + "/".join(sorted(self.modes)))
        return ", ".join(bits)

    def as_dict(self):
        return {"colour": self.colour, "brightness": self.brightness,
                "streams": self.streams, "modes": sorted(self.modes),
                "zones": self.zones, "summary": self.describe()}
