#!/usr/bin/env python3
"""
sunp_assets.py - identify and decode the resource formats inside RO_RES.

The firmware's resource volume is not a flat pile of opaque .BIN files. Most
of it is one of a handful of iCatch/Sunplus formats, and they nest:

  FIF1 archive     8-byte wrapper + "FIF1" + a name/size table. Holds the IQ
                   tuning sets, face-detection databases and UI defaults.
  RIFF SPFN        .SFN icon and font sprite sheets. Decoded to PNG.
  RIFF SPST        .SST string tables. Decoded to id = text listings.
  record wrapper   8- or 16-byte header (type 0x18ba / 0x18ce) around a single
                   tuning table; the 0x18ce form carries a size and checksum.
  FDL Font         third-party CJK/Thai outline fonts. Not decoded.

Commands:
    identify DIR|FILE        classify assets and print a census
    extract  DIR -o OUT      recursively unpack archives and decode what it can

Offsets and field meanings here were derived by parsing the SeaLife DC2000
image and cross-checking sizes, so this describes one firmware rather than a
specification. Anything not understood is written out verbatim rather than
silently dropped.
"""

import argparse
import os
import struct
import sys
import zlib

FIF1_WRAPPER = 0x13F6       # u16 at +2 of the 8-byte wrapper on FIF1 files
REC_8 = 0x18BA              # 8-byte record wrapper
REC_16 = 0x18CE             # 16-byte record wrapper, with size + checksum


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------

def u16(d, o):
    return struct.unpack_from("<H", d, o)[0]


def u32(d, o):
    return struct.unpack_from("<I", d, o)[0]


def write_png(path, w, h, rgba):
    """8-bit RGBA PNG, stdlib only."""
    raw = b"".join(b"\x00" + bytes(rgba[y * w * 4:(y + 1) * w * 4]) for y in range(h))

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 9))
                + chunk(b"IEND", b""))


# --------------------------------------------------------------------------
# FIF1 archive
# --------------------------------------------------------------------------

def is_fif1(d):
    return len(d) > 16 and d[8:12] == b"FIF1"


def fif1_entries(d):
    """[(name, size, offset, data)]. Entry payloads are 16-byte aligned.

    Some archives (BOOTIQ, BOOTLIQ) place an all-zero 16-byte terminator
    record after the name table and others do not, so the terminator is
    detected rather than assumed.
    """
    count = u32(d, 12)
    table_end = 16 + count * 16
    if d[table_end:table_end + 16] == bytes(16):
        table_end += 16                      # zero terminator record
    out, off = [], table_end
    for i in range(count):
        rec = d[16 + i * 16:32 + i * 16]
        name = rec[:12].split(b"\x00")[0].decode("latin1")
        size = u32(rec, 12)
        out.append((name, size, off, d[off:off + size]))
        off = (off + size + 15) // 16 * 16
    return out


# --------------------------------------------------------------------------
# RIFF (SPFN sprite sheets, SPST string tables)
# --------------------------------------------------------------------------

def riff_chunks(d):
    if len(d) < 12 or d[:4] != b"RIFF":
        return None, []
    form = d[8:12].decode("latin1")
    out, o = [], 12
    while o + 8 <= len(d):
        cid = d[o:o + 4].decode("latin1")
        sz = u32(d, o + 4)
        if o + 8 + sz > len(d):
            break
        out.append((cid, o + 8, sz))
        o += 8 + sz + (sz & 1)
    return form, out


def sfn_decode(d):
    """SPFN sprite sheet -> dict(w, h, bpp, palette, glyphs).

    fshp chunk: u16 width, u16 height, u16 flags (low byte = bits per pixel),
    u32 bytes-per-glyph, u16 glyph count, then one record per glyph of
    u16 charcode, u16 advance width, and height*stride packed pixel rows.
    bytes-per-glyph == 4 + stride*height, which is how the layout was pinned
    down: it reproduces every chunk size exactly.

    fplt chunk: u16 colour count, then count * 3 bytes of RGB. Pixel values
    index it. Index 0 is transparent in every sheet checked -- the UI icons
    are drawn over live video -- so it is emitted as alpha 0 rather than as
    palette entry 0, which holds a non-colour marker (128, 0, 0).
    """
    form, chunks = riff_chunks(d)
    if form != "SPFN":
        return None
    hit = [(o, s) for c, o, s in chunks if c == "fshp"]
    if not hit:
        return None
    off, size = hit[0]
    w, h, flags, per, count = struct.unpack_from("<HHHIH", d, off)
    bpp = flags & 0xFF
    if bpp not in (1, 2, 4, 8) or not w or not h:
        return None
    stride = (w * bpp + 7) // 8
    if per != 4 + stride * h or 12 + per * count > size + 12:
        return None

    palette = []
    phit = [(o, s) for c, o, s in chunks if c == "fplt"]
    if phit:
        po, ps = phit[0]
        n = u16(d, po)
        if 2 + n * 3 <= ps:
            palette = [tuple(d[po + 2 + i * 3:po + 5 + i * 3]) for i in range(n)]

    maxv = (1 << bpp) - 1
    glyphs = []
    for g in range(count):
        base = off + 12 + g * per
        code, adv = struct.unpack_from("<HH", d, base)
        px = bytearray(w * h)
        for y in range(h):
            row = d[base + 4 + y * stride:base + 4 + (y + 1) * stride]
            for x in range(w):
                i = x * bpp // 8
                if i < len(row):
                    px[y * w + x] = (row[i] >> (8 - bpp - (x * bpp) % 8)) & maxv
        glyphs.append((code, adv, px))
    return {"w": w, "h": h, "bpp": bpp, "maxv": maxv,
            "palette": palette, "glyphs": glyphs}


def sfn_sheet_rgba(s):
    """Lay every glyph out in a row and resolve to RGBA."""
    w, h, pal, maxv = s["w"], s["h"], s["palette"], s["maxv"]
    glyphs = s["glyphs"]
    sheet_w = max(1, w * len(glyphs))
    buf = bytearray(sheet_w * h * 4)
    for g, (_, _, px) in enumerate(glyphs):
        for y in range(h):
            for x in range(w):
                v = px[y * w + x]
                if v == 0:
                    continue                    # transparent
                if v < len(pal):
                    r, gg, b = pal[v]
                else:                           # no palette: coverage ramp
                    r = gg = b = v * 255 // maxv
                o = (y * sheet_w + g * w + x) * 4
                buf[o:o + 4] = bytes((r, gg, b, 255))
    return sheet_w, h, buf


def sst_decode(d):
    """SPST string table -> [(id, text)].

    strn chunk: u16 language id, u16 unknown, u16 count, then records of
    u16 id, u16 byte length, and that many bytes of UTF-16LE (no terminator).
    """
    form, chunks = riff_chunks(d)
    if form != "SPST":
        return None
    hit = [(o, s) for c, o, s in chunks if c == "strn"]
    if not hit:
        return None
    off, size = hit[0]
    b = d[off:off + size]
    count = u16(b, 4)
    out, o = [], 6
    for _ in range(count):
        if o + 4 > len(b):
            break
        sid, blen = u16(b, o), u16(b, o + 2)
        o += 4
        out.append((sid, b[o:o + blen].decode("utf-16-le", "replace")))
        o += blen
    return out


# --------------------------------------------------------------------------
# record wrappers
# --------------------------------------------------------------------------

def unwrap(d):
    """Strip an 8- or 16-byte record wrapper. Returns (info, payload) or None."""
    if len(d) < 8:
        return None
    kind = u16(d, 2)
    if kind == REC_8:
        size = u16(d, 6)
        if 8 + size <= len(d):
            return {"wrapper": "0x18ba", "tag": u16(d, 0), "size": size}, d[8:8 + size]
    elif kind == REC_16 and len(d) >= 16:
        size = u32(d, 8)
        if 16 + size <= len(d):
            return ({"wrapper": "0x18ce", "tag": u16(d, 0), "size": size,
                     "checksum": f"0x{u32(d, 12):08x}",
                     "sub": d[16:20].decode("latin1", "replace")}, d[16:16 + size])
    return None


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

def classify(d, name=""):
    """Returns (category, detail). Category is coarse, for the census."""
    if not d:
        return "empty", "empty"
    if is_fif1(d):
        return "FIF1 archive", f"FIF1 archive ({u32(d, 12)} members)"
    form, chunks = riff_chunks(d)
    if form == "SPFN":
        s = sfn_decode(d)
        return "RIFF/SPFN sprite sheet", (
            f"RIFF/SPFN sprite {s['w']}x{s['h']} {s['bpp']}bpp, "
            f"{len(s['glyphs'])} glyphs, {len(s['palette'])}-colour palette"
            if s else "RIFF/SPFN sprite (header not understood)")
    if form == "SPST":
        s = sst_decode(d)
        return "RIFF/SPST string table", (
            f"RIFF/SPST strings ({len(s)} entries)" if s else "RIFF/SPST strings")
    if form:
        return f"RIFF/{form}", f"RIFF/{form}"
    if d[:8] == b"FDL Font":
        return "FDL Font", "FDL Font (third-party outline font, not decoded)"
    if d[:4] == b"FDDB":
        return "FDDB face database", "FDDB face-detection database"
    if d[:2] == b"\xff\xd8":
        return "JPEG", "JPEG"
    if d[:4] == b"RIFX":
        return "RIFX", "RIFX"
    if d[:2] == b"\x09\x02":
        return "USB descriptors", "USB configuration descriptor set"
    if d[:5] == b"<JOB>":
        return "text", "text: key/timing test script"
    if d[:2] == b"\xff\xfe":
        return "text", "UTF-16LE text"
    w = unwrap(d)
    if w:
        info = w[0]
        extra = f", sub={info['sub']!r}" if info.get("sub", "").isalnum() else ""
        return f"record wrapper {info['wrapper']}", (
            f"record wrapper {info['wrapper']} tag=0x{info['tag']:04x} "
            f"payload={info['size']}{extra}")
    if set(d) <= {0x00, 0xFF}:
        return "blank placeholder", f"blank placeholder (all 0x{d[0]:02x})"
    if all(32 <= c < 127 or c in (9, 10, 13) for c in d[:512]):
        return "text", "text"
    return "unknown", "unknown"


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------

def extract(data, name, outdir, census, depth=0):
    os.makedirs(outdir, exist_ok=True)
    cat, detail = classify(data, name)
    census[cat] = census.get(cat, 0) + 1
    print("  " * depth + f"{name:<30} {len(data):>8}  {detail}")

    if is_fif1(data):
        sub = os.path.join(outdir, name + ".d")
        for mname, msize, moff, mdata in fif1_entries(data):
            extract(mdata, mname, sub, census, depth + 1)
        return

    s = sfn_decode(data)
    if s:
        sheet_w, sheet_h, rgba = sfn_sheet_rgba(s)
        write_png(os.path.join(outdir, name + ".png"), sheet_w, sheet_h, rgba)
        with open(os.path.join(outdir, name + ".glyphs.txt"), "w") as f:
            f.write(f"# {name}: {s['w']}x{s['h']} {s['bpp']}bpp, "
                    f"{len(s['glyphs'])} glyphs\n")
            if s["palette"]:
                f.write(f"# palette ({len(s['palette'])} colours, index 0 = "
                        f"transparent):\n")
                for i, (r, g, b) in enumerate(s["palette"]):
                    f.write(f"#   {i}\t#{r:02x}{g:02x}{b:02x}\n")
            f.write("# index\tcode\thex\tadvance\tchar\n")
            for i, (code, adv, _) in enumerate(s["glyphs"]):
                ch = chr(code) if 32 <= code < 0x3000 else ""
                f.write(f"{i}\t{code}\t0x{code:04x}\t{adv}\t{ch}\n")
        return

    t = sst_decode(data)
    if t is not None:
        with open(os.path.join(outdir, name + ".txt"), "w") as f:
            f.write(f"# {name}: {len(t)} strings\n")
            for sid, text in t:
                f.write(f"{sid}\t{text}\n")
        return

    w = unwrap(data)
    if w and depth == 0:
        info, payload = w
        with open(os.path.join(outdir, name + ".payload"), "wb") as f:
            f.write(payload)
        return

    if depth:                       # archive member with no decoder: keep it
        with open(os.path.join(outdir, name), "wb") as f:
            f.write(data)


def iter_inputs(target):
    if os.path.isfile(target):
        yield target
        return
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames.sort()
        for fn in sorted(filenames):
            if fn == ".DS_Store":
                continue
            yield os.path.join(dirpath, fn)


def cmd_identify(args):
    census = {}
    rows = []
    for p in iter_inputs(args.target):
        d = open(p, "rb").read()
        cat, detail = classify(d, os.path.basename(p))
        rows.append((os.path.relpath(p, args.target)
                     if os.path.isdir(args.target) else p, len(d), detail))
        census[cat] = census.get(cat, 0) + 1
    width = max((len(r[0]) for r in rows), default=10)
    for path, size, k in rows:
        print(f"{path:<{width}}  {size:>9}  {k}")
    print(f"\n{len(rows)} files")
    for k, n in sorted(census.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>4}  {k}")


def cmd_extract(args):
    census = {}
    for p in iter_inputs(args.target):
        d = open(p, "rb").read()
        rel = os.path.relpath(p, args.target) if os.path.isdir(args.target) \
            else os.path.basename(p)
        out = os.path.join(args.output, os.path.dirname(rel))
        extract(d, os.path.basename(rel), out, census)
    print(f"\nwrote {args.output}/")
    for k, n in sorted(census.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>4}  {k}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("identify", help="classify assets and print a census")
    p.add_argument("target")
    p.set_defaults(func=cmd_identify)
    p = sub.add_parser("extract", help="recursively unpack and decode")
    p.add_argument("target")
    p.add_argument("-o", "--output", default="assets")
    p.set_defaults(func=cmd_extract)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
