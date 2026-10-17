# Firmware internals

Findings from reading the MIPS code, as opposed to the container and resource
formats covered in the [README](../README.md). Everything here was produced
with `sunp_code.py` against the baseline image
(`VSF010W v1.09.00`, built Oct 2 2019), and every claim cites the address it
came from so it can be re-checked:

```sh
./sunp_code.py xref unpacked/sections/06_mainfw.bin --string 'Check UHS-I SD Bus'
/tmp/capenv/bin/python sunp_code.py dis unpacked/sections/06_mainfw.bin --addr 0x80077df4
```

Where something is inference rather than something I read in the code, it says
so. Addresses are virtual; file offset = VA − load base.

## Storage stack: there is no card-capacity ceiling

The DC2000 is commonly described as supporting SDXC only up to 64 GB. **No
such limit exists in the firmware.** Every layer that could impose one was
traced and is correct well past 128 GB:

| Layer | Address | What it does | Verdict |
|---|---|---|---|
| CSD capacity | `0x80077df4` | `extract_bits(csd, 58, 22)` — full 22-bit `C_SIZE` — then `(C_SIZE+1) << 10` sectors | Correct to just under 2 TB |
| SDXC detection | `0x80077e98` | `sltu sizeMB, 32769` → card type 4 above 32 GB | No upper bound |
| MBR parse | `0x8005d970` | `lwl`/`lwr` unaligned u32 LBA + sector count; `srl $a0, 0xb` for MB; `lastSec = start + count - 1` | Unsigned 32-bit throughout |
| exFAT mount check | `0x80040098` | The only validation is `sltu startSec < lastSec` | Cannot fail on a valid large card |
| exFAT bitmap | `0x80041660` | Windowed cache, bounds at `+0x2b8`/`+0x2bc`, re-read on miss | Not a fixed whole-bitmap buffer |

The partition record is `{u32 startSec, u32 lastSec, u32 type, u32 sizeMB}` —
no 16-bit megabyte field anywhere. exFAT is fully implemented in both the main
firmware and the ISP loader (~53 symbols each), so "no exFAT support" is also
not the explanation.

Two hypotheses that string analysis suggested and the code disproved, recorded
so nobody re-derives them: a 16-bit MB capacity field, and an allocation
bitmap read whole into a fixed buffer. Neither exists.

If large cards misbehave, the cause is not a size check. The next candidate is
bus speed, below.

## SD bus speed

The driver implements UHS-I properly: 1.8 V switching, CMD19 tuning, and
`SDR12`/`SDR25`/`SDR50`/`SDR104` plus `UHS-I SD Card DDR Mode Enable`. Writes
are multi-block DMA with CMD12, and CMD23 (`SET_BLOCK_COUNT`) support is read
from SCR bit 25 (`ext $a1, $v0, 0x19, 1` at `0x80077f74`) and cached at
`-0x3edc($gp)`.

**Clock selection** lives at `0x80078440`, keyed on a speed-mode byte at
`-0x3ee4($gp)`:

| Mode | Result |
|---|---|
| < 3 | 3.3 V paths (`3V Set 50MHz`, lower) |
| 3 | default; falls through the ladder below |
| 4, 7 | `Set 50MHz` |
| 5 | `Set 100MHz` (SDR104) |
| 6 | computed, clamped at 142 MHz |

`sunp_code.py gp --offset -0x3ee4` finds all 54 accesses; several sites set
mode 3 after calls to `0x80056200` / `0x80056114`.

**Two things worth knowing.** First, there is a silent fallback ladder — `SD
tuning Cmd fail, set 100MHz` → `set 50MHz` → `set 25MHz`. A card that fails
the 1.8 V switch or CMD19 tuning quietly runs at half or a quarter of the bus
rate with no user-visible indication, which plausibly explains more real-world
speed variance than anything else.

Second, at `0x80078474`:

```
lw   $v0, -0x3efc($gp)
andi $v0, $v0, 0x1000
bnez $v0, 0x80078558     ; skip the entire high-speed setup
```

A config bit that **bypasses high-speed selection altogether**. Its runtime
default could not be resolved statically — it is a `gp`-relative global in
DRAM (`gp = 0xaf8032c0`, runtime RAM, not in the image). Check it on the
console before considering a patch.

## Video encoder

The H.264 encoder is parameterised and the parameters are exposed as debug
console arguments in `private/cmd_media.c`:

```
w=  h=  fps=  vidcodec=  264type=  264gop=  264qp=  264i=  264ms=
264trans=  264prof=  bitrate=  adbitr=  hz=  prof=  bit=  codec=
```

with `264qp: %s ini/max/min: %d/%d/%d` and `264prof: Profile: %d (0:base/main,
1:high)`. There is a per-resolution getter,
`View_GetVideoBitRate(VIDEO_SIZE_1080P_10)`.

These being console-settable matters: encoder changes can be tested live over
UART before anything is patched into the image.

Recording length is bounded by the 4 GB file limit with a seamless
continuation mechanism (`MEDIA_REC_SEAMLESS_*`, `mov_seamlessStreamBackupDo`),
configured by `[seamless] SlotType:%d, SlotSize:%d(ms or KB), spaceThr:%d KB,
bitRate:%d KB`. No artificial recording-duration cap was found.

## Runtime hooks that need no firmware change

- **`D:\ALC.TXT`** — the camera loads audio automatic-level-control parameters
  from the SD card at boot (`sp5kMediaAlcParamFileLoad`, "Load \"%s\" as Audio
  ALC parameter file"). Same format as `RO_RES/ALCSTR.TXT`. A vendor-provided
  tuning hook requiring no flash at all.
- **Debug console** (`private/cmd_*.c`, over UART) — memory peek/poke, raw
  I²C, filesystem shell, OS introspection, clock-tree dump, PLL override, plus
  a disk benchmark (`Disk speed(KB/s) measure dev:%lu, period:%lu(ms)` with a
  per-operation read/write table) and the media encoder parameters above.

## Settings enumeration

37 `VIEW_ITEM_*` capture/view settings exist, including `RAW_JPEG` (exposed in
the UI as "RAW + JPEG"), `ISO_AUTO_MAX`, `AEB_EV`, `VIDEO_STABILIZER`,
`PRERECORD`, `PANORAMA_DEGREE`, `TIME_LAPSE`.

The ISO menu has 10 entries: Auto, 125–6400, 12800 and 25600. The builder
case for it (`0x803d2a08`) creates a 10-item menu. `ISO 12800`/`ISO 25600`
(UI strings 841–842) and Maximum ISO Auto 3200–12800 (844–846) sit at the end
of the string table, which fits the manual's note that V1.08 added them. The
manual lists the same options. An earlier version of this section said the
menu stopped at 6400; that was wrong.

## Key dispatch

### Key message table

Buttons reach the UI as 32-bit message IDs. The firmware carries its own
name table for them at **`0x80ac9b3c`** (main firmware, file offset
`0xac9b3c`). It sits directly before the `JOB`/`LOP` keywords
(`0x80ac9cb8`) of the `LT_DPC.BIN` key-script format, which is probably what
consumes it; nothing in the code references it with `lui`/`addiu`. Rows are
16 bytes:

```c
struct key_name_row {      /* 0x80ac9b3c, 24 rows, no terminator */
    u32 index;             /* 0, 1, 2 … */
    const char *name;      /* "MSG_KEY_SNAP1" … */
    u32 press_msg;         /* message posted on press */
    u32 release_msg;       /* on release; 0 if the key has none */
};
```

| # | Name | Press | Release |
|---|---|---|---|
| 0 | `MSG_DELAY` | `0x00000000` | `0x00000000` |
| 1 | `MSG_KEY_SNAP1` (half-press) | `0xf0000002` | `0xf0000022` |
| 2 | `MSG_KEY_SNAP2` (full press) | `0xf0000001` | `0xf0000021` |
| 3 | `MSG_KEY_UP` | `0xf0000003` | `0xf0000023` |
| 4 | `MSG_KEY_DOWN` | `0xf0000004` | `0xf0000024` |
| 5 | `MSG_KEY_LEFT` | `0xf0000005` | `0xf0000025` |
| 6 | `MSG_KEY_RIGHT` | `0xf0000006` | `0xf0000026` |
| 7 | `MSG_KEY_MENU` | `0xf000000d` | `0xf000002d` |
| 8 | `MSG_KEY_SET` | `0xf0000007` | `0xf0000027` |
| 9 | `MSG_KEY_TELE` | `0xf0000008` | `0xf0000028` |
| 10 | `MSG_KEY_WIDE` | `0xf0000009` | `0xf0000029` |
| 11 | `MSG_KEY_PLAYBACK` | `0xf000000c` | `0xf000002c` |
| 12 | `MSG_KEY_VIDEO` | `0xf000000b` | `0xf000002b` |
| 13 | `MSG_KEY_LOCK` | `0xf000000a` | `0xf000002a` |
| 14 | `MSG_KEY_WIFI` | `0xf000000e` | `0xf000002e` |
| 15 | `MSG_KEY_POWER_OFF` | `0xf00000a1` | `0xf00000a1` |
| 16 | `MSG_MODEKEY_AUTO_SCENE` | `0xf0000061` | — |
| 17 | `MSG_MODEKEY_PROGRAM` | `0xf0000068` | — |
| 18 | `MSG_MODEKEY_SCENE` | `0xf0000062` | — |
| 19 | `MSG_MODEKEY_ART` | `0xf0000064` | — |
| 20 | `MSG_MODEKEY_BURST` | `0xf000006b` | — |
| 21 | `MSG_MODEKEY_VIDEO` | `0xf0000069` | — |
| 22 | `MSG_DEBUGMSG_LENS` | `0xf0000000` | — |
| 23 | `MSG_ENTER_TEST_MODE` | `0xf2000012` | — |

Not every row is a DC2000 button. The manual lists the physical controls as
Power, Shutter (half press = `SNAP1`, full = `SNAP2`), Menu, Playback, Movie
(`VIDEO`), Wireless (`WIFI`), OK (`SET`), the 4-way pad and an 8-position mode
dial (P, Intelligent Scene, Land Scene, Panorama, M, Underwater, A, S). The
camera has no zoom lever or lock key, so `TELE`, `WIDE` and `LOCK` appear to be
platform leftovers. The `MSG_MODEKEY_*` rows (Auto Scene, Program, Scene, Art,
Burst, Video) don't match this dial either. Digital zoom is driven from
Up/Down once the Zoom shortcut is enabled.

Release is always press + `0x20` for the physical keys. The ordering of the
low byte is not the row order (`SNAP2` is `01`, `SNAP1` is `02`), so look IDs
up in the table rather than deriving them. `MSG_KEY_WATER` exists as a string
(`0x8091d08c`) but has no row here.

To find every handler for a key, search for the press ID. Handlers are
compare trees (`lui $x, 0xf000` followed by `addiu`/`sltu`/`beq`), not jump
tables, so `sunp_code.py xref --addr` will not find them. The reliable search
is a `lui 0xf000` paired with `addiu …, 0x0e` inside a short window.

### Common fallback: `ProcessDialModeChange`

Each UI mode has its own key handler. About 40 of them tail-call
**`ProcessDialModeChange` (`0x8040e35c`)** for any key they don't consume, so
it is the single place where a key gains a camera-wide meaning. For
`MSG_KEY_WIFI` it does this (`0x8040e3f8`–`0x8040e490`):

1. return unless `uwMode` (`0x80ab7c64 + 0x75e`) is exactly `0x2010`, the
   still-capture live view;
2. return unless the phone app is connected (see below);
3. return if `0x801fcdd8(0xc00)` reports busy;
4. if a function OSD is open (bit `0x400` of `*[0x80ab7c1c]`), close it via
   `0x803b0858(0)`;
5. tail-call `ViewMenu_WiFiAutoSendMenuCreate` (`0x803d5610`), the
   "Auto Send" On/Off menu.

### What the Wi-Fi button does

The Wi-Fi key does **not** switch Wi-Fi on or off. It only does something
while the Link123 app is connected. The manual agrees: it calls this the
"Wireless Auto Send button", tells users not to press it when connecting, and
describes it only for the app's Camera mode.

| Context | Handler | Effect |
|---|---|---|
| Still live view, app connected | `0x8040e35c` | Opens the Auto Send menu |
| Single-image playback, app connected | `0x8048cc78` → `0x8048de18` | Enters Quick Send mode (`uwMode = 0x1151`), which shows the Quick Send icon. Per the manual, OK then sends the displayed image |
| Quick Send | `0x8048fe2c` → `0x804909b0` | Leaves Quick Send |
| "Download GPS Data" Wi-Fi screen | `0x804d3a6c` → `0x804d3e90` | Exits and stops the soft AP (`WifiNetLib_Ap_Disable`) |
| Factory key test | `0x8045fb04` → `0x80460348` | Registers key bit `0x40000` |
| Anything else, or app not connected | — | Nothing |

The view message handler at `0x804108b0` also re-injects `MSG_KEY_WIFI` into
`ProcessDialModeChange` after the post-capture review closes. It does this
when the byte at `0x80c60548` is set, which happens during an app
remote-control session (set by `ProcessRemoteCtrlSwitchMode` via `0x803c7fdc`,
cleared via `0x803c80f8`).

**App-connected flag.** `[0x80ad9948] + 0x11d` is set to 1 on
`REMOTE_WIFI_HCE_MSG_GENERAL_APP_FOUND` (`0x80420194`) and cleared on
`APP_NOT_FOUND`, `WIFI_SYSTEM_DISABLE` and `LOST_FROM_HTTP`.

**How Wi-Fi actually starts.** `MSG_UI_ENABLE_APP_WIFI` (`0xf400000b`,
handled at `0x8040bf38`) is posted when a phone connects over Bluetooth LE
(`0x8041f9ac`, after `REMOTE_BLUETOOTH_BLE_DEVICE_CONNECTED`). It is also
posted from `ViewCreate` (`0x803b4274`) to resume a pending session.
`DISABLE` is `0xf400000c` and `TIMEOUT` is `0xf400000d`.

### Shortcut menus

The Setup menu's "Shortcut" option (UI strings 883–895) assigns one of 11
functions to the **OK** button in record mode. The default is Digital Zoom.
The manual says the shortcut is only available in Program and UW modes; in
the other dial modes OK opens that mode's own sub-menu. The code path can be
reused from any key handler:

- **`View_ShortcutFunc(byShortcut)`** (`0x803b6c70`) switches on
  `byShortcut − 1` through the table at `0x80911454`. Each case checks a
  per-mode "allowed" bit in `*[0x80ab7c5c]` and then calls
  `ViewMenuGoSubMenuDirect`. If the bit is clear it does nothing.
- **`ViewMenuGoSubMenuDirect(byShortcut)`** (`0x803d3a28`) first calls
  `0x803d0730`. That sets the menu type `*[0x80ab7c58] + 0x22` to `0x10` and
  builds the 28-item still menu. It then maps the shortcut through a stack
  table (`nShortcutMenuTbl`) to a view-menu item, opens it with `0x803d39b0`,
  and sets `uwMode = 0x2100`. Because the type is forced to `0x10`, the item
  numbers below hold in every dial mode.
- The view-menu builder `0x803d21cc` dispatches on that item through a
  28-entry jump table at `0x809149b8`.

| byShortcut | Function | Allowed bit | Menu item |
|---|---|---|---|
| 1 | Focus Mode | `[1] & 0x40` | — |
| 2 | Self-timer | `[1] & 0x01` | — |
| 3 | EV Compensation | `[2] & 0x80` | — |
| 4 | White Balance | `[0] & 0x10` | `0x0d` |
| 5 | **ISO** | `[0] & 0x04` | `0x0f` (`View_DrawIsoMenu`, `0x80465ab8`) |
| 6 | Metering | `[0] & 0x40` | `0x11` |
| 7 | Exposure Bracketing | `[3] & 0x20` | `0x12` |
| 8 | AF Area | `[3] & 0x04` | `0x13` |
| 9 | AE/AF Lock | — | `0x15` |
| 10 | Digital Zoom | — | `0x16` |
| 11 | Grid Line | — | `0x1a` |

Shortcuts 1–3 are handled by dedicated code (cases at `0x803b6d68`,
`0x803b6db0` and `0x803b6ddc`) instead of a sub-menu, so the menu-item column
is empty for them. Rows 9–11 have special-case gating that was not decoded.
The manual's list names 10 options plus Off but leaves out AE/AF Lock, which
it describes separately as a shortcut added in V1.08. With AE/AF Lock that
gives 11, matching the table.

The user's choice is stored in **`[0x80ab7c60] + 0x3e`**. The view key handler
reads it and tail-calls `View_ShortcutFunc(+0x3e, 0)` (`0x803baba0` →
`0x803bafd0`). `[0x80ab7c60] + 0x3c` is `nAsmAdjusting`, which is non-zero
while aperture or shutter is being adjusted in A/S/M. Both names come from the
debug print in `ViewMainMode_ProcUpKey_VSF010W` (`0x803b64e8`). An earlier
version of this section swapped the two fields.

**Where ISO is allowed.** `0x8038d924(viewMode, scene, mask)` recomputes the
mask whenever the mode changes. Emulating it with Unicorn for view modes 0–15
and scenes 0–63, the ISO bit (`[0] & 0x04`) is set for view modes 4–8, depends
on the scene in mode 1, and is clear everywhere else. The bit never depends on
the mask's previous value. Modes 4–6 are the A/S/M modes: the view key handler
routes keys to the aperture/shutter adjuster when `gsUI + 0x766` is 4–6 and
`nAsmAdjusting` is set. Matching the manual's per-mode menu access, 7–8 are
probably P and UW, and 1 Land Scene. That mapping is inferred, not read from
a table.

### Version numbers

The version is stored in three independent places, all as BCD-style hex
(`1.09` = major `0x01`, minor `0x09`, sub `0x00`):

| Copy | Where | Used by |
|---|---|---|
| Firmware ID string `v0x01.0x09.0x00` | mainfw `+0xac4` | the card updater, via `sscanf("v0x%X.0x%X")` |
| `addiu $v1, $zero, 0x0900` stored to `0x80c6118c` (major from `App_PreBootProc`'s argument) | `0x801c5810` | update-screen "current version" (`%X.%02X`), photo EXIF Software tag (`%x.%04x`), USB/PTP DeviceInfo, calibration screen |
| HCE version: literal `1, 9, 0` | `0x801c40b0` / `0x801c472c` / `0x801c4028` | the **startup-screen** `v%X.%02X%02X` at bottom right (the place the manual tells users to read the version), the version string sent to the Link123 app, and the boot log |

A version bump has to change all of them. `sunp_tool.py check` checks that
the second and third copies agree with the string.

## What is and is not tunable

Realistically improvable, in rising order of risk:

1. **Resource data** — colour (`3DLUT*`, `RGAMA`/`GGAMA`/`BGAMA`, `HC*`,
   `AWBCTT`), lens corrections (`LDC`, `CA`), art filters (30 `LOMO*`), UI
   strings/icons/sounds, boot defaults (`UIDATA`/`BK_UI`), USB identity
   (`USBSTR.TXT`) and UVC descriptors (`USBUVC.BIN`). All handled by the
   overlay workflow; a bad table gives bad output, not a brick.
2. **Code patches** — encoder bitrate/QP defaults, the `0x1000` speed-bypass
   bit. Fixed-length edits only.

Not reachable from firmware: resolution and framerate beyond 1080p60 (no 4K
anywhere in the image), the lens, housing and flash hardware, and card
throughput beyond the negotiated UHS mode. RAW already exists, so there is
nothing to unlock there.

## Safety

No recovery path has been established for a bad flash on this model, and the
per-unit sensor calibration (`BADPIX`, `DEFPIX1`, `DEFTPXL0/1`, `LSC`, …) ships
blank in the update image and exists only on the camera. Take a full flash
backup over the USB vendor ISP path (`usbVNDRReq*`) before the first write of
any kind.
