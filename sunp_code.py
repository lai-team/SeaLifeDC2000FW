#!/usr/bin/env python3
"""
sunp_code.py - navigate the MIPS blobs inside a SUNP BURN FILE image.

Both code sections are flat: file offset == virtual address - load base, so a
section file can be disassembled directly once you know its base.

    06_mainfw.bin    -> 0x80000000
    01_dramcode.bin  -> 0x80040000

Bases are inferred from the section filename and can be overridden with
--base. Verified empirically: pointer words in each blob resolve to real
strings at the implied offsets.

Commands:
    xref  SECTION (--string S | --addr VA)   find code that references it
    dis   SECTION --addr VA [--count N]      disassemble, annotating string refs
    gp    SECTION --offset OFF               find reads/writes of a gp-relative global

`xref` is pure struct parsing and always works. `dis` needs capstone:

    python3 -m venv /tmp/capenv && /tmp/capenv/bin/pip install capstone
    /tmp/capenv/bin/python sunp_code.py dis ...

Why this exists: the resource formats came apart with pure structure analysis,
but questions about *behaviour* (does the firmware cap card capacity? what
gates SD bus speed?) can only be answered by reading the code. This is the
tooling that produced the findings in docs/firmware-internals.md.
"""

import argparse
import os
import struct
import sys

BASES = {"mainfw": 0x80000000, "dramcode": 0x80040000}

OP_LUI, OP_ADDIU, OP_ORI = 0x0F, 0x09, 0x0D
GP_REG = 28
# gp-relative loads/stores, by opcode
GP_OPS = {0x20: "lb", 0x21: "lh", 0x23: "lw", 0x24: "lbu", 0x25: "lhu",
          0x28: "sb", 0x29: "sh", 0x2B: "sw"}


def die(msg):
    sys.exit("error: " + msg)


def infer_base(path, override=None):
    if override is not None:
        return override
    name = os.path.basename(path).lower()
    for key, base in BASES.items():
        if key in name:
            return base
    die(f"cannot infer load base from {name!r}; pass --base 0x...")


def hi_lo(addr):
    """Split an address the way the assembler does for lui + signed addiu."""
    lo = addr & 0xFFFF
    hi = ((addr >> 16) + (1 if lo & 0x8000 else 0)) & 0xFFFF
    return hi, lo


def find_xrefs(d, base, addr, window=14):
    """Code sites that materialise `addr` via lui + addiu/ori."""
    hi, lo = hi_lo(addr)
    out = []
    for i in range(0, len(d) - 4, 4):
        w = struct.unpack_from("<I", d, i)[0]
        if (w >> 26) != OP_LUI or (w & 0xFFFF) != hi:
            continue
        rt = (w >> 16) & 0x1F
        for j in range(i + 4, min(i + 4 + window * 4, len(d) - 4), 4):
            w2 = struct.unpack_from("<I", d, j)[0]
            op = w2 >> 26
            if op in (OP_ADDIU, OP_ORI) and ((w2 >> 21) & 0x1F) == rt \
                    and (w2 & 0xFFFF) == lo:
                out.append(base + i)
                break
            if op == OP_LUI and ((w2 >> 16) & 0x1F) == rt:
                break                    # register reloaded, pair broken
    return out


def find_pointers(d, base, addr):
    """Bare 32-bit words equal to `addr` (pointer tables, vtables)."""
    needle = struct.pack("<I", addr)
    out, s = [], 0
    while True:
        i = d.find(needle, s)
        if i < 0:
            break
        if i % 4 == 0:
            out.append(base + i)
        s = i + 1
    return out


def read_cstring(d, base, addr, limit=120):
    off = addr - base
    if not 0 <= off < len(d):
        return None
    end = d.find(b"\x00", off, min(off + limit, len(d)))
    if end < 0:
        return None
    s = d[off:end]
    if len(s) < 3 or not all(32 <= c < 127 or c in (9, 10, 13) for c in s):
        return None
    return s.decode("latin1")


def load(path, base):
    return open(path, "rb").read(), base


def cmd_xref(args):
    base = infer_base(args.section, args.base)
    d = open(args.section, "rb").read()
    if args.string:
        needle = args.string.encode("latin1")
        off = d.find(needle)
        if off < 0:
            die(f"string {args.string!r} not found in {args.section}")
        addr = base + off
        print(f'"{args.string}" @ file 0x{off:x}, va 0x{addr:08x}')
    else:
        addr = int(args.addr, 0)
        print(f"va 0x{addr:08x}")
    code = find_xrefs(d, base, addr)
    print(f"  lui/addiu refs : {['0x%08x' % a for a in code] or 'none'}")
    if args.pointers:
        ptr = find_pointers(d, base, addr)
        shown = ["0x%08x" % a for a in ptr[:20]]
        print(f"  pointer words  : {shown or 'none'}"
              + (f" (+{len(ptr)-20} more)" if len(ptr) > 20 else ""))


def cmd_dis(args):
    try:
        from capstone import Cs, CS_ARCH_MIPS, CS_MODE_MIPS32, CS_MODE_LITTLE_ENDIAN
    except ImportError:
        die("capstone not installed. Try:\n"
            "  python3 -m venv /tmp/capenv && /tmp/capenv/bin/pip install capstone\n"
            "  /tmp/capenv/bin/python sunp_code.py dis ...")
    base = infer_base(args.section, args.base)
    d = open(args.section, "rb").read()
    addr = int(args.addr, 0)
    off = addr - base
    if not 0 <= off < len(d):
        die(f"0x{addr:08x} is outside this section "
            f"(0x{base:08x}-0x{base+len(d):08x})")
    n = args.count * 4
    md = Cs(CS_ARCH_MIPS, CS_MODE_MIPS32 + CS_MODE_LITTLE_ENDIAN)

    pending = {}                         # reg -> hi value from a lui
    for ins in md.disasm(d[off:off + n], addr):
        note = ""
        parts = [p.strip() for p in ins.op_str.split(",")]
        if ins.mnemonic == "lui" and len(parts) == 2:
            try:
                pending[parts[0]] = int(parts[1], 0) << 16
            except ValueError:
                pass
        elif ins.mnemonic in ("addiu", "ori") and len(parts) == 3:
            reg, src, imm = parts
            if src in pending:
                try:
                    v = int(imm, 0)
                    if ins.mnemonic == "addiu" and v > 0x7FFF:
                        v -= 0x10000
                    full = (pending[src] + v) & 0xFFFFFFFF
                    s = read_cstring(d, base, full)
                    note = f"   ; 0x{full:08x}" + (f'  "{s}"' if s else "")
                    pending.pop(src, None)
                except ValueError:
                    pass
        print(f"0x{ins.address:08x}  {ins.bytes.hex():<9} "
              f"{ins.mnemonic:<9} {ins.op_str}{note}")


def cmd_gp(args):
    """Find every gp-relative access to one global."""
    base = infer_base(args.section, args.base)
    d = open(args.section, "rb").read()
    want = int(args.offset, 0) & 0xFFFF
    print(f"gp-relative accesses at offset 0x{want:04x}:")
    found = 0
    for i in range(0, len(d) - 4, 4):
        w = struct.unpack_from("<I", d, i)[0]
        op = w >> 26
        if op not in GP_OPS or ((w >> 21) & 0x1F) != GP_REG:
            continue
        if (w & 0xFFFF) != want:
            continue
        rt = (w >> 16) & 0x1F
        kind = "write" if GP_OPS[op].startswith("s") else "read "
        print(f"  0x{base+i:08x}  {kind}  {GP_OPS[op]} $r{rt}")
        found += 1
    print(f"  {found} access(es)")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", type=lambda s: int(s, 0),
                    help="override the inferred load base")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("xref", help="find code referencing a string or address")
    p.add_argument("section")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--string")
    g.add_argument("--addr")
    p.add_argument("--pointers", action="store_true",
                   help="also report bare pointer words (tables, vtables)")
    p.set_defaults(func=cmd_xref)

    p = sub.add_parser("dis", help="disassemble, annotating string references")
    p.add_argument("section")
    p.add_argument("--addr", required=True)
    p.add_argument("--count", type=int, default=48, help="instructions (default 48)")
    p.set_defaults(func=cmd_dis)

    p = sub.add_parser("gp", help="find accesses to a gp-relative global")
    p.add_argument("section")
    p.add_argument("--offset", required=True, help="e.g. -0x3ee4 or 0xc11c")
    p.set_defaults(func=cmd_gp)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
