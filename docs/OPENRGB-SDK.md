# OpenRGB SDK network protocol, as LightShow uses it

> Byte-exact notes on the OpenRGB SDK wire format, extracted 2026-09-15 from a
> line-by-line read of the OpenRGB sources (NetworkProtocol.h,
> RGBControllerInterface.h, RGBController.cpp, NetworkClient.cpp,
> NetworkServer.cpp, master branch) and kept here so `lightshow/openrgb.py` can
> be checked against something other than itself. LightShow negotiates protocol
> version 3 on purpose and only needs sections 1-8; the version-6 fields are
> documented for completeness. One correction to the extract as delivered: the
> packet header is **16 bytes** (`"<4sIII"` = magic + three u32), verified live
> against `openrgb --server` 1.0rc3, not 12 as the extract said.


Verified byte-exact against master source (NetworkProtocol.h, RGBControllerInterface.h,
RGBController.cpp GetDeviceDescriptionData/GetModeDescriptionData/GetZoneDescriptionData/
SetColorDescription, NetworkClient.cpp, NetworkServer.cpp — all fetched raw from
raw.githubusercontent.com, no summarization). Note: the CURRENT server protocol version is
6, not 4. All integers little-endian.

## 1. Header (16 bytes; header and payload may be two writes)

`NetPacketHeader` — struct `"<4sIII"`: magic(4)="ORGB", pkt_dev_id(u32), pkt_id(u32),
pkt_size(u32) = payload byte length that follows. TCP port 6742 default ("ORGB" on a phone
keypad). Source: NetworkProtocol.h L52-58, L44.

## 2. Version negotiation — id 40 `NET_PACKET_ID_REQUEST_PROTOCOL_VERSION`

Request payload: `"<I"` = client's max supported version (currently 6). Response payload:
`"<I"` = server's protocol version. If no reply within ~1s the real client assumes version
0. Effective version = min(client, server). Source: NetworkClient.cpp L1811-1817,
L2334-2341, L2686-2699.

Version history (comment block, NetworkProtocol.h L16-28):
- 0: unversioned
- 1: adds vendor string to device description
- 2: profile controls
- 3: adds mode.brightness_min/max and mode.brightness fields
- 4: adds zone.segments (with segment name/type/start_idx/leds_count), network plugins
- 5: zone flags, controller flags, resizable effects-only zones
- 6: server name, mode.value dropped from wire, LED alt-names + controller flags, zone
  active_mode+modes+display_name, segment matrix_map+flags, controller
  display_name+configuration JSON, direction enum extended (diagonal), mode flags extended
  (diagonal)

## 3. `NET_PACKET_ID_SET_CLIENT_NAME` = 50

dev_id=0. Payload: raw client name bytes INCLUDING a trailing NUL (`pkt_size =
strlen(name)+1`), no length prefix — the header's pkt_size IS the length. Source:
NetworkClient.cpp L2663-2673.

## 4. `NET_PACKET_ID_REQUEST_CONTROLLER_COUNT` = 0

Request: dev_id=0, pkt_size=0, no payload.
Response payload (protocol < 6): `"<I"` = controller_count only.
Response payload (protocol >= 6): `"<I"` count, then count × `"<I"` controller ids.
Source: NetworkServer.cpp L3603-3632.

## 5. `NET_PACKET_ID_REQUEST_CONTROLLER_DATA` = 1

Request: dev_id = target controller index (or id if proto>=6). Payload: none if
protocol==0; else `"<I"` = protocol_version client wants to use for the reply. Source:
NetworkClient.cpp L582-608.

Response payload = full device description, in this EXACT order
(`GetDeviceDescriptionData`, RGBController.cpp L2434-2622):

1. `device_type` i32
2. name: `"<H"` len(incl. NUL) + bytes
3. vendor (only if version>=1): `"<H"` len + bytes
4. description: `"<H"` len + bytes
5. version: `"<H"` len + bytes
6. serial: `"<H"` len + bytes
7. location: `"<H"` len + bytes
8. `num_modes` `"<H"`
9. `active_mode` `"<i"`
10. num_modes × mode blocks (layout below)
11. `num_zones` `"<H"`
12. num_zones × zone blocks (layout below)
13. `num_leds` `"<H"`
14. num_leds × LED blocks: name `"<H"`+bytes, then `value` `"<I"` ONLY if version<6
    (dropped at v6!)
15. colors: `"<H"` num_colors, then num_colors × `RGBColor` `"<I"` (this is the flat
    controller-wide color array, separate from per-mode colors)
16. if version>=5: `num_led_display_names` `"<H"`, then that many `"<H"`+bytes strings
17. if version>=5: controller `flags` `"<I"`
18. if version>=6: display_name `"<H"`+bytes, then configuration (JSON) `"<I"` len(incl
    NUL) + bytes — NOTE this one string uses a 4-byte length, not 2-byte

**Mode block** (`GetModeDescriptionData`, L2859-2993):
name `"<H"`+bytes; `value` `"<i"` ONLY if version<6; `flags` `"<I"` (diagonal bit 11
stripped if version<6); `speed_min` `"<I"`; `speed_max` `"<I"`; if version>=3:
`brightness_min` `"<I"`, `brightness_max` `"<I"`; `colors_min` `"<I"`; `colors_max` `"<I"`;
`speed` `"<I"`; if version>=3: `brightness` `"<I"`; `direction` `"<I"` (clamped to LEFT if
>VERTICAL and version<6); `color_mode` `"<I"`; `num_colors` `"<H"`; num_colors ×
`RGBColor` `"<I"`.

**Zone block** (`GetZoneDescriptionData`, L3078-3252):
name `"<H"`+bytes; `type` `"<I"` (LINEAR_LOOP→LINEAR, MATRIX_LOOP_X/Y→MATRIX,
SEGMENTED→LINEAR if version<6); `leds_min` `"<I"`; `leds_max` `"<I"`; `leds_count` `"<I"`;
`matrix_map_size` `"<H"` (byte length of what follows, 0 if empty) then if nonzero:
`height` `"<I"`, `width` `"<I"`, height*width × `"<I"` map values; if version>=4:
`num_segments` `"<H"` then that many segment blocks; if version>=5: zone `flags` `"<I"`;
if version>=6: zone `active_mode` `"<i"`, zone `num_modes` `"<H"` + that many mode blocks
(same layout as above), zone display_name `"<H"`+bytes.

**Segment block** (L2995-3046): name `"<H"`+bytes; `type` `"<I"`; `start_idx` `"<I"`;
`leds_count` `"<I"`; if version>=6: `matrix_map_size` `"<H"` + matrix data (same as zone
matrix map), then segment `flags` `"<I"`.

**RGBColor**: `unsigned int`, packed by `ToRGBColor(r,g,b) = (b<<16)|(g<<8)|r`. As a
little-endian u32 on the wire this is byte order R,G,B,0x00 — i.e.
`struct.pack("<I", r | g<<8 | b<<16)`, equivalently `"<BBBx"` (R,G,B,pad). Source:
RGBControllerInterface.h L23-31.

## 6. Mode flag bits (RGBControllerInterface.h L62-77)

HAS_SPEED=1<<0, HAS_DIRECTION_LR=1<<1, HAS_DIRECTION_UD=1<<2, HAS_DIRECTION_HV=1<<3,
HAS_BRIGHTNESS=1<<4, HAS_PER_LED_COLOR=1<<5, HAS_MODE_SPECIFIC_COLOR=1<<6,
HAS_RANDOM_COLOR=1<<7, MANUAL_SAVE=1<<8, AUTOMATIC_SAVE=1<<9,
REQUIRES_ENTIRE_DEVICE=1<<10, HAS_DIRECTION_DIAG=1<<11 (v6+ only, stripped for older
clients).

MODE_COLORS_*: NONE=0, PER_LED=1, MODE_SPECIFIC=2, RANDOM=3 (L99-105).

MODE_DIRECTION_*: LEFT=0, RIGHT=1, UP=2, DOWN=3, HORIZONTAL=4, VERTICAL=5, UP_LEFT=6,
UP_RIGHT=7, DOWN_LEFT=8, DOWN_RIGHT=9 (v6+; older clients only ever see 0-5) (L82-94).

DEVICE_TYPE_* (int, in enum-declaration order, L182-206): MOTHERBOARD=0, DRAM=1, GPU=2,
COOLER=3, LEDSTRIP=4, KEYBOARD=5, MOUSE=6, MOUSEMAT=7, HEADSET=8, HEADSET_STAND=9,
GAMEPAD=10, LIGHT=11, SPEAKER=12, VIRTUAL=13, STORAGE=14, CASE=15, MICROPHONE=16,
ACCESSORY=17, KEYPAD=18, LAPTOP=19, MONITOR=20, UNKNOWN=21.

ZONE_TYPE_*: SINGLE=0, LINEAR=1, MATRIX=2, LINEAR_LOOP=3, MATRIX_LOOP_X=4,
MATRIX_LOOP_Y=5, SEGMENTED=6 (L151-160).

## 7. LED update packets (NetworkClient.cpp L1455-1503, NetworkServer.cpp L3094-3161,
L3270+)

`RGBCONTROLLER_UPDATELEDS`=1050: payload = `data_size`(`"<I"`, = remaining bytes incl.
num_colors field) then `num_colors`(`"<H"`) then num_colors×`RGBColor`(`"<I"`). This is
exactly `SetColorDescription`'s wire format (L3532+).

`RGBCONTROLLER_UPDATEZONELEDS`=1051: same as above but server reads a `zone_idx` `"<I"`
immediately after `data_size`, before `num_colors` (confirm zone_idx position against
`SetColorDescription`'s zone-resize variant if implementing — server-side dispatch at
L1483-1484 queues it identically to UPDATELEDS, actual zone_idx field is inside the
data_ptr blob, consistent with openrgb-python's `data_size,zone_idx,num_colors,colors`
layout).

`RGBCONTROLLER_UPDATESINGLELED`=1052: payload = `led_idx`(`"<i"`) + `color`(`"<I"`
RGBColor). No data_size prefix — server validates `data_size ==
sizeof(int)+sizeof(RGBColor)` (=8 bytes) exactly (NetworkServer.cpp L3291).

## 8. Mode packets

`RGBCONTROLLER_UPDATEMODE`=1101 / `RGBCONTROLLER_SAVEMODE`=1102: payload =
`data_size`(`"<I"`) + `mode_idx`(`"<i"`) + full mode block (section 5 layout,
version-gated) via `SetModeDescription`. Source: NetworkServer.cpp
`ProcessRequest_RGBController_UpdateSaveMode`, L3163-3268.

`RGBCONTROLLER_SETCUSTOMMODE`=1100: dev_id=target, pkt_size=0, no payload. Source:
NetworkClient.cpp L1506-1520.

## 9. Pitfalls

- `NET_PACKET_ID_DEVICE_LIST_UPDATED`=100 arrives unsolicited (dev_id=0, pkt_size=0)
  whenever devices are rescanned/added. A client blocking on a reply for another packet id
  must keep reading and skip/recurse past this one rather than treat it as the awaited
  reply — the reference client literally calls `read()` again recursively on it
  (openrgb-python's `network.py` does the same: read, callback, loop again).
- Header + payload are sent as two separate `send()` calls in the reference implementation
  (not required to be one write, but must both complete — no framing besides pkt_size, so
  a client must always read exactly `pkt_size` more bytes after the 16-byte header, looping
  on `recv` since TCP doesn't guarantee one packet = one read).
- No idle timeout enforced server-side; the version-request path in the client waits up to
  1s (200×5ms) then falls back to version 0 — good default for a client implementation
  too.
- All strings are NUL-terminated in the length count (len = strlen+1), and are written
  with `strcpy`, i.e. they must not contain embedded NULs.
- Server max packet size is 8 MiB (`OPENRGB_SDK_MAX_PACKET_SIZE`).

If the exact `UPDATEZONELEDS` byte offsets or the `RESIZEZONE`/`CONFIGUREZONE`/
`ADDSEGMENT` payloads matter for the client, those still need to be confirmed
line-by-line from the server-side parse functions — the zone_idx position above was
inferred from client-side symmetry with UPDATELEDS rather than read directly.
