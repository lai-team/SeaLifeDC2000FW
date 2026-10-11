# overlay/

Changes to apply on top of the baseline image pinned in `../baseline.json`.

The vendor firmware is not in this repo, so this directory is how modifications
get versioned: it holds only what differs, which is small enough to diff and
review in git. `sunp_tool.py build` turns *baseline image + this directory*
into a modified image, deterministically — the same overlay and the same
baseline always produce the same bytes.

Run `sunp_tool.py status` to see what's here and what it would change.

## `files/<VOL>/<path>`

Replaces the file at `<path>` in volume `<VOL>` (`A` or `B`), or creates it if
it doesn't exist. Missing parent directories are created automatically.

```
overlay/files/A/RO_RES/JPG/SEAL.JPG      -> replaces A:\RO_RES\JPG\SEAL.JPG
overlay/files/B/USER_RES/EXTRA.BIN       -> creates  B:\USER_RES\EXTRA.BIN
```

Names that don't fit uppercase 8.3 get an LFN chain plus a `~1` short name,
matching how the vendor's own volumes store e.g. `ALCZoom.TXT` and
`UCS-CNS-HEI_W4_SYMBOL.FDL`.

## `delete.txt`

One `<VOL>:/path` per line. `#` starts a comment.

```
# drop the leftover FUJIFILM USB identity
A:/RO_RES/USB_CH.TXT
```

## `patches/<section>.patch`

Byte edits against a raw section, for changing the firmware itself rather than
a file in a volume. `<section>` is a section name from `sunp_tool.py info`,
with or without the numeric prefix and extension — `mainfw`, `06_mainfw.bin`
and `06_mainfw` all resolve to the same section.

```
# <offset into the section>: <expected bytes> -> <replacement bytes>
0x00ac1dbc: 69 43 61 74 63 68 -> 49 43 41 54 43 48
```

The expected bytes are verified before anything is written, so a patch that no
longer matches its target fails the build instead of corrupting it. Old and new
must be the same length — these are in-place edits, not splices, because
shifting a section's contents would break every absolute address inside the
MIPS firmware.

Offsets are relative to the start of the section. For the main firmware,
section offset equals virtual address − `0x80000000`, so an address you read
off a disassembler goes in directly after subtracting the load base.
