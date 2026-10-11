# SeaLife DC2000 firmware tooling

`sunp_tool.py` unpacks and repacks iCatch **"SUNP BURN FILE"** camera firmware
images — the `FPUPDATE.DAT` files these cameras read off an SD card to update
themselves.

Developed against the SeaLife DC2000 image
`sha256:9beffb33d8b930da160aeb5443b94089881d97128da2d17402b4edb3afbe61ed`
(47,739,310 bytes). **The firmware image itself is not included in this repo** —
it's SeaLife's, and it embeds third-party code (a signed MAGIX installer, a
Broadcom Wi-Fi blob, licensed Morpho and ArcSoft libraries). Get it from the
vendor's firmware-update download, or off a camera.

Everything below was derived by parsing the image, not from vendor
documentation, so treat it as an accurate description of *one* firmware rather
than a specification.

## Usage

```
sunp_tool.py info   IMG              parse and print the container layout
              unpack IMG [-o DIR]    split into sections + extract both FAT volumes
              repack DIR [-o IMG]    rebuild, recomputing every offset/size field
              ls     VOL             list a FAT volume image
              get    VOL PATH DST    copy a file out of a FAT volume image
              put    VOL PATH SRC    replace a file inside a FAT volume image
```

Typical edit cycle:

```sh
./sunp_tool.py unpack FPUPDATE.DAT -o unpacked
./sunp_tool.py put unpacked/sections/04_volA.img /RO_RES/JPG/SEAL.JPG new.jpg
./sunp_tool.py repack unpacked -o FPUPDATE.new.DAT
```

`unpack` writes `sections/` (the raw blobs), `files/A` and `files/B` (extracted
trees), and `manifest.json` (offsets, per-section SHA-256, disk descriptors,
preserved-file list). It then repacks in memory and asserts the result is
byte-identical to the input.

> `files/` is a **read-only convenience copy**. `repack` builds from
> `sections/` only — edits made under `files/` are ignored. Use `put` to change
> a volume's contents.

Python 3.8+, no dependencies.

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
of disk A and disk B. `+0x20` = 5, `+0x40` = `0xFFFFFFFF`, `+0x80` = `+0x90` =
1; purpose not established.

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

There is **no encryption, no signature and no container-level checksum**. Every
`checkSum`/CRC string in the ISP loader belongs to structures it *writes* to
flash (NAND reserved-block headers, FW0/FW1 areas, DRAM PARA), not to
`FPUPDATE.DAT`. The updater validates only: the magic string, a
`SD_BRN_PREFIX` / `SECRET_PATTERN` project tag, a version comparison, and a
read-back verify after each write. So a repack only has to get the offsets
right — which is what `repack` recomputes (header pointers, HDR 1 disk sizes,
and the XOR'd offset/size pairs in HDR 2).

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

- Round-trip of an untouched image is byte-identical.
- `put` is exercised on FAT16 grow/shrink and FAT12 grow, with every other file
  in both volumes verified unchanged afterwards, and the written file read back
  before the image is committed to disk.
- `put` can only **replace** existing files — no add, delete or rename.
- Only FAT12/FAT16 and exactly two disk images are handled. The firmware's own
  `fmt` command advertises FAT32 and exFAT, so other models in this family may
  not parse.
- **Flashing a modified image can brick the camera.** Whether the ISP loader can
  recover a corrupt main firmware has not been established. Take a full flash
  backup over the USB vendor ISP path first.

Not affiliated with SeaLife, iCatch, or any vendor named here.
