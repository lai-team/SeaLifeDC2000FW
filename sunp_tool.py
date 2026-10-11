#!/usr/bin/env python3
"""
sunp_tool.py - unpack / repack iCatch "SUNP BURN FILE" camera firmware images.

Developed against the SeaLife DC2000 FPUPDATE.DAT (iCatch CA6350A / V35,
project VSF010W v1.09.00).  The container has no checksum or signature over
itself, so a repack only has to get the section offsets right.

Commands:
    unpack  IMG  [-o DIR]        split into sections + extract the FAT volumes
    repack  DIR  [-o IMG]        rebuild a container from an unpacked dir
    info    IMG                  parse and print the container layout
    ls      VOL                  list a FAT volume image
    put     VOL PATH SRC         replace a file inside a FAT volume image
    get     VOL PATH DST         copy a file out of a FAT volume image

Typical edit cycle:
    ./sunp_tool.py unpack FPUPDATE.DAT -o unpacked
    ./sunp_tool.py put unpacked/sections/04_volA.img /RO_RES/JPG/SEAL.JPG new.jpg
    ./sunp_tool.py repack unpacked -o FPUPDATE.new.DAT

Flashing a bad image can brick the camera.  `unpack` self-tests that the
sections repack byte-identically, and `put` reads the file back after writing.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import struct
import sys

MAGIC = b"SUNP BURN FILE\x00\x00"
HDR1_MAGIC = b"SUNP BURN HDR 1\x00"
HDR2_MAGIC = b"SUNP BURN HDR 2\x00"
HDR3_MAGIC = b"SUNP BURN HDR 3\x00"
DRAMPAR_MAGIC = b"DRAMPAR1"

HEADER_LEN = 0x200
HDR2_XOR = 0x7A          # HDR 2 disk descriptors are XOR-obfuscated
HDR2_ENTRY = 0x80        # bytes per disk descriptor
HDR3_ENTRY = 0x70        # bytes per preserved-file path
ENTRIES_OFF = 0x20       # first entry in HDR 2 / HDR 3

# Offsets of the u32 pointer fields in the 0x200-byte container header.
F_TOTAL, F_HDR1, F_HDR2, F_MAINFW, F_UNK20, F_HDR3, F_DRAMPAR = (
    0x10, 0x14, 0x18, 0x1C, 0x20, 0x24, 0x28)

# Section file names, in container order.
SECTIONS = ["00_header.bin", "01_dramcode.bin", "02_hdr1.bin", "03_hdr2.bin",
            "04_volA.img", "05_volB.img", "06_mainfw.bin", "07_hdr3.bin",
            "08_drampar.bin"]

# Disk-size fields inside HDR 1 (one u32 per disk, in disk order).
HDR1_DISKSIZE = [0x74, 0x78]


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def die(msg):
    sys.exit("error: " + msg)


# --------------------------------------------------------------------------
# container
# --------------------------------------------------------------------------

class Container:
    """Parsed view of a SUNP BURN FILE image."""

    def __init__(self, data):
        if data[:len(MAGIC)] != MAGIC:
            die("not a SUNP BURN FILE (bad magic)")
        self.data = data
        self.total = u32(data, F_TOTAL)
        self.off_hdr1 = u32(data, F_HDR1)
        self.off_hdr2 = u32(data, F_HDR2)
        self.off_mainfw = u32(data, F_MAINFW)
        self.off_hdr3 = u32(data, F_HDR3)
        self.off_drampar = u32(data, F_DRAMPAR)

        if self.total != len(data):
            print(f"warning: header size 0x{self.total:x} != file size "
                  f"0x{len(data):x}", file=sys.stderr)

        for name, off, magic in (("HDR1", self.off_hdr1, HDR1_MAGIC),
                                 ("HDR2", self.off_hdr2, HDR2_MAGIC),
                                 ("HDR3", self.off_hdr3, HDR3_MAGIC),
                                 ("DRAMPAR", self.off_drampar, DRAMPAR_MAGIC)):
            if data[off:off + len(magic)] != magic:
                die(f"{name} magic missing at 0x{off:x}")

        self.disks = self._parse_hdr2()
        self.preserved = self._parse_hdr3()

        # HDR 2 is immediately followed by disk 0, so its length is implied by
        # where the first disk starts rather than being stored anywhere.
        self.len_hdr2 = (self.disks[0]["offset"] - self.off_hdr2
                         if self.disks else 0x120)

    def _parse_hdr2(self):
        d, base = self.data, self.off_hdr2
        count = u32(d, base + 0x10)
        if not 0 < count <= 8:
            die(f"implausible HDR2 disk count {count}")
        disks = []
        for i in range(count):
            raw = d[base + ENTRIES_OFF + i * HDR2_ENTRY:
                    base + ENTRIES_OFF + (i + 1) * HDR2_ENTRY]
            e = bytes(b ^ HDR2_XOR for b in raw)
            disks.append({
                "index": i,
                "tag": e[:0x70].split(b"\x00")[0].decode("latin1"),
                "offset": struct.unpack_from("<I", e, 0x70)[0],
                "size": struct.unpack_from("<I", e, 0x74)[0],
            })
        return disks

    def _parse_hdr3(self):
        d, base = self.data, self.off_hdr3
        count = u32(d, base + 0x10)
        if count > 64:
            die(f"implausible HDR3 entry count {count}")
        out = []
        for i in range(count):
            s = d[base + ENTRIES_OFF + i * HDR3_ENTRY:
                  base + ENTRIES_OFF + (i + 1) * HDR3_ENTRY]
            out.append(s.split(b"\x00")[0].decode("latin1"))
        return out

    @property
    def len_hdr3(self):
        return ENTRIES_OFF + len(self.preserved) * HDR3_ENTRY

    def layout(self):
        """Ordered [(section_filename, offset, length)] covering the whole file."""
        if len(self.disks) != 2:
            die(f"expected 2 disk images, found {len(self.disks)}; this tool's "
                "fixed section list cannot describe that image")
        a, b = self.disks
        return [
            (SECTIONS[0], 0, HEADER_LEN),
            (SECTIONS[1], HEADER_LEN, self.off_hdr1 - HEADER_LEN),
            (SECTIONS[2], self.off_hdr1, self.off_hdr2 - self.off_hdr1),
            (SECTIONS[3], self.off_hdr2, self.len_hdr2),
            (SECTIONS[4], a["offset"], a["size"]),
            (SECTIONS[5], b["offset"], b["size"]),
            (SECTIONS[6], self.off_mainfw, self.off_hdr3 - self.off_mainfw),
            (SECTIONS[7], self.off_hdr3, self.len_hdr3),
            (SECTIONS[8], self.off_drampar, len(self.data) - self.off_drampar),
        ]

    def check_contiguous(self):
        """The format has no padding; verify sections tile the file exactly."""
        pos, problems = 0, []
        for name, off, length in self.layout():
            if off != pos:
                problems.append(f"{name}: starts at 0x{off:x}, expected 0x{pos:x}")
            if length < 0:
                problems.append(f"{name}: negative length {length}")
            pos = off + length
        if pos != len(self.data):
            problems.append(f"sections end at 0x{pos:x}, file is 0x{len(self.data):x}")
        return problems

    def firmware_id(self):
        """The VQSD_BRN:<project> v<maj>.<min>.<sub>@<hw> tag inside the main FW."""
        i = self.data.find(b"VQSD_BRN:", self.off_mainfw, self.off_hdr3)
        if i < 0:
            return None
        return self.data[i:i + 96].split(b"\x00")[0].decode("latin1")

    def build_stamp(self):
        """The __TIME__/__DATE__ literal pair the main firmware prints at boot."""
        m = re.search(rb"(\d\d:\d\d:\d\d)\x00+([A-Z][a-z]{2} [ \d]\d \d{4})\x00",
                      self.data[self.off_mainfw:self.off_hdr3])
        if not m:
            return None
        return f"{m.group(2).decode()} {m.group(1).decode()}"


# --------------------------------------------------------------------------
# FAT12 / FAT16
# --------------------------------------------------------------------------

class Fat:
    """Minimal FAT12/FAT16 reader-writer for the SUNPLUS resource volumes."""

    def __init__(self, data):
        self.d = bytearray(data)
        b = self.d
        self.bps = struct.unpack_from("<H", b, 11)[0]
        self.spc = b[13]
        self.rsvd = struct.unpack_from("<H", b, 14)[0]
        self.nfat = b[16]
        self.rootent = struct.unpack_from("<H", b, 17)[0]
        tot16 = struct.unpack_from("<H", b, 19)[0]
        self.spf = struct.unpack_from("<H", b, 22)[0]
        tot32 = struct.unpack_from("<I", b, 32)[0]
        self.tot = tot16 or tot32
        if not (self.bps and self.spc and self.spf and self.tot):
            die("not a FAT volume (empty BPB)")
        self.oem = bytes(b[3:11])
        self.fat_start = self.rsvd * self.bps
        self.root_start = self.fat_start + self.nfat * self.spf * self.bps
        self.root_bytes = self.rootent * 32
        self.data_start = self.root_start + self.root_bytes
        self.clus_size = self.spc * self.bps
        datasec = self.tot - (self.rsvd + self.nfat * self.spf
                              + self.root_bytes // self.bps)
        self.nclus = datasec // self.spc
        self.fat12 = self.nclus < 4085
        self.eoc_val = 0xFFF if self.fat12 else 0xFFFF
        self.bad_val = 0xFF7 if self.fat12 else 0xFFF7

    # -- FAT table ---------------------------------------------------------

    def _fat_off(self, copy, n):
        base = self.fat_start + copy * self.spf * self.bps
        return base + (n + n // 2 if self.fat12 else n * 2)

    def get(self, n):
        o = self._fat_off(0, n)
        v = struct.unpack_from("<H", self.d, o)[0]
        if self.fat12:
            return (v >> 4) if (n & 1) else (v & 0xFFF)
        return v

    def set(self, n, val):
        for copy in range(self.nfat):
            o = self._fat_off(copy, n)
            if self.fat12:
                cur = struct.unpack_from("<H", self.d, o)[0]
                cur = ((cur & 0x000F) | (val << 4)) if (n & 1) else \
                      ((cur & 0xF000) | (val & 0xFFF))
                struct.pack_into("<H", self.d, o, cur & 0xFFFF)
            else:
                struct.pack_into("<H", self.d, o, val & 0xFFFF)

    def is_eoc(self, v):
        return v >= (0xFF8 if self.fat12 else 0xFFF8)

    def chain(self, start):
        out, c, seen = [], start, set()
        while 2 <= c < self.nclus + 2 and not self.is_eoc(c) and c not in seen:
            seen.add(c)
            out.append(c)
            c = self.get(c)
        return out

    def free_clusters(self):
        return [n for n in range(2, self.nclus + 2) if self.get(n) == 0]

    # -- data --------------------------------------------------------------

    def _clus_off(self, c):
        return self.data_start + (c - 2) * self.clus_size

    def read(self, start, size):
        if not start or not size:
            return b""
        buf = bytearray()
        for c in self.chain(start):
            buf += self.d[self._clus_off(c):self._clus_off(c) + self.clus_size]
            if len(buf) >= size:
                break
        return bytes(buf[:size])

    # -- directories -------------------------------------------------------

    def _regions(self, start):
        if start is None:
            yield self.root_start, self.root_bytes
        else:
            for c in self.chain(start):
                yield self._clus_off(c), self.clus_size

    def readdir(self, start=None):
        """Yield dicts with name/attr/clus/size and the dirent's absolute offset."""
        ents, lfn = [], []
        for base, length in self._regions(start):
            for i in range(0, length, 32):
                o = base + i
                e = self.d[o:o + 32]
                if len(e) < 32 or e[0] == 0:
                    return ents
                if e[0] == 0xE5:
                    lfn = []
                    continue
                if e[11] == 0x0F:
                    lfn.append((e[0] & 0x3F,
                                (e[1:11] + e[14:26] + e[28:32])
                                .decode("utf-16-le", "replace")))
                    continue
                short = e[0:8].decode("latin1").rstrip()
                ext = e[8:11].decode("latin1").rstrip()
                if ext:
                    short += "." + ext
                long = "".join(p for _, p in sorted(lfn)).split("\x00")[0] if lfn else ""
                lfn = []
                ents.append({
                    "name": long or short,
                    "short": short,
                    "attr": e[11],
                    "clus": struct.unpack_from("<H", e, 26)[0],
                    "size": struct.unpack_from("<I", e, 28)[0],
                    "dirent": o,
                    "mtime": self._dos_time(e),
                })
        return ents

    @staticmethod
    def _dos_time(e):
        t, dt = struct.unpack_from("<H", e, 22)[0], struct.unpack_from("<H", e, 24)[0]
        try:
            return datetime.datetime(1980 + (dt >> 9), (dt >> 5) & 15 or 1,
                                     dt & 31 or 1, t >> 11, (t >> 5) & 63,
                                     (t & 31) * 2).isoformat()
        except ValueError:
            return None

    def walk(self, start=None, path=""):
        for e in self.readdir(start):
            if e["short"] in (".", ".."):
                continue
            p = path + "/" + e["name"]
            isdir = bool(e["attr"] & 0x10)
            yield p, e, isdir
            if isdir:
                yield from self.walk(e["clus"], p)

    def find(self, path):
        want = path.replace("\\", "/").strip("/").upper()
        for p, e, isdir in self.walk():
            if not isdir and p.strip("/").upper() == want:
                return e
        return None

    # -- write -------------------------------------------------------------

    def replace(self, path, content):
        """Rewrite an existing file's contents, reallocating its cluster chain."""
        e = self.find(path)
        if e is None:
            die(f"{path}: not found in volume")
        old = self.chain(e["clus"]) if e["clus"] else []
        need = (len(content) + self.clus_size - 1) // self.clus_size
        pool = old + [c for c in self.free_clusters() if c not in set(old)]
        if need > len(pool):
            die(f"{path}: need {need} clusters, only {len(pool)} available "
                f"({(need - len(pool)) * self.clus_size} bytes short)")

        new = pool[:need]
        for c in set(old) - set(new):
            self.set(c, 0)
        for i, c in enumerate(new):
            self.set(c, self.eoc_val if i == need - 1 else new[i + 1])

        for i, c in enumerate(new):
            chunk = content[i * self.clus_size:(i + 1) * self.clus_size]
            o = self._clus_off(c)
            self.d[o:o + self.clus_size] = chunk.ljust(self.clus_size, b"\x00")

        struct.pack_into("<H", self.d, e["dirent"] + 26, new[0] if new else 0)
        struct.pack_into("<I", self.d, e["dirent"] + 28, len(content))

        back = self.read(new[0] if new else 0, len(content))
        if back != content:
            die(f"{path}: read-back verification failed; volume not written")
        return len(old), need


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_info(args):
    data = open(args.image, "rb").read()
    c = Container(data)
    print(f"{args.image}: {len(data)} bytes  sha256={sha256(data)}")
    fid = c.firmware_id()
    if fid:
        print(f"firmware id : {fid}")
    stamp = c.build_stamp()
    if stamp:
        print(f"build stamp : {stamp}")
    print()
    print(f"{'section':<16}{'offset':>12}{'length':>12}")
    for name, off, length in c.layout():
        print(f"{name:<16}{'0x%x' % off:>12}{'0x%x' % length:>12}")
    print()
    for d in c.disks:
        print(f"disk {d['index']}: tag={d['tag']!r} "
              f"offset=0x{d['offset']:x} size=0x{d['size']:x}")
    print(f"\npreserved across update ({len(c.preserved)}):")
    for p in c.preserved:
        print("  " + p)
    problems = c.check_contiguous()
    print("\nlayout: " + ("contiguous, no gaps" if not problems else "PROBLEMS"))
    for p in problems:
        print("  ! " + p)


def cmd_unpack(args):
    data = open(args.image, "rb").read()
    c = Container(data)
    problems = c.check_contiguous()
    if problems:
        for p in problems:
            print("warning: " + p, file=sys.stderr)

    out = args.output
    sec_dir = os.path.join(out, "sections")
    os.makedirs(sec_dir, exist_ok=True)

    manifest = {
        "source": os.path.basename(args.image),
        "total_size": len(data),
        "sha256": sha256(data),
        "firmware_id": c.firmware_id(),
        "build_stamp": c.build_stamp(),
        "header_fields": {
            "total": c.total, "hdr1": c.off_hdr1, "hdr2": c.off_hdr2,
            "mainfw": c.off_mainfw, "hdr3": c.off_hdr3,
            "drampar": c.off_drampar, "unk_0x20": u32(data, F_UNK20),
        },
        "disks": c.disks,
        "preserved_files": c.preserved,
        "sections": [],
    }

    for name, off, length in c.layout():
        blob = data[off:off + length]
        with open(os.path.join(sec_dir, name), "wb") as f:
            f.write(blob)
        manifest["sections"].append({
            "name": name, "offset": off, "length": length, "sha256": sha256(blob),
        })
        print(f"  sections/{name:<16} 0x{off:08x}  {length:>10} bytes")

    if not args.no_files:
        for sec, label in ((SECTIONS[4], "A"), (SECTIONS[5], "B")):
            img = open(os.path.join(sec_dir, sec), "rb").read()
            root = os.path.join(out, "files", label)
            n, total = _extract_volume(img, root)
            print(f"  files/{label}/{' ' * 11} {n} files, {total} bytes "
                  f"(read-only copy; edit via `put`)")

    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)

    rebuilt = _build(out)
    ok = rebuilt == data
    print(f"\nround-trip self-test: {'OK (byte-identical)' if ok else 'FAILED'}")
    if not ok:
        die("repack of freshly unpacked sections does not match the original")


def _extract_volume(img, root):
    fat = Fat(img)
    n = total = 0
    for p, e, isdir in fat.walk():
        dest = os.path.join(root, p.lstrip("/"))
        if isdir:
            os.makedirs(dest, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(fat.read(e["clus"], e["size"]))
        n += 1
        total += e["size"]
    return n, total


def _build(dir_):
    """Concatenate sections and patch every offset/size field to match."""
    sec_dir = os.path.join(dir_, "sections")
    blobs = []
    for name in SECTIONS:
        p = os.path.join(sec_dir, name)
        if not os.path.exists(p):
            die(f"missing section {name} in {sec_dir}")
        blobs.append(bytearray(open(p, "rb").read()))

    hdr, dram, h1, h2, volA, volB, mainfw, h3, drampar = blobs
    if len(hdr) != HEADER_LEN:
        die(f"header section must be exactly 0x{HEADER_LEN:x} bytes")

    offs, pos = [], 0
    for b in blobs:
        offs.append(pos)
        pos += len(b)
    total = pos

    o_hdr1, o_hdr2, o_volA, o_volB, o_main, o_hdr3, o_dram = (
        offs[2], offs[3], offs[4], offs[5], offs[6], offs[7], offs[8])

    struct.pack_into("<I", hdr, F_TOTAL, total)
    struct.pack_into("<I", hdr, F_HDR1, o_hdr1)
    struct.pack_into("<I", hdr, F_HDR2, o_hdr2)
    struct.pack_into("<I", hdr, F_MAINFW, o_main)
    struct.pack_into("<I", hdr, F_HDR3, o_hdr3)
    struct.pack_into("<I", hdr, F_DRAMPAR, o_dram)

    # HDR 2 disk descriptors carry absolute offset + size, XOR-obfuscated.
    for i, (off, size) in enumerate(((o_volA, len(volA)), (o_volB, len(volB)))):
        base = ENTRIES_OFF + i * HDR2_ENTRY
        if base + HDR2_ENTRY > len(h2):
            die("HDR2 section too short for two disk descriptors")
        field = struct.pack("<II", off, size)
        h2[base + 0x70:base + 0x78] = bytes(b ^ HDR2_XOR for b in field)

    # HDR 1 mirrors the disk sizes.
    for i, size in enumerate((len(volA), len(volB))):
        struct.pack_into("<I", h1, HDR1_DISKSIZE[i], size)

    return b"".join(bytes(b) for b in blobs)


def cmd_repack(args):
    data = _build(args.directory)
    out = args.output
    with open(out, "wb") as f:
        f.write(data)
    print(f"{out}: {len(data)} bytes  sha256={sha256(data)}")

    mpath = os.path.join(args.directory, "manifest.json")
    if os.path.exists(mpath):
        m = json.load(open(mpath))
        if m.get("sha256") == sha256(data):
            print("identical to the original image recorded in manifest.json")
        else:
            print(f"differs from original ({m.get('total_size')} bytes, "
                  f"sha256={m.get('sha256')})")
    for p in Container(data).check_contiguous():
        print("warning: " + p, file=sys.stderr)


def cmd_ls(args):
    fat = Fat(open(args.volume, "rb").read())
    kind = "FAT12" if fat.fat12 else "FAT16"
    free = len(fat.free_clusters())
    print(f"# {args.volume}: {kind} oem={fat.oem.decode('latin1')!r} "
          f"{fat.bps}B/sector {fat.spc} sectors/cluster "
          f"({fat.clus_size}B clusters), {fat.nclus} clusters, "
          f"{free} free ({free * fat.clus_size} bytes)")
    n = total = 0
    for p, e, isdir in fat.walk():
        print(f"{'d' if isdir else '-'} {e['size']:>10}  {e['mtime'] or '?':<19}  {p}")
        if not isdir:
            n += 1
            total += e["size"]
    print(f"# {n} files, {total} bytes")


def cmd_get(args):
    fat = Fat(open(args.volume, "rb").read())
    e = fat.find(args.path)
    if e is None:
        die(f"{args.path}: not found")
    with open(args.dest, "wb") as f:
        f.write(fat.read(e["clus"], e["size"]))
    print(f"{args.path} -> {args.dest} ({e['size']} bytes)")


def cmd_put(args):
    content = open(args.source, "rb").read()
    fat = Fat(open(args.volume, "rb").read())
    old_n, new_n = fat.replace(args.path, content)
    with open(args.volume, "wb") as f:
        f.write(fat.d)
    print(f"{args.path}: {old_n} -> {new_n} clusters, {len(content)} bytes, "
          f"verified; {len(fat.free_clusters())} clusters free")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("info", help="parse and print the container layout")
    p.add_argument("image")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("unpack", help="split into sections and extract volumes")
    p.add_argument("image")
    p.add_argument("-o", "--output", default="unpacked")
    p.add_argument("--no-files", action="store_true",
                   help="skip extracting the FAT trees")
    p.set_defaults(func=cmd_unpack)

    p = sub.add_parser("repack", help="rebuild a container from an unpacked dir")
    p.add_argument("directory")
    p.add_argument("-o", "--output", default="FPUPDATE.new.DAT")
    p.set_defaults(func=cmd_repack)

    p = sub.add_parser("ls", help="list a FAT volume image")
    p.add_argument("volume")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("get", help="copy a file out of a FAT volume image")
    p.add_argument("volume")
    p.add_argument("path")
    p.add_argument("dest")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("put", help="replace a file inside a FAT volume image")
    p.add_argument("volume")
    p.add_argument("path")
    p.add_argument("source")
    p.set_defaults(func=cmd_put)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
