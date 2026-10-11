#!/usr/bin/env python3
"""
sunp_tool.py - unpack / repack / patch iCatch "SUNP BURN FILE" camera firmware.

Developed against the SeaLife DC2000 FPUPDATE.DAT (iCatch CA6350A / V35,
project VSF010W v1.09.00).  The container has no checksum or signature over
itself, so a repack only has to get the section offsets right.

Two workflows:

  Exploration            unpack an image, poke at the pieces, repack.
      unpack IMG -o DIR  /  repack DIR -o IMG

  Versioned modification keep the vendor image out of git and track only a
                         pinned baseline plus a small overlay of changes, so
                         `build` reproduces a byte-identical result anywhere.
      baseline IMG       pin the image: hashes, layout, per-file inventory
      status             show what overlay/ changes relative to the baseline
      build              baseline image + overlay/ -> modified image

  overlay/
      files/<VOL>/<path>      replace or add a file in that volume
      delete.txt              one "<VOL>:/path" per line to remove
      patches/<section>.patch byte edits against a raw section

Flashing a bad image can brick the camera.  Every mutating path verifies its
work: unpack self-tests that sections repack byte-identically, writes are read
back before the image is committed, and `build --verify` rebuilds twice and
checks the results match.
"""

import argparse
import hashlib
import json
import os
import re
import struct
import sys

MAGIC = b"SUNP BURN FILE\x00\x00"
ROLE_MAGIC = {
    "hdr1": b"SUNP BURN HDR 1\x00",
    "hdr2": b"SUNP BURN HDR 2\x00",
    "hdr3": b"SUNP BURN HDR 3\x00",
    "drampar": b"DRAMPAR1",
}

HEADER_LEN = 0x200
HDR2_XOR = 0x7A          # HDR 2 disk descriptors are XOR-obfuscated
HDR2_ENTRY = 0x80        # bytes per disk descriptor
HDR3_ENTRY = 0x70        # bytes per preserved-file path
ENTRIES_OFF = 0x20       # first entry in HDR 2 / HDR 3

# u32 pointer fields in the 0x200-byte container header.
F_TOTAL, F_HDR1, F_HDR2, F_MAINFW, F_UNK20, F_HDR3, F_DRAMPAR = (
    0x10, 0x14, 0x18, 0x1C, 0x20, 0x24, 0x28)

HDR1_DISKSIZE0 = 0x74    # HDR 1 mirrors each disk's size, stride 4

BASELINE_JSON = "baseline.json"
OVERLAY_DIR = "overlay"
MANIFEST_JSON = "manifest.json"
BASELINE_VERSION = 2

# FAT
ATTR_RO, ATTR_HIDDEN, ATTR_SYS, ATTR_VOLID, ATTR_DIR, ATTR_ARCHIVE = (
    0x01, 0x02, 0x04, 0x08, 0x10, 0x20)
ATTR_LFN = 0x0F
SFN_INVALID = set('"*/:<>?\\|+,;=[] ')
FREE_SLOT, END_SLOT = 0xE5, 0x00


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def die(msg):
    sys.exit("error: " + msg)


def warn(msg):
    print("warning: " + msg, file=sys.stderr)


# --------------------------------------------------------------------------
# FAT12 / FAT16
# --------------------------------------------------------------------------

def sfn_checksum(name11):
    s = 0
    for c in name11:
        s = (((s & 1) << 7) + (s >> 1) + c) & 0xFF
    return s


class Fat:
    """FAT12/FAT16 reader-writer for the SUNPLUS resource volumes.

    Created entries use all-zero timestamps and NT flags, matching what the
    vendor's own image contains, which also keeps builds deterministic.
    """

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
        self.eoc = 0xFFF if self.fat12 else 0xFFFF
        self.eoc_min = 0xFF8 if self.fat12 else 0xFFF8

        # The vendor's mastering tool populates FAT copy 0 only and leaves the
        # spare copies empty, so mirroring writes into them would diverge from
        # the known-good baseline for no benefit. Follow whatever convention
        # the volume already uses: maintain a copy only if it currently
        # mirrors copy 0.
        span = self.spf * self.bps
        first = bytes(self.d[self.fat_start:self.fat_start + span])
        self.fat_copies = [0] + [
            c for c in range(1, self.nfat)
            if bytes(self.d[self.fat_start + c * span:
                            self.fat_start + (c + 1) * span]) == first]
        self.stale_fats = [c for c in range(1, self.nfat)
                           if c not in self.fat_copies]

    @property
    def kind(self):
        return "FAT12" if self.fat12 else "FAT16"

    # -- FAT table ---------------------------------------------------------

    def _fat_off(self, copy, n):
        base = self.fat_start + copy * self.spf * self.bps
        return base + (n + n // 2 if self.fat12 else n * 2)

    def get(self, n, copy=0):
        v = struct.unpack_from("<H", self.d, self._fat_off(copy, n))[0]
        if self.fat12:
            return (v >> 4) if (n & 1) else (v & 0xFFF)
        return v

    def set(self, n, val):
        for copy in self.fat_copies:
            o = self._fat_off(copy, n)
            if self.fat12:
                cur = struct.unpack_from("<H", self.d, o)[0]
                cur = ((cur & 0x000F) | (val << 4)) if (n & 1) else \
                      ((cur & 0xF000) | (val & 0xFFF))
                struct.pack_into("<H", self.d, o, cur & 0xFFFF)
            else:
                struct.pack_into("<H", self.d, o, val & 0xFFFF)

    def is_eoc(self, v):
        return v >= self.eoc_min

    def chain(self, start):
        out, c, seen = [], start, set()
        while 2 <= c < self.nclus + 2 and not self.is_eoc(c) and c not in seen:
            seen.add(c)
            out.append(c)
            c = self.get(c)
        return out

    def free_clusters(self):
        return [n for n in range(2, self.nclus + 2) if self.get(n) == 0]

    def _link(self, clusters):
        for i, c in enumerate(clusters):
            self.set(c, self.eoc if i == len(clusters) - 1 else clusters[i + 1])

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

    def _write_clusters(self, clusters, content):
        for i, c in enumerate(clusters):
            chunk = content[i * self.clus_size:(i + 1) * self.clus_size]
            o = self._clus_off(c)
            self.d[o:o + self.clus_size] = chunk.ljust(self.clus_size, b"\x00")

    # -- directories -------------------------------------------------------

    def _slots(self, dirstart):
        """Ordered absolute offsets of every 32-byte slot in a directory."""
        if dirstart is None:
            return [self.root_start + i for i in range(0, self.root_bytes, 32)]
        out = []
        for c in self.chain(dirstart):
            base = self._clus_off(c)
            out += [base + i for i in range(0, self.clus_size, 32)]
        return out

    def readdir(self, dirstart=None):
        ents, lfn, lfn_slots = [], [], []
        for o in self._slots(dirstart):
            e = self.d[o:o + 32]
            if e[0] == END_SLOT:
                break
            if e[0] == FREE_SLOT:
                lfn, lfn_slots = [], []
                continue
            if e[11] == ATTR_LFN:
                lfn.append((e[0] & 0x3F,
                            (e[1:11] + e[14:26] + e[28:32])
                            .decode("utf-16-le", "replace")))
                lfn_slots.append(o)
                continue
            short = e[0:8].decode("latin1").rstrip()
            ext = e[8:11].decode("latin1").rstrip()
            if ext:
                short += "." + ext
            long = "".join(p for _, p in sorted(lfn)).split("\x00")[0] if lfn else ""
            ents.append({
                "name": long or short,
                "short": short,
                "attr": e[11],
                "clus": struct.unpack_from("<H", e, 26)[0],
                "size": struct.unpack_from("<I", e, 28)[0],
                "dirent": o,
                "slots": lfn_slots + [o],
            })
            lfn, lfn_slots = [], []
        return ents

    def walk(self, dirstart=None, path=""):
        for e in self.readdir(dirstart):
            if e["short"] in (".", ".."):
                continue
            if e["attr"] & ATTR_VOLID:
                continue
            p = path + "/" + e["name"]
            isdir = bool(e["attr"] & ATTR_DIR)
            yield p, e, isdir
            if isdir:
                yield from self.walk(e["clus"], p)

    @staticmethod
    def norm(path):
        return "/" + path.replace("\\", "/").strip("/")

    def find(self, path):
        want = self.norm(path).upper()
        for p, e, isdir in self.walk():
            if not isdir and p.upper() == want:
                return e
        return None

    def find_dir(self, path):
        """Start cluster of a directory; None means the root directory."""
        want = self.norm(path).upper()
        if want == "/":
            return None, True
        for p, e, isdir in self.walk():
            if isdir and p.upper() == want:
                return e["clus"], True
        return None, False

    # -- directory slot allocation ----------------------------------------

    def _extend_dir(self, dirstart):
        free = self.free_clusters()
        if not free:
            die("volume full: cannot extend directory")
        c = free[0]
        chain = self.chain(dirstart)
        self.set(chain[-1], c)
        self.set(c, self.eoc)
        o = self._clus_off(c)
        self.d[o:o + self.clus_size] = b"\x00" * self.clus_size

    def _alloc_slots(self, dirstart, n):
        for _ in range(64):
            run = []
            for o in self._slots(dirstart):
                b0 = self.d[o]
                if b0 in (END_SLOT, FREE_SLOT):
                    run.append(o)
                    if len(run) == n:
                        return run
                else:
                    run = []
            if dirstart is None:
                die(f"root directory full: need {n} consecutive free entries")
            self._extend_dir(dirstart)
        die("could not allocate directory entries")

    def _short_name(self, dirstart, longname, exclude=()):
        taken = {e["short"].upper() for e in self.readdir(dirstart)
                 if e["short"].upper() not in {s.upper() for s in exclude}}
        base, dot, ext = longname.rpartition(".")
        if not dot:
            base, ext = longname, ""

        def clean(s):
            return "".join(c for c in s.upper()
                           if c not in SFN_INVALID and 32 < ord(c) < 127)

        b, e = clean(base), clean(ext)[:3]
        if not b:
            b = "FILE"
        plain = b[:8] + ("." + e if e else "")
        if len(b) <= 8 and len(clean(ext)) <= 3 and plain == longname \
                and plain.upper() not in taken:
            return plain, False
        for n in range(1, 1000000):
            suf = "~%d" % n
            cand = b[:max(1, 8 - len(suf))] + suf + ("." + e if e else "")
            if cand.upper() not in taken:
                return cand, True
        die(f"cannot generate a unique short name for {longname!r}")

    @staticmethod
    def _name11(short):
        base, dot, ext = short.rpartition(".")
        if not dot:
            base, ext = short, ""
        return (base[:8].ljust(8) + ext[:3].ljust(3)).encode("latin1")

    @staticmethod
    def _lfn_slots(longname, name11):
        cks = sfn_checksum(name11)
        u = longname.encode("utf-16-le")
        chars = [u[i:i + 2] for i in range(0, len(u), 2)] + [b"\x00\x00"]
        while len(chars) % 13:
            chars.append(b"\xff\xff")
        total = len(chars) // 13
        out = []
        for i in range(total):
            part = b"".join(chars[i * 13:(i + 1) * 13])
            e = bytearray(32)
            e[0] = (i + 1) | (0x40 if i == total - 1 else 0)
            e[1:11] = part[0:10]
            e[11] = ATTR_LFN
            e[13] = cks
            e[14:26] = part[10:22]
            e[28:32] = part[22:26]
            out.append(bytes(e))
        return list(reversed(out))     # highest sequence first, as on disk

    def _sfn_slot(self, name11, clus, size, attr=ATTR_ARCHIVE):
        e = bytearray(32)
        e[0:11] = name11
        e[11] = attr
        struct.pack_into("<H", e, 26, clus)
        struct.pack_into("<I", e, 28, size)
        return bytes(e)                # timestamps stay zero, as in the vendor image

    # -- mutation ----------------------------------------------------------

    def write_file(self, path, content):
        """Replace a file, or create it if absent. Returns 'replace'|'create'."""
        path = self.norm(path)
        existing = self.find(path)
        need = (len(content) + self.clus_size - 1) // self.clus_size
        old = self.chain(existing["clus"]) if existing and existing["clus"] else []
        pool = old + [c for c in self.free_clusters() if c not in set(old)]
        if need > len(pool):
            die(f"{path}: need {need} clusters, only {len(pool)} available "
                f"({(need - len(pool)) * self.clus_size} bytes short)")
        new = pool[:need]

        for c in set(old) - set(new):
            self.set(c, 0)
        self._link(new)
        self._write_clusters(new, content)

        if existing:
            struct.pack_into("<H", self.d, existing["dirent"] + 26,
                             new[0] if new else 0)
            struct.pack_into("<I", self.d, existing["dirent"] + 28, len(content))
            action = "replace"
        else:
            dirpath, _, fname = path.rpartition("/")
            dirstart, ok = self.find_dir(dirpath or "/")
            if not ok:
                die(f"{path}: parent directory {dirpath or '/'} does not exist")
            short, need_lfn = self._short_name(dirstart, fname)
            name11 = self._name11(short)
            slots = (self._lfn_slots(fname, name11) if need_lfn else []) \
                + [self._sfn_slot(name11, new[0] if new else 0, len(content))]
            for off, blob in zip(self._alloc_slots(dirstart, len(slots)), slots):
                self.d[off:off + 32] = blob
            action = "create"

        back = self.find(path)
        if back is None or self.read(back["clus"], back["size"]) != content:
            die(f"{path}: read-back verification failed; volume not written")
        return action

    def mkdir(self, path):
        path = self.norm(path)
        if self.find_dir(path)[1]:
            return False
        parent, _, name = path.rpartition("/")
        dirstart, ok = self.find_dir(parent or "/")
        if not ok:
            die(f"{path}: parent directory {parent or '/'} does not exist")
        free = self.free_clusters()
        if not free:
            die(f"{path}: volume full")
        c = free[0]
        self.set(c, self.eoc)
        o = self._clus_off(c)
        self.d[o:o + self.clus_size] = b"\x00" * self.clus_size
        self.d[o:o + 32] = self._sfn_slot(b".          ", c, 0, ATTR_DIR)
        self.d[o + 32:o + 64] = self._sfn_slot(
            b"..         ", dirstart or 0, 0, ATTR_DIR)

        short, need_lfn = self._short_name(dirstart, name)
        name11 = self._name11(short)
        slots = (self._lfn_slots(name, name11) if need_lfn else []) \
            + [self._sfn_slot(name11, c, 0, ATTR_DIR)]
        for off, blob in zip(self._alloc_slots(dirstart, len(slots)), slots):
            self.d[off:off + 32] = blob
        if not self.find_dir(path)[1]:
            die(f"{path}: directory creation failed verification")
        return True

    def remove(self, path):
        path = self.norm(path)
        e = self.find(path)
        if e is None:
            die(f"{path}: not found in volume")
        for c in self.chain(e["clus"]) if e["clus"] else []:
            self.set(c, 0)
        for o in e["slots"]:
            self.d[o] = FREE_SLOT
        if self.find(path) is not None:
            die(f"{path}: delete failed verification")

    # -- consistency check -------------------------------------------------

    def fsck(self):
        """Returns (problems, notes). Notes are benign vendor quirks."""
        problems, notes = [], []
        owner = {}
        for p, e, isdir in self.walk():
            if e["clus"] == 0:
                if not isdir and e["size"]:
                    problems.append(f"{p}: size {e['size']} but start cluster 0")
                continue
            if not 2 <= e["clus"] < self.nclus + 2:
                problems.append(f"{p}: start cluster {e['clus']} out of range")
                continue
            c, seen = e["clus"], set()
            while 2 <= c < self.nclus + 2 and not self.is_eoc(c):
                if c in seen:
                    problems.append(f"{p}: cluster chain loops at {c}")
                    break
                if c in owner:
                    problems.append(f"{p}: cluster {c} cross-linked with {owner[c]}")
                    break
                seen.add(c)
                owner[c] = p
                c = self.get(c)
            else:
                if not self.is_eoc(c):
                    problems.append(f"{p}: chain leaves the volume at {c}")
            if not isdir:
                want = (e["size"] + self.clus_size - 1) // self.clus_size
                if len(seen) != want:
                    problems.append(f"{p}: {len(seen)} clusters allocated, "
                                    f"{want} needed for {e['size']} bytes")

        for copy in self.fat_copies[1:]:
            bad = [n for n in range(2, self.nclus + 2)
                   if self.get(n) != self.get(n, copy)]
            if bad:
                problems.append(f"maintained FAT copy {copy} differs from copy 0 "
                                f"at {len(bad)} entries (first: {bad[0]})")
        if self.stale_fats:
            notes.append(f"FAT cop{'y' if len(self.stale_fats) == 1 else 'ies'} "
                         f"{', '.join(map(str, self.stale_fats))} not maintained "
                         "(vendor convention); writes update copy 0 only")

        orphans = [n for n in range(2, self.nclus + 2)
                   if self.get(n) != 0 and n not in owner]
        if orphans:
            problems.append(f"{len(orphans)} allocated clusters unreachable from "
                            f"any directory entry (first: {orphans[0]})")
        return problems, notes


# --------------------------------------------------------------------------
# container
# --------------------------------------------------------------------------

def disk_letter(i):
    return chr(ord("A") + i)


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
            warn(f"header size 0x{self.total:x} != file size 0x{len(data):x}")
        for role, off in (("hdr1", self.off_hdr1), ("hdr2", self.off_hdr2),
                          ("hdr3", self.off_hdr3), ("drampar", self.off_drampar)):
            m = ROLE_MAGIC[role]
            if data[off:off + len(m)] != m:
                die(f"{role} magic missing at 0x{off:x}")

        self.disks = self._parse_hdr2()
        self.preserved = self._parse_hdr3()
        self.len_hdr2 = (self.disks[0]["offset"] - self.off_hdr2
                         if self.disks else ENTRIES_OFF)

    def _parse_hdr2(self):
        d, base = self.data, self.off_hdr2
        count = u32(d, base + 0x10)
        if not 0 < count <= 16:
            die(f"implausible HDR2 disk count {count}")
        disks = []
        for i in range(count):
            raw = d[base + ENTRIES_OFF + i * HDR2_ENTRY:
                    base + ENTRIES_OFF + (i + 1) * HDR2_ENTRY]
            e = bytes(b ^ HDR2_XOR for b in raw)
            disks.append({
                "index": i,
                "letter": disk_letter(i),
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
        return [d[base + ENTRIES_OFF + i * HDR3_ENTRY:
                  base + ENTRIES_OFF + (i + 1) * HDR3_ENTRY]
                .split(b"\x00")[0].decode("latin1") for i in range(count)]

    @property
    def len_hdr3(self):
        return ENTRIES_OFF + len(self.preserved) * HDR3_ENTRY

    def sections(self):
        """Ordered [{name, role, offset, length, disk}] covering the whole file."""
        out = [
            ("header", "header", 0, HEADER_LEN, None),
            ("dramcode", "dramcode", HEADER_LEN, self.off_hdr1 - HEADER_LEN, None),
            ("hdr1", "hdr1", self.off_hdr1, self.off_hdr2 - self.off_hdr1, None),
            ("hdr2", "hdr2", self.off_hdr2, self.len_hdr2, None),
        ]
        for d in self.disks:
            out.append((f"vol{d['letter']}", "disk", d["offset"], d["size"],
                        d["index"]))
        out += [
            ("mainfw", "mainfw", self.off_mainfw, self.off_hdr3 - self.off_mainfw,
             None),
            ("hdr3", "hdr3", self.off_hdr3, self.len_hdr3, None),
            ("drampar", "drampar", self.off_drampar,
             len(self.data) - self.off_drampar, None),
        ]
        ext = {"disk": ".img"}
        return [{"name": f"{i:02d}_{stem}{ext.get(role, '.bin')}", "role": role,
                 "offset": off, "length": length, "disk": disk}
                for i, (stem, role, off, length, disk) in enumerate(out)]

    def check_contiguous(self):
        pos, problems = 0, []
        for s in self.sections():
            if s["offset"] != pos:
                problems.append(f"{s['name']}: starts at 0x{s['offset']:x}, "
                                f"expected 0x{pos:x}")
            if s["length"] < 0:
                problems.append(f"{s['name']}: negative length {s['length']}")
            pos = s["offset"] + s["length"]
        if pos != len(self.data):
            problems.append(f"sections end at 0x{pos:x}, "
                            f"file is 0x{len(self.data):x}")
        return problems

    def firmware_id(self):
        i = self.data.find(b"VQSD_BRN:", self.off_mainfw, self.off_hdr3)
        return None if i < 0 else \
            self.data[i:i + 96].split(b"\x00")[0].decode("latin1")

    def build_stamp(self):
        m = re.search(rb"(\d\d:\d\d:\d\d)\x00+([A-Z][a-z]{2} [ \d]\d \d{4})\x00",
                      self.data[self.off_mainfw:self.off_hdr3])
        return None if not m else f"{m.group(2).decode()} {m.group(1).decode()}"


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------

def assemble(sections):
    """Concatenate [{role, disk, data}] and patch every derived field."""
    blobs = [bytearray(s["data"]) for s in sections]
    by_role = {}
    for s, b in zip(sections, blobs):
        by_role.setdefault(s["role"], []).append((s, b))

    for role in ("header", "hdr1", "hdr2", "mainfw", "hdr3", "drampar"):
        if len(by_role.get(role, [])) != 1:
            die(f"expected exactly one '{role}' section, "
                f"found {len(by_role.get(role, []))}")
    hdr = by_role["header"][0][1]
    if len(hdr) != HEADER_LEN:
        die(f"header section must be exactly 0x{HEADER_LEN:x} bytes")

    offs, pos = [], 0
    for b in blobs:
        offs.append(pos)
        pos += len(b)

    def off_of(role):
        s, _ = by_role[role][0]
        return offs[sections.index(s)]

    struct.pack_into("<I", hdr, F_TOTAL, pos)
    struct.pack_into("<I", hdr, F_HDR1, off_of("hdr1"))
    struct.pack_into("<I", hdr, F_HDR2, off_of("hdr2"))
    struct.pack_into("<I", hdr, F_MAINFW, off_of("mainfw"))
    struct.pack_into("<I", hdr, F_HDR3, off_of("hdr3"))
    struct.pack_into("<I", hdr, F_DRAMPAR, off_of("drampar"))

    h1 = by_role["hdr1"][0][1]
    h2 = by_role["hdr2"][0][1]
    disks = sorted(by_role.get("disk", []), key=lambda sb: sb[0]["disk"])
    for i, (s, b) in enumerate(disks):
        base = ENTRIES_OFF + i * HDR2_ENTRY
        if base + HDR2_ENTRY > len(h2):
            die(f"HDR2 section too short for {len(disks)} disk descriptors")
        field = struct.pack("<II", offs[sections.index(s)], len(b))
        h2[base + 0x70:base + 0x78] = bytes(x ^ HDR2_XOR for x in field)
        fo = HDR1_DISKSIZE0 + 4 * i
        if i > 1:
            warn(f"disk {i}: HDR1 size field offset 0x{fo:x} is extrapolated")
        struct.pack_into("<I", h1, fo, len(b))

    return b"".join(bytes(b) for b in blobs)


def carve(data, section_meta):
    out = []
    for s in section_meta:
        out.append(dict(s, data=data[s["offset"]:s["offset"] + s["length"]]))
    return out


# --------------------------------------------------------------------------
# baseline / overlay
# --------------------------------------------------------------------------

def volume_inventory(img):
    fat = Fat(img)
    files = {}
    for p, e, isdir in fat.walk():
        if isdir:
            continue
        files[p] = {"size": e["size"], "sha256": sha256(fat.read(e["clus"], e["size"]))}
    return {
        "type": fat.kind, "cluster_size": fat.clus_size,
        "clusters": fat.nclus, "free_clusters": len(fat.free_clusters()),
        "files": dict(sorted(files.items())),
    }


def make_baseline(image_path, data):
    c = Container(data)
    for p in c.check_contiguous():
        warn(p)
    meta = c.sections()
    secs = carve(data, meta)
    bl = {
        "baseline_version": BASELINE_VERSION,
        "source_name": os.path.basename(image_path),
        "size": len(data),
        "sha256": sha256(data),
        "firmware_id": c.firmware_id(),
        "build_stamp": c.build_stamp(),
        "header_unk_0x20": u32(data, F_UNK20),
        "preserved_files": c.preserved,
        "disks": c.disks,
        "sections": [{"name": s["name"], "role": s["role"], "disk": s["disk"],
                      "offset": s["offset"], "length": s["length"],
                      "sha256": sha256(s["data"])} for s in secs],
        "volumes": {},
    }
    for s in secs:
        if s["role"] == "disk":
            bl["volumes"][disk_letter(s["disk"])] = volume_inventory(s["data"])
    return bl


def load_baseline(path=BASELINE_JSON):
    if not os.path.exists(path):
        die(f"{path} not found; run `baseline <image>` first")
    bl = json.load(open(path))
    if bl.get("baseline_version") != BASELINE_VERSION:
        die(f"{path} was written by a different tool version "
            f"({bl.get('baseline_version')}, expected {BASELINE_VERSION}); "
            "regenerate it with `baseline`")
    return bl


def read_baseline_image(bl, image_path=None):
    path = image_path or bl["source_name"]
    if not os.path.exists(path):
        die(f"baseline image {path} not found (pass --image to point at it)")
    data = open(path, "rb").read()
    got = sha256(data)
    if got != bl["sha256"]:
        die(f"{path} does not match the pinned baseline\n"
            f"  expected sha256 {bl['sha256']}\n"
            f"  got      sha256 {got}")
    return data


# -- overlay ---------------------------------------------------------------

PATCH_RE = re.compile(
    r"^\s*(0x[0-9a-fA-F]+|\d+)\s*:\s*([0-9a-fA-F\s]+?)\s*->\s*([0-9a-fA-F\s]+?)\s*$")


def parse_patch(text, where):
    edits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.split("#")[0].strip()
        if not line:
            continue
        m = PATCH_RE.match(line)
        if not m:
            die(f"{where}:{lineno}: cannot parse patch line: {line!r}\n"
                "  expected  <offset>: <old bytes> -> <new bytes>")
        off = int(m.group(1), 0)
        try:
            old = bytes.fromhex(m.group(2).replace(" ", ""))
            new = bytes.fromhex(m.group(3).replace(" ", ""))
        except ValueError as exc:
            die(f"{where}:{lineno}: bad hex ({exc})")
        if len(old) != len(new):
            die(f"{where}:{lineno}: old is {len(old)} bytes, new is {len(new)}; "
                "patches must preserve length")
        if not old:
            die(f"{where}:{lineno}: empty patch")
        edits.append((off, old, new, lineno))
    return edits


def scan_overlay(overlay=OVERLAY_DIR):
    """Collect overlay operations in a deterministic order."""
    ops = {"patches": [], "deletes": [], "writes": []}
    if not os.path.isdir(overlay):
        return ops

    pdir = os.path.join(overlay, "patches")
    if os.path.isdir(pdir):
        for fn in sorted(os.listdir(pdir)):
            if not fn.endswith(".patch"):
                continue
            path = os.path.join(pdir, fn)
            ops["patches"].append({
                "section": fn[:-len(".patch")],
                "source": path,
                "edits": parse_patch(open(path).read(), path),
            })

    dpath = os.path.join(overlay, "delete.txt")
    if os.path.exists(dpath):
        seen = set()
        for lineno, line in enumerate(open(dpath).read().splitlines(), 1):
            line = line.split("#")[0].strip()
            if not line:
                continue
            if ":" not in line:
                die(f"{dpath}:{lineno}: expected '<VOL>:/path', got {line!r}")
            vol, _, p = line.partition(":")
            key = (vol.strip().upper(), Fat.norm(p))
            if key in seen:
                continue
            seen.add(key)
            ops["deletes"].append({"vol": key[0], "path": key[1], "source": dpath})
        ops["deletes"].sort(key=lambda o: (o["vol"], o["path"]))

    fdir = os.path.join(overlay, "files")
    if os.path.isdir(fdir):
        for vol in sorted(os.listdir(fdir)):
            vroot = os.path.join(fdir, vol)
            if not os.path.isdir(vroot):
                continue
            for dirpath, dirnames, filenames in os.walk(vroot):
                dirnames.sort()
                for fn in sorted(filenames):
                    if fn == ".DS_Store":
                        continue
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, vroot).replace(os.sep, "/")
                    ops["writes"].append({"vol": vol.upper(),
                                          "path": Fat.norm(rel), "source": full})
        ops["writes"].sort(key=lambda o: (o["vol"], o["path"]))
    return ops


def apply_overlay(secs, ops, log=None):
    """Mutate carved sections in place. Deterministic for a given overlay."""
    log = log if log is not None else []
    by_name = {s["name"]: s for s in secs}
    by_vol = {disk_letter(s["disk"]): s for s in secs if s["role"] == "disk"}

    for p in ops["patches"]:
        matches = [n for n in by_name
                   if n == p["section"] or n.split("_", 1)[-1].rsplit(".", 1)[0]
                   == p["section"]]
        if len(matches) != 1:
            die(f"{p['source']}: section {p['section']!r} matches "
                f"{len(matches)} sections {sorted(matches)}")
        s = by_name[matches[0]]
        buf = bytearray(s["data"])
        for off, old, new, lineno in p["edits"]:
            if off + len(old) > len(buf):
                die(f"{p['source']}:{lineno}: offset 0x{off:x} past end of "
                    f"{s['name']} (0x{len(buf):x} bytes)")
            cur = bytes(buf[off:off + len(old)])
            if cur != old:
                die(f"{p['source']}:{lineno}: {s['name']}+0x{off:x} holds "
                    f"{cur.hex(' ')}, patch expects {old.hex(' ')}")
            buf[off:off + len(new)] = new
        s["data"] = bytes(buf)
        log.append(f"patch   {s['name']:<18} {len(p['edits'])} edit(s) "
                   f"from {p['source']}")

    fats = {}

    def fat_for(vol, source):
        if vol not in by_vol:
            die(f"{source}: no disk volume {vol!r} in this image "
                f"(have {sorted(by_vol)})")
        if vol not in fats:
            fats[vol] = Fat(by_vol[vol]["data"])
        return fats[vol]

    for op in ops["deletes"]:
        fat_for(op["vol"], op["source"]).remove(op["path"])
        log.append(f"delete  {op['vol']}:{op['path']}")

    for op in ops["writes"]:
        fat = fat_for(op["vol"], op["source"])
        content = open(op["source"], "rb").read()
        parent = op["path"].rpartition("/")[0]
        if parent and not fat.find_dir(parent)[1]:
            made, parts = [], parent.strip("/").split("/")
            for i in range(len(parts)):
                sub = "/" + "/".join(parts[:i + 1])
                if fat.mkdir(sub):
                    made.append(sub)
            for m in made:
                log.append(f"mkdir   {op['vol']}:{m}")
        action = fat.write_file(op["path"], content)
        log.append(f"{action:<7} {op['vol']}:{op['path']}  {len(content)} bytes")

    for vol, fat in fats.items():
        by_vol[vol]["data"] = bytes(fat.d)
    return log


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_info(args):
    data = open(args.image, "rb").read()
    c = Container(data)
    print(f"{args.image}: {len(data)} bytes  sha256={sha256(data)}")
    for label, val in (("firmware id", c.firmware_id()),
                       ("build stamp", c.build_stamp())):
        if val:
            print(f"{label} : {val}")
    print()
    print(f"{'section':<18}{'role':<10}{'offset':>12}{'length':>12}")
    for s in c.sections():
        print(f"{s['name']:<18}{s['role']:<10}"
              f"{'0x%x' % s['offset']:>12}{'0x%x' % s['length']:>12}")
    print()
    for d in c.disks:
        print(f"disk {d['letter']}: tag={d['tag']!r} "
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
    for p in c.check_contiguous():
        warn(p)

    sec_dir = os.path.join(args.output, "sections")
    os.makedirs(sec_dir, exist_ok=True)
    secs = carve(data, c.sections())

    manifest = {
        "baseline_version": BASELINE_VERSION,
        "source": os.path.basename(args.image),
        "size": len(data), "sha256": sha256(data),
        "firmware_id": c.firmware_id(), "build_stamp": c.build_stamp(),
        "disks": c.disks, "preserved_files": c.preserved,
        "sections": [],
    }
    for s in secs:
        open(os.path.join(sec_dir, s["name"]), "wb").write(s["data"])
        manifest["sections"].append({
            "name": s["name"], "role": s["role"], "disk": s["disk"],
            "offset": s["offset"], "length": s["length"],
            "sha256": sha256(s["data"])})
        print(f"  sections/{s['name']:<18} 0x{s['offset']:08x}  "
              f"{s['length']:>10} bytes")

    if not args.no_files:
        for s in secs:
            if s["role"] != "disk":
                continue
            letter = disk_letter(s["disk"])
            n, total = extract_volume(s["data"],
                                      os.path.join(args.output, "files", letter))
            print(f"  files/{letter}/{'':<13} {n} files, {total} bytes "
                  f"(reference copy; repack builds from sections/)")

    json.dump(manifest, open(os.path.join(args.output, MANIFEST_JSON), "w"),
              indent=2)

    rebuilt = assemble(load_sections(args.output))
    print(f"\nround-trip self-test: "
          f"{'OK (byte-identical)' if rebuilt == data else 'FAILED'}")
    if rebuilt != data:
        die("repack of freshly unpacked sections does not match the original")


def extract_volume(img, root):
    fat = Fat(img)
    n = total = 0
    for p, e, isdir in fat.walk():
        dest = os.path.join(root, p.lstrip("/"))
        if isdir:
            os.makedirs(dest, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        open(dest, "wb").write(fat.read(e["clus"], e["size"]))
        n += 1
        total += e["size"]
    return n, total


def load_sections(dir_):
    mpath = os.path.join(dir_, MANIFEST_JSON)
    if not os.path.exists(mpath):
        die(f"{mpath} not found; `repack` needs the manifest written by `unpack`")
    man = json.load(open(mpath))
    if man.get("baseline_version") != BASELINE_VERSION:
        die(f"{mpath} was written by a different tool version; re-run `unpack`")
    secs = []
    for s in man["sections"]:
        p = os.path.join(dir_, "sections", s["name"])
        if not os.path.exists(p):
            die(f"missing section {s['name']} in {dir_}/sections")
        secs.append(dict(s, data=open(p, "rb").read()))
    return secs


def cmd_repack(args):
    data = assemble(load_sections(args.directory))
    open(args.output, "wb").write(data)
    print(f"{args.output}: {len(data)} bytes  sha256={sha256(data)}")
    man = json.load(open(os.path.join(args.directory, MANIFEST_JSON)))
    if man.get("sha256") == sha256(data):
        print("identical to the image recorded in manifest.json")
    else:
        print(f"differs from the recorded image (sha256={man.get('sha256')})")
    for p in Container(data).check_contiguous():
        warn(p)


def cmd_baseline(args):
    data = open(args.image, "rb").read()
    bl = make_baseline(args.image, data)
    json.dump(bl, open(args.output, "w"), indent=1)
    nfiles = sum(len(v["files"]) for v in bl["volumes"].values())
    print(f"{args.output}: pinned {bl['source_name']} "
          f"({bl['size']} bytes, sha256={bl['sha256']})")
    print(f"  {len(bl['sections'])} sections, {len(bl['volumes'])} volumes, "
          f"{nfiles} files inventoried")
    print(f"  firmware id : {bl['firmware_id']}")
    os.makedirs(os.path.join(OVERLAY_DIR, "files"), exist_ok=True)
    os.makedirs(os.path.join(OVERLAY_DIR, "patches"), exist_ok=True)
    print(f"  overlay dir : {OVERLAY_DIR}/ (files/<VOL>/..., patches/, delete.txt)")


def cmd_status(args):
    bl = load_baseline(args.baseline)
    ops = scan_overlay(args.overlay)
    path = args.image or bl["source_name"]
    if os.path.exists(path):
        state = "matches baseline" if sha256(open(path, "rb").read()) == bl["sha256"] \
            else "DOES NOT MATCH BASELINE"
    else:
        state = "not present here"
    print(f"baseline  {path}  {state}")
    print(f"          sha256 {bl['sha256']}")
    print(f"          {bl['firmware_id']}")

    n = 0
    print(f"\noverlay   {args.overlay}/")
    for p in ops["patches"]:
        n += 1
        print(f"  patch   {p['section']:<22} {len(p['edits'])} edit(s)")
        for off, old, new, _ in p["edits"]:
            print(f"            0x{off:08x}  {old.hex(' ')} -> {new.hex(' ')}")
    for op in ops["deletes"]:
        n += 1
        inv = bl["volumes"].get(op["vol"], {}).get("files", {})
        known = "" if op["path"] in inv else "   (not in baseline!)"
        print(f"  delete  {op['vol']}:{op['path']}{known}")
    for op in ops["writes"]:
        n += 1
        inv = bl["volumes"].get(op["vol"], {}).get("files", {})
        new = open(op["source"], "rb").read()
        base = inv.get(op["path"])
        if base is None:
            print(f"  add     {op['vol']}:{op['path']}  {len(new)} bytes")
        elif base["sha256"] == sha256(new):
            print(f"  same    {op['vol']}:{op['path']}  "
                  f"identical to baseline (no-op)")
        else:
            print(f"  modify  {op['vol']}:{op['path']}  "
                  f"{base['size']} -> {len(new)} bytes")
    if not n:
        print("  (empty - build would reproduce the baseline exactly)")


def _build_once(bl, data, overlay):
    secs = carve(data, bl["sections"])
    for s, rec in zip(secs, bl["sections"]):
        if sha256(s["data"]) != rec["sha256"]:
            die(f"section {s['name']} does not match the baseline hash")
    log = apply_overlay(secs, scan_overlay(overlay))
    return assemble(secs), log


def cmd_build(args):
    bl = load_baseline(args.baseline)
    data = read_baseline_image(bl, args.image)
    out, log = _build_once(bl, data, args.overlay)
    for line in log:
        print("  " + line)
    if not log:
        print("  (empty overlay)")
    open(args.output, "wb").write(out)
    digest = sha256(out)
    print(f"\n{args.output}: {len(out)} bytes  sha256={digest}")
    if digest == bl["sha256"]:
        print("identical to the baseline image")

    for p in Container(out).check_contiguous():
        warn(p)
    for s in carve(out, Container(out).sections()):
        if s["role"] == "disk":
            problems, _ = Fat(s["data"]).fsck()
            for p in problems:
                warn(f"{s['name']}: {p}")
            if not problems:
                print(f"fsck {s['name']}: clean")

    if args.verify:
        again, _ = _build_once(bl, data, args.overlay)
        print("determinism check: "
              + ("OK (second build is byte-identical)" if again == out
                 else "FAILED - build is not reproducible"))
        if again != out:
            die("build is not deterministic")


def open_volume(path):
    return Fat(open(path, "rb").read())


def cmd_ls(args):
    fat = open_volume(args.volume)
    free = len(fat.free_clusters())
    print(f"# {args.volume}: {fat.kind} oem={fat.oem.decode('latin1')!r} "
          f"{fat.bps}B/sector {fat.spc} sectors/cluster ({fat.clus_size}B "
          f"clusters), {fat.nclus} clusters, {free} free "
          f"({free * fat.clus_size} bytes)")
    n = total = 0
    for p, e, isdir in fat.walk():
        print(f"{'d' if isdir else '-'} {e['size']:>10}  {p}")
        if not isdir:
            n += 1
            total += e["size"]
    print(f"# {n} files, {total} bytes")


def cmd_get(args):
    fat = open_volume(args.volume)
    e = fat.find(args.path)
    if e is None:
        die(f"{args.path}: not found")
    open(args.dest, "wb").write(fat.read(e["clus"], e["size"]))
    print(f"{args.path} -> {args.dest} ({e['size']} bytes)")


def cmd_put(args):
    content = open(args.source, "rb").read()
    fat = open_volume(args.volume)
    action = fat.write_file(args.path, content)
    open(args.volume, "wb").write(fat.d)
    print(f"{action} {args.path}: {len(content)} bytes, verified; "
          f"{len(fat.free_clusters())} clusters free")


def cmd_rm(args):
    fat = open_volume(args.volume)
    fat.remove(args.path)
    open(args.volume, "wb").write(fat.d)
    print(f"deleted {args.path}; {len(fat.free_clusters())} clusters free")


def cmd_mkdir(args):
    fat = open_volume(args.volume)
    made = fat.mkdir(args.path)
    open(args.volume, "wb").write(fat.d)
    print(f"{'created' if made else 'already exists'} {args.path}")


def cmd_fsck(args):
    data = open(args.target, "rb").read()
    vols = []
    if data[:len(MAGIC)] == MAGIC:
        for s in carve(data, Container(data).sections()):
            if s["role"] == "disk":
                vols.append((s["name"], s["data"]))
    else:
        vols.append((os.path.basename(args.target), data))
    bad = 0
    for name, img in vols:
        problems, notes = Fat(img).fsck()
        bad += len(problems)
        print(f"{name}: "
              f"{'clean' if not problems else str(len(problems)) + ' problem(s)'}")
        for p in problems:
            print("  ! " + p)
        for n in notes:
            print("  - " + n)
    sys.exit(1 if bad else 0)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
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

    p = sub.add_parser("baseline", help="pin an image as the build baseline")
    p.add_argument("image")
    p.add_argument("-o", "--output", default=BASELINE_JSON)
    p.set_defaults(func=cmd_baseline)

    p = sub.add_parser("status", help="show what the overlay changes")
    p.add_argument("--baseline", default=BASELINE_JSON)
    p.add_argument("--overlay", default=OVERLAY_DIR)
    p.add_argument("--image", help="path to the baseline image")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("build", help="baseline image + overlay -> new image")
    p.add_argument("--baseline", default=BASELINE_JSON)
    p.add_argument("--overlay", default=OVERLAY_DIR)
    p.add_argument("--image", help="path to the baseline image")
    p.add_argument("-o", "--output", default="FPUPDATE.build.DAT")
    p.add_argument("--verify", action="store_true",
                   help="build twice and check the results are identical")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("ls", help="list a FAT volume image")
    p.add_argument("volume")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("get", help="copy a file out of a FAT volume image")
    p.add_argument("volume")
    p.add_argument("path")
    p.add_argument("dest")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("put", help="replace or add a file in a FAT volume image")
    p.add_argument("volume")
    p.add_argument("path")
    p.add_argument("source")
    p.set_defaults(func=cmd_put)

    p = sub.add_parser("rm", help="delete a file from a FAT volume image")
    p.add_argument("volume")
    p.add_argument("path")
    p.set_defaults(func=cmd_rm)

    p = sub.add_parser("mkdir", help="create a directory in a FAT volume image")
    p.add_argument("volume")
    p.add_argument("path")
    p.set_defaults(func=cmd_mkdir)

    p = sub.add_parser("fsck", help="check FAT consistency of a volume or image")
    p.add_argument("target")
    p.set_defaults(func=cmd_fsck)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
