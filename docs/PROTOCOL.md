# MSI MysticLight keyboard protocol

Everything here was verified against real hardware (`0db0:1801`, MSI Crosshair
16 Max HX) on 2026-09-11. Where something is inferred rather than observed it
says so.

## Transport

A single USB HID interface, vendor usage page `0xFF00`. The report descriptor
declares:

- Report ID `1`: 63-byte OUTPUT and FEATURE
- Report ID `2`: 63-byte FEATURE, volatile

All traffic is **64-byte feature reports** (1 report ID + 63 data) sent with the
Linux `HIDIOCSFEATURE` ioctl:

```
_IOC(_IOC_WRITE|_IOC_READ, 'H', 0x06, 64) = 0xC0404806
```

Reading back with `HIDIOCGFEATURE` (`0xC0404807`) echoes the last packet's first
ten bytes; the keyframe region always reads as zero. That echo is useful for
confirming the device accepted a packet.

## Packets

Two feature reports per update, in this order.

### 1. Zone select

```
byte 0 : 0x02   report ID
byte 1 : 0x01   packet ID - zone select
byte 2 : mask   zone bitmask
3..63  : 0x00
```

The controller remembers the selection, so repeated writes of the same mask can
be skipped.

### 2. Mode and colour

```
byte 0     : 0x02   report ID
byte 1     : 0x02   packet ID - set mode/colour
byte 2     : mode
byte 3     : speed low byte    cycle duration in 1/100 s, little endian
byte 4     : speed high byte
byte 5     : 0x00
byte 6     : 0x00
byte 7     : 0x0F   constant, observed in OEM traffic
byte 8     : 0x01   constant, observed in OEM traffic
byte 9     : wave direction   0 = right-to-left, 1 = left-to-right
byte 10..49: up to 10 keyframes of (time_frame, R, G, B)
byte 50..63: padding
```

`time_frame` is 0-100, the percent position within the animation cycle. The
controller interpolates between keyframes on its own, which is why hardware
effects animate indefinitely at zero CPU.

Close the loop by repeating the first colour at `time_frame = 100`, or the cycle
jumps on wrap.

### Modes

| Value | Mode |
|---|---|
| 0 | off |
| 1 | static |
| 2 | breathing |
| 3 | cycle |
| 4 | wave |

**Mode 0 is not the same as static black.** Static black renders as "display the
colour black" and leaves the keyboard's base illumination lit. Mode 0 actually
darkens the zone. This is easy to miss because the two look identical on a
screen mock-up and only differ on physical keys.

### Zones

| Mask | Group |
|---|---|
| 1 | WASD cluster |
| 2 | Main letter block |
| 4 | Nav / arrows |
| 8 | Numpad |
| 15 | all |

Bits above `0x08` were probed (`0x10`, `0x20`, `0x40`, `0x80`, `0xF0`) and
produced no observable change.

## Observed hardware behaviour

- **Base backlight.** Lighting any zone raises a dim illumination across the
  whole board. Setting the other zones to static black, to mode 0, or changing
  the write order does not reduce it. Only a whole-board mode 0 reaches true
  black. The likely explanation is a base backlight level normally controlled by
  the keyboard-brightness hotkey, which is an embedded-controller function
  rather than anything in this protocol.
- **No brightness field.** Brightness must be applied by scaling RGB values
  before sending them.
- **Zone count.** Four groups, despite "24-zone" marketing. The 24 refers to
  physical LED clusters, not addressable units.

## Unexplored

The packet-ID byte (byte 1) is only known for `0x01` and `0x02`. Other values
are unexplored and may include a base-backlight or brightness command.

**A warning before you go looking.** The `msi-perkeyrgb` project has a
[documented issue](https://github.com/Askannz/msi-perkeyrgb/issues/24) where
sending a malformed effect bricked a keyboard's backlight. Probing unknown
packet IDs on this controller carries a real risk of the same. Nothing in
LightShow sends anything outside the two packets documented above.

## Credits

The packet layout is ported from
[OpenRGB](https://gitlab.com/CalcProgrammer1/OpenRGB)'s
`Controllers/MSIKeyboardController/MSIKeyboard1565Controller/`, which did the
original reverse engineering. The additions here are the newer product IDs, the
mode-0 versus static-black distinction, the zone bitmask probing and the base
backlight findings.
