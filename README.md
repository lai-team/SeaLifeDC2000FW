# SeaLife DC2000 firmware tooling

Three tools for iCatch **"SUNP BURN FILE"** camera firmware — the
`FPUPDATE.DAT` files these cameras read off an SD card to update themselves.

| Tool | Scope |
|---|---|
| [`sunp_tool.py`](sunp_tool.py) | The container: unpack, repack, FAT volumes, and a baseline+overlay build workflow |
| [`sunp_assets.py`](sunp_assets.py) | The resources: `FIF1` archives, sprite sheets, string tables, record wrappers |
| [`sunp_code.py`](sunp_code.py) | The MIPS blobs: cross-references, disassembly, `gp`-relative globals |

Findings from reading the code — the storage stack, SD bus speed, the video
encoder, and what is actually tunable — are in
[docs/firmware-internals.md](docs/firmware-internals.md).

Developed against the SeaLife DC2000 image
`sha256:9beffb33d8b930da160aeb5443b94089881d97128da2d17402b4edb3afbe61ed`
(47,739,310 bytes). **The firmware image itself is not in this repo** — it's
SeaLife's, and it embeds third-party code (a signed MAGIX installer, a Broadcom
Wi-Fi blob, licensed Morpho and ArcSoft libraries). Get it from the vendor's
firmware-update download, or off a camera.

Everything below was derived by parsing the image, not from vendor
documentation, so treat it as an accurate description of *one* firmware rather
than a specification.

## Versioned modification

Because the vendor image can't be committed, modifications are tracked as a
**pinned baseline plus a small overlay of changes**:

- `baseline.json` records the image's SHA-256, section layout, and a per-file
  inventory of both volumes. Committed.
- `overlay/` holds only what differs — replaced files, added files, deletions,
  and byte patches. Committed. See [overlay/README.md](overlay/README.md).
- `build` combines the two into a modified image. Deterministic: the same
  baseline and overlay always produce the same bytes, so a given commit
  identifies an exact firmware image without the image being in git.

```sh
./sunp_tool.py baseline FPUPDATE.DAT        # pin the image (once)
# ... drop files into overlay/files/A/..., write overlay/patches/mainfw.patch
./sunp_tool.py status                       # what would change
./sunp_tool.py build --verify -o out.DAT    # build, fsck, and check reproducibility
```

`build` refuses to run if the baseline image doesn't hash to the pinned value,
verifies every carved section against its recorded hash, verifies each patch's
expected bytes before writing, runs `fsck` on the resulting volumes, and with
`--verify` builds twice and compares. An empty overlay reproduces the baseline
byte-for-byte.

Before writing anything, `build` runs the same pre-flight checks as
`check IMG`. The output is not written if any of these fail:
- the header balancing checksum (the file word sum must be 0);
- the firmware ID string, parsed the way the camera's updater does;
- section layout identical to the baseline;
- the flasher, HDR 1/2/3 and the DRAM-timing sections byte-identical to the
  baseline;
- the main-firmware reset and startup region (`0x0`–`0xac0`) untouched.

It also warns if the version string and the running-version constant
disagree, and lists every main-firmware byte range that changed.

## Commands

```
info    IMG                 parse and print the container layout
unpack  IMG [-o DIR]        split into sections + extract both FAT volumes
repack  DIR [-o IMG]        rebuild from an unpacked dir, recomputing offsets
baseline IMG                pin an image as the build baseline
status                      show what overlay/ changes vs the baseline
build   [--verify] [-o IMG] baseline image + overlay/ -> modified image
check   IMG                 pre-flight safety checks (run automatically by build)
ls      VOL                 list a FAT volume image
get     VOL PATH DST        copy a file out
put     VOL PATH SRC        replace or add a file
rm      VOL PATH            delete a file
mkdir   VOL PATH            create a directory
fsck    IMG|VOL             check FAT consistency
```

`unpack` is for exploration: it writes `sections/` (raw blobs), `files/A` and
`files/B` (extracted trees), and `manifest.json`, then repacks in memory and
asserts the result is byte-identical to the input.

Reading the code (`sunp_code.py`):

```
xref  SECTION (--string S | --addr VA)   find code that references it
dis   SECTION --addr VA [--count N]      disassemble, annotating string refs
gp    SECTION --offset OFF               find accesses to a gp-relative global
```

`xref` and `gp` are pure struct parsing. `dis` needs capstone:
`python3 -m venv /tmp/capenv && /tmp/capenv/bin/pip install capstone`.

> Under `unpack`, `files/` is a **reference copy** — `repack` builds from
> `sections/` only, so edits there are ignored. Use `put`/`rm` on a section
> image, or the overlay workflow.

Python 3.8+, no dependencies.

## Resource formats (`sunp_assets.py`)

The resource volume is not a pile of opaque `.BIN` blobs — nearly all of it is
one of a handful of iCatch/Sunplus formats, and they nest.

```
sunp_assets.py identify unpacked/files/A/RO_RES
sunp_assets.py extract  unpacked/files/A/RO_RES -o assets
sunp_assets.py embedded unpacked/sections/06_mainfw.bin -o embedded
```

Of 259 files, 256 are identified; `extract` recursively unpacks the archives
and decodes sprite sheets to PNG and string tables to text.

| Format | Files | Status |
|---|---|---|
| RIFF `SPFN` sprite sheets (`.SFN`) | 66 | decoded → PNG + glyph index |
| UTF-16 / plain text (`.txt`, `LT_DPC.BIN`) | 67 | plain text already |
| RIFF `SPST` string tables (`.SST`) | 32 | decoded → `id<TAB>text` |
| JPEG | 32 | standard |
| Record wrapper `0x18ce` / `0x18ba` | 35 | header parsed, payload extracted |
| `FIF1` archives | 8 | decoded → member files |
| `FDL Font` (CJK/Thai outlines) | 8 | third-party, **not decoded** |
| Blank calibration placeholders | 4 | all-`0x00`/`0xFF` |
| RIFF/WAVE, USB descriptors | 4 | standard |

### `FIF1` archive

8-byte wrapper, then `FIF1`, `u32 count`, then `count` records of
`char[12] name` + `u32 size`. Member payloads follow, each 16-byte aligned.
Some archives (BOOTIQ, BOOTLIQ) place an all-zero 16-byte terminator after the
name table and others do not, so the tool detects it rather than assuming.

Members are the ISP tuning sets (`3DLUT*`, `RGAMA*`, `WDR*`, `YGAMMA*`, …),
the face-detection databases (`FDDB` magic), and the UI defaults.

### RIFF `SPFN` sprite sheets

Standard RIFF with chunks `mark`, `fshp`, `fplt`, `comp`, `dsgn`.

```
fshp:  u16 width, u16 height, u16 flags (low byte = bits per pixel),
       u32 bytes-per-glyph, u16 glyph count,
       then per glyph: u16 charcode, u16 advance width, height*stride rows
       bytes-per-glyph == 4 + stride*height   (reproduces every chunk size)
fplt:  u16 colour count, then count*3 bytes RGB
```

Pixel values index the palette; index 0 is transparent in every sheet checked
(the icons are drawn over live video), and palette entry 0 holds a non-colour
marker `#800000`. Fonts carry real charcodes — `ENGSMALL.SFN` is a 76-glyph
proportional ASCII font, `D28X44.SFN` the monospaced date-stamp digits.

### RIFF `SPST` string tables

```
strn:  u16 language id, u16 unknown, u16 count,
       then per string: u16 id, u16 byte length, UTF-16LE (no terminator)
```

`ENGLISH.SST` holds 914 entries; id 0 is `Language`, id 1 is `English`.

### Record wrappers and their sub-blocks

Single tuning tables are wrapped in a small header, two variants:

```
0x18ba:  u16 tag, u16 0x18ba, u16 flags, u16 payload size          (8 bytes)
0x18ce:  u16 tag, u16 0x18ce, u16 flags, u16 ?, u32 payload size,
         u32 checksum field                                        (16 bytes)
```

The payload is itself a `char[4] tag + u32 size` TLV. All 46 `0x18ce` records
in the image are consumed exactly by it, using two tags: `PAR0` (parameters,
31 records) and `DAT0` (the table itself, 46). The firmware's own IQ dump
routine references both, next to a `D:\DUMP%d\%s.BIN` format string.

**On the checksum field:** it is *not* CRC-32. Ruled out by sweeping poly,
init, reflection and xorout across a catalogue of 32-bit CRCs plus FNV, djb2,
sdbm, Jenkins and Adler, over the payload, the whole record, and every
sub-range of the smallest record. A standard reflected CRC-32 table does exist
at `mainfw+0xa43f18`, but no range reproduces the field.

More usefully, **nothing appears to verify it**. Every `checkSum`/CRC string in
either MIPS blob belongs to NAND reserved-block headers, eMMC boot areas, the
DRAM parameter area or the SD/SDIO layer — none to these records. So editing a
tuning record probably does not require fixing the field. That is inference
from strings, not proof from disassembly; treat it as likely rather than
certain. Records with identical payloads do share the value, but since those
are byte-identical copies that is equally consistent with a content hash or an
authoring-tool tag.

### Archives compiled into the firmware

The main firmware carries four more `FIF1` archives of fallback resources,
used when the volume copies are missing:

```
sunp_assets.py embedded unpacked/sections/06_mainfw.bin -o embedded
```

| Offset | Members |
|---|---|
| `0x00879c80` | `AGC.BIN`, `EXPOSURE.BIN` |
| `0x0087e520` | `DISPGAM0`, `DISPGAMA`, `DISPMAX`, `NTSCMAX`, `PALMAX` |
| `0x0087ec40` | `AALUT`, `CDSP6350`, `EDGEGLUT`, `HSC` |
| `0x0087f1e0` | `BEEP1.WAV`, `SHUTTER1.WAV` |

### Other decoded pieces

`USBUVC.BIN` is a raw USB configuration descriptor set, decoded by the tool to
a readable listing: a composite device exposing a **UVC webcam** (two video
streaming interfaces, 7 bandwidth alt-settings each) plus a **UAC audio**
function, 100 mA bus powered.

`LT_DPC.BIN` is a plain-text key/timing script (`<JOB> MSG = MSG_KEY_RIGHT
TIME = 1000ms </JOB>`, wrapped in `<LOP CNT=10000>`) — a factory life-test
sequence driving the camera's own key events.

The `DRAMPAR1` section holds four blocks. Three of them are DRAM timing
profiles: after a 4-byte block header, a flat list of `u16 register, u8 value`
writes over the `0x70xx` controller space (`0x7050=0xa0`, `0x7054=0x2a`, …),
95–98% of addresses landing in plausible register ranges. The fourth and
largest block writes a different space (`0x00xx`/`0x02xx`) and its framing is
less certain.

### Still opaque

- **`FDL` fonts** (8 files, `FDL Font` magic) — a third-party CJK/Thai outline
  format. Not decoded.
- **IQ coefficient arrays** — extracted as `DAT0`/`PAR0` blocks, but the field
  meanings inside are unknown. The `3DLUT*` tables are 760 four-byte entries
  whose fourth byte is always zero, i.e. RGB triplets padded to 4 bytes; the
  lattice geometry did not resolve (760 is not a cube, and a 124-byte header
  before a 9³ grid fits the arithmetic but is not supported by the data —
  these are stylised art LUTs, so smoothness cannot discriminate).
- **The `0x18ce` checksum algorithm**, as above.
- `AECFG.BIN` (63 B) and `AWBCTT.BIN` (80 B) — small unwrapped tables.

Notably, `BADPIX.BIN`, `DEFPIX1.BIN`, `DEFTPXL0.BIN` and `DEFTPXL1.BIN` ship
entirely blank — they are four of the seven files HDR 3 preserves, so the real
per-unit calibration lives only on the camera and is restored over these.

## The platform

| | |
|---|---|
| SoC | iCatch **CA6350A** ("V35" family), MIPS32 4Kx, little-endian |
| RTOS | ThreadX G4.0c.4.0 (Express Logic), MIPS32_4Kx/GNU build |
| Sensor | Sony **IMX183**, 1", 20 MP (max still 5472×3648) |
| Wi-Fi | Broadcom **BCM43430A1**; dongle firmware 7.46.3.0 (2015-05-26) embedded and pushed over SDIO by a ported DHD |
| Bluetooth | BCM43438A1 / BCM2049B0; BLE name `PICO-<MAC>` |
| USB | VID `0x0733`, "SEALIFE DC2000" — MSDC `0x0097`, PTP `0x0096`, PC-cam `0x7251` |
| Firmware ID | `VQSD_BRN:VSF010W v0x01.0x09.0x00@HW_VSF010W_B` → v1.09.00 |
| Built | Oct 2 2019 09:11:26, on Windows/Cygwin under `d:\VSF010W_WIFI_Local\vc83\` |

## Container format

Sections tile the file exactly — there is no padding and no gap.

| Offset | Length | Section |
|---|---|---|
| `0x00000000` | `0x200` | Container header |
| `0x00000200` | `0x098E00` | "DRAM CODE" — ISP / card-update loader (MIPS, load base `0x80040000`) |
| `0x00099000` | `0x001000` | `SUNP BURN HDR 1` — partition sizes |
| `0x0009A000` | `0x000120` | `SUNP BURN HDR 2` — disk descriptors (XOR-obfuscated) |
| `0x0009A120` | `0x2100000` | Disk A — FAT16, OEM `SUNPLUS` |
| `0x0219A120` | `0x0100000` | Disk B — FAT12 |
| `0x0229A120` | `0x0AECA00` | Main firmware (MIPS, load base `0x80000000`) |
| `0x02D86B20` | `0x000330` | `SUNP BURN HDR 3` — files preserved across an update |
| `0x02D86E50` | `862` | `DRAMPAR1` — DRAM timing tables |

### Container header (`0x200` bytes)

```
0x00  char[16]  "SUNP BURN FILE\0\0"
0x10  u32       total file size
0x14  u32       offset of HDR 1
0x18  u32       offset of HDR 2
0x1C  u32       offset of the main firmware
0x20  u32       0 (unknown/unused in this image)
0x24  u32       offset of HDR 3
0x28  u32       offset of DRAMPAR1
```

### HDR 1 — partition sizes

Magic `SUNP BURN HDR 1\0`. Mostly zero. `+0x74` and `+0x78` hold the byte sizes
of disk A and disk B (stride 4 per disk, though only two disks are attested).
`+0x20` = 5, `+0x40` = `0xFFFFFFFF`, `+0x80` = `+0x90` = 1; purpose not
established.

### HDR 2 — disk descriptors

Magic `SUNP BURN HDR 2\0`, descriptor count at `+0x10`. Descriptors start at
`+0x20`, are `0x80` bytes each, and **the descriptor area is XOR-obfuscated with
`0x7A`** (the magic and count are plaintext). Decoded, each descriptor is:

```
+0x00  char[]  tag, e.g. "060916:build<sign>a?"   (…b? for the second disk)
+0x70  u32     absolute file offset of the disk image
+0x74  u32     size of the disk image
```

### HDR 3 — files preserved across an update

Magic `SUNP BURN HDR 3\0`, count at `+0x10`, then `0x70`-byte entries from
`+0x20`, each a NUL-terminated DOS path. In this image, seven per-unit sensor
calibration files the updater backs up and restores:

```
A:\RO_RES\COLD.BIN        A:\RO_RES\DEFTPXL0.BIN    A:\RO_RES\DEFTPXL1.BIN
A:\RO_RES\DEFPIX1.BIN     A:\RO_RES\BPCAP.BIN       A:\RO_RES\LT_DPC.BIN
A:\RO_RES\LSC.BIN
```

### Integrity

There is **no encryption and no signature**, but there **is** one
container-level checksum. The u32 at header `+0x1fc` is a balancing word: the
little-endian u32 sum of every full word in the file (the 2-byte tail is
ignored) is exactly zero. The vendor image sums to `0x00000000`, which cannot
be chance. No code that checks it was found in the ISP loader or in the main
firmware's update entry (`0x804dfe1c`). Something else may check it, such as
the boot ROM or the vendor PC tool, so `assemble()` always recomputes it and
`sunp_tool.py check` fails any image that doesn't sum to zero.

The other `checkSum`/CRC strings in the ISP loader belong to structures it
*writes* to flash (SPI/NAND reserved-block headers, eMMC boot areas, DRAM
PARA). Those checksums are computed from the data being written, not taken
from `FPUPDATE.DAT`. The firmware is written by `spiFwWrite` and then read
back and compared (`Fw verify...`).

The camera-side updater (`0x80431180`) validates:
- the magic string;
- the `VQSD_BRN:` prefix and 7-character project tag (`VSF010W`);
- `sscanf("v0x%X.0x%X")`, which gives card version `(major << 16) | minor`.

The only other gate is low battery. It shows both versions and then asks; it
does **not** require the card version to be newer, so downgrades are allowed.

A repack therefore has to get the offsets right and rebalance the checksum.
The tool recomputes header pointers, HDR 1 disk sizes, the XOR'd offset/size
pairs in HDR 2, and `+0x1fc`.

### FAT quirks in the vendor volumes

Both volumes declare two FATs but **only FAT copy 0 is populated** — copy 1
holds just the media-descriptor signature and is otherwise zeroed, so the
camera evidently reads copy 0 only. All directory entries have zero timestamps
and zero NT case flags; mixed-case and long names use proper LFN chains with
`~1` short names.

The tool follows whatever convention a volume already uses: it maintains a
spare FAT copy only if that copy currently mirrors copy 0, and it writes zero
timestamps. That keeps a modified image as close to the known-good baseline as
possible, and makes builds deterministic.

## Load addresses

Both MIPS blobs are flat: **file offset = virtual address − load base**.

- Main firmware → `0x80000000`
- ISP / card-update loader → `0x80040000` (matches its first instruction, `lui $k0, 0x8004`)

Verified empirically: pointer words in each blob resolve to real strings at the
implied offsets. They load straight into Ghidra/IDA as raw MIPS32 little-endian
at those bases.

## Volume contents

**Disk A `/RO_RES/`** — the read-only resource volume: icon and font sprite
sheets (`.SFN`), CJK/Thai/Mideast fonts (`.FDL`), 29 language string tables
(`.SST` plus UTF-16 `.txt`), UI JPEGs, startup WAVs, ISP tuning blobs
(`IQ/APPIQ.BIN`, `LENIQ.BIN`, 30 `LOMO*.BIN` art filters), and the sensor
calibration listed in HDR 3.

**Disk A `/RAMDISK/MAGIX.EXE`** — a Windows 32-bit PE, a MAGIX Setup
Self-Extractor built 2015-11-03, Authenticode-signed by MAGIX Software GmbH.
The camera exposes it as a RAM disk over USB. It's the only high-entropy region
in the image.

**Disk B `/USER_RES/`** — user-writable: custom startup image and UI data.

## Update mechanism

The loader reads `D:\FPUPDATE.DAT` from SD, checks the header magic, locates the
project/version tag, backs up the HDR 3 calibration files, writes the disk
images, firmware and DRAM parameters, verifies each by read-back, then
auto-powers off. It supports NAND, SPI NOR and eMMC targets. There is also a USB
vendor-request ISP path (`usbVNDRReq*`) for raw flash read/write.

## Status and caveats

Verified against the baseline image:

- Round-trip of an untouched image is byte-identical, and an empty overlay
  builds back to the baseline SHA-256.
- Replace, add, delete and mkdir are exercised on both FAT16 and FAT12, with
  every untouched file in both volumes confirmed byte-identical afterwards.
- Results cross-checked against an independent FAT implementation: the built
  volumes mount under macOS, long names read back correctly, and `fsck_msdos`
  reports no errors.

Known limits:

- No rename; no recursive directory delete.
- FAT12/FAT16 only. The firmware's own `fmt` command advertises FAT32 and
  exFAT, so other models in this family may not parse.
- Patches are fixed-length in-place edits. Changing a section's size would
  shift everything after it, which the container header handles, but absolute
  addresses inside the MIPS firmware would not survive it.
- **Flashing a modified image can brick the camera.** Nothing here has been
  tested on hardware. Whether the ISP loader can recover a corrupt main
  firmware has not been established — take a full flash backup over the USB
  vendor ISP path first.

Not affiliated with SeaLife, iCatch, or any vendor named here.
