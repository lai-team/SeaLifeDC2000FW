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

One caution: string tables contain `ISO 12800` and `ISO 25600` while the menu
exposes 125–6400. Unused strings are common in these ODM tables — the usable
range is set by the sensor driver and the ISO-indexed noise-filter tables, not
by the presence of a string. Do not read that as an unlockable feature.

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
