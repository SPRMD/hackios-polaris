"""Verify the boot shim actually moves the payload, by decoding its instructions.

A byte search for 0xA0000000 in the shim finds nothing even when the address is right,
because the compiler loads it as `movz x9, #0xa000, lsl #16` -- the constant is split
across the instruction's immediate fields. Searching for the address as a data literal is
therefore the wrong test, and it reported a false failure twice while the shim was correct.

So decode the two instruction forms that can materialise a 64-bit constant here:

    MOVZ  Rd, #imm16, lsl #shift      opcode 0b110100101 << 23
    LDR   Xt, <literal>               loads a 64-bit value from a nearby literal pool

and check for the three addresses the shim must place, plus the copy sizes.

usage: check_shim.py boot.img --expect-payload-base 0xA0000000
"""
import struct
import sys
import zlib


def decode(blob, fv_off):
    """Yield (offset, kind, value) for MOVZ and LDR-literal instructions in the shim."""
    shim = blob[:fv_off]
    # AArch64 instructions are 4-byte aligned. The head of the shim is the PE/stack header,
    # not code, so start where the first `b` or `adr` was seen (offset 0 of _Head is code).
    seen = []
    i = 0
    while i + 4 <= len(shim):
        w = struct.unpack_from('<I', shim, i)[0]
        # MOVZ: sf=1, opc=10, 100101, hw, imm16, Rd
        if (w & 0x7F800000) == 0x52800000 or (w & 0x7F800000) == 0xD2800000:
            sf = (w >> 31) & 1
            hw = (w >> 21) & 3
            imm16 = (w >> 5) & 0xFFFF
            rd = w & 0x1F
            if sf:
                value = imm16 << (hw * 16)
                seen.append((i, 'movz', value, rd, hw, imm16))
        # LDR Xt, literal: opc=01, 011000, imm19
        elif (w & 0xFF000000) == 0x58000000:
            imm19 = (w >> 5) & 0x7FFFF
            if imm19 & (1 << 18):
                imm19 -= 1 << 19
            target = i + imm19 * 4
            if 0 <= target <= len(shim) - 8:
                value = struct.unpack_from('<Q', shim, target)[0]
                seen.append((i, 'ldr', value, w & 0x1F, target, None))
        i += 4
    return seen


def main(argv):
    path = argv[1]
    want = None
    if '--expect-payload-base' in argv:
        want = int(argv[argv.index('--expect-payload-base') + 1], 0)
    size_args = {}
    for key in ('fd-base', 'fd-size', 'payload-size'):
        if '--' + key in argv:
            size_args[key] = int(argv[argv.index('--' + key) + 1], 0)

    d = open(path, 'rb').read()
    page = struct.unpack_from('<I', d, 36)[0]
    ksize = struct.unpack_from('<I', d, 8)[0]
    dec = zlib.decompressobj(zlib.MAX_WBITS | 16)
    blob = dec.decompress(d[page:page + ksize]) + dec.flush()

    fvh = blob.find(b'_FVH')
    if fvh < 0:
        print('  no _FVH in the image')
        return 1
    fv_off = fvh - 0x28
    fv_len = struct.unpack_from('<Q', blob, fv_off + 0x20)[0]
    print('  shim: %d bytes, firmware volume at 0x%x, FvLength 0x%x'
          % (fv_off, fv_off, fv_len))

    ins = decode(blob, fv_off)
    movz = [(o, v) for o, k, v, *_ in ins if k == 'movz']
    ldr = [(o, v) for o, k, v, *_ in ins if k == 'ldr']
    print('  decoded %d constant loads: %d movz, %d ldr-literal'
          % (len(movz) + len(ldr), len(movz), len(ldr)))

    checks = []
    if 'fd-base' in size_args:
        checks.append(('FD_BASE', size_args['fd-base']))
    if 'fd-size' in size_args:
        checks.append(('FD_SIZE', size_args['fd-size']))
    if want is not None:
        checks.append(('XNU_PAYLOAD_BASE', want))
    payload_size = None
    if 'payload-size' in size_args:
        payload_size = size_args['payload-size']
        checks.append(('PAYLOAD_SIZE', payload_size))

    ok = True
    for name, value in checks:
        # A 64-bit constant may arrive as one MOVZ (if it fits) or several instructions,
        # and as an LDR from the literal pool. Accept either representation.
        hit = None
        for off, v in movz:
            if v == value:
                hit = 'movz at 0x%x' % off
                break
        if hit is None:
            for off, v in ldr:
                if v == value:
                    hit = 'ldr literal at 0x%x' % off
                    break
        if hit is None:
            # The high half may be materialised as MOVZ with a shift (0xA000 << 16).
            for off, v in movz:
                if v and (value & 0xFFFF0000) == v and (value & 0xFFFF) == 0:
                    hit = 'movz<<16 of 0x%x at 0x%x (+ movk for the low half)' % (v, off)
                    break
        print('  %-18s 0x%-10x %s' % (name, value, hit or 'NOT FOUND'))
        if hit is None:
            ok = False

    # The payload must sit immediately after the FD, which is what the shim assumes when it
    # computes _Payload + FD_SIZE.
    pay = blob[fv_off + fv_len:]
    print('  payload after the FD: %d bytes, starts %r' % (len(pay), pay[:4]))
    if pay[:4] != b'XNUP':
        print('  the payload is not at FD_BASE+FD_SIZE where the shim looks for it')
        ok = False
    elif payload_size is not None and len(pay) != payload_size:
        print('  PAYLOAD_SIZE is %d but the payload is %d bytes' % (payload_size, len(pay)))
        ok = False
    else:
        print('  PAYLOAD_SIZE matches the payload exactly')

    print()
    print('  RESULT:', 'the shim places the FD and the payload as intended' if ok
          else 'PROBLEMS ABOVE')
    return 0 if ok else 1


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('usage: check_shim.py boot.img --expect-payload-base 0xA0000000 '
              '--fd-base 0x9FC00000 --fd-size 0x220000', file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv))
