"""Verify the boot shim actually places the firmware volume and the payload, by decoding it.

Two classes of mistake this catches, both of which cost a phone boot:

1. A byte search for 0xA0000000 in the shim finds nothing even when the address is right,
   because the compiler materialises it as `movz x9, #0xa000, lsl #16` -- the constant is split
   across the instruction's immediate fields. Searching for the address as a data literal is
   therefore the wrong test. So decode the forms that can materialise a 64-bit constant:

       MOVZ  Rd, #imm16, lsl #shift      opcode 0b110100101 << 23
       LDR   Xt, <literal>               loads a 64-bit value from a nearby literal pool

2. The FD's position in the image is the shim's own size, and the shim computes it from its
   load address with `adr x1, _Head; add x1, x1, #SHIM_SIZE`. If those two disagree -- which is
   what happens when the shim uses a label at the end of its source file, because the literal
   pool is emitted after that label -- the firmware volume is copied to FD_BASE shifted by the
   size of the pool. Its header is then garbage, the firmware never starts, and the phone shows
   a black screen with the backlight on. That happened once: the shim said 192, the FD was at
   200, and the pool was the missing 8 bytes. So decode the pair and compare it with where the
   firmware volume actually is in the image.

usage: check-shim.py boot.img --expect-payload-base 0xA0000000 --fd-base 0x9FC00000 \
           --fd-size 0x220000 [--payload-size N]
"""
import struct
import sys
import zlib


def decode_constants(blob, shim_len):
    """Return (offset, kind, value) for MOVZ and LDR-literal instructions in the shim."""
    shim = blob[:shim_len]
    seen = []
    i = 0
    while i + 4 <= len(shim):
        w = struct.unpack_from('<I', shim, i)[0]
        # MOVZ: sf=1, opc=10, 100101, hw, imm16, Rd
        if (w & 0x7F800000) == 0x52800000 or (w & 0x7F800000) == 0xD2800000:
            sf = (w >> 31) & 1
            hw = (w >> 21) & 3
            imm16 = (w >> 5) & 0xFFFF
            if sf:
                seen.append((i, 'movz', imm16 << (hw * 16)))
        # LDR Xt, literal: opc=01, 011000, imm19
        elif (w & 0xFF000000) == 0x58000000:
            imm19 = (w >> 5) & 0x7FFFF
            if imm19 & (1 << 18):
                imm19 -= 1 << 19
            target = i + imm19 * 4
            if 0 <= target <= len(shim) - 8:
                seen.append((i, 'ldr', struct.unpack_from('<Q', shim, target)[0]))
        i += 4
    return seen


def decode_slot(blob):
    """Decode the shim's entry and return the FD offset it computes, or (None, why).

    The entry is `adr x1, _Head; b _Start` at offset 0 -- code0/code1 of the arm64 Image header,
    which keeps the header's magic at its fixed offset 0x38 -- and the body at the branch target
    starts with `add x1, x1, #SHIM_SIZE`.
    """
    if len(blob) < 0x48:
        return None, 'the image is too short to hold the shim\'s entry and its arm64 header'
    adr, br = struct.unpack_from('<II', blob, 0)
    if (adr & 0x9F000000) != 0x10000000 or (adr & 0x1F) != 1:
        return None, 'the shim does not begin with `adr x1, _Head` (got %08x)' % adr
    imm = (((adr >> 5) & 0x7FFFF) << 2) | ((adr >> 29) & 3)
    if imm & 0x100000:
        imm -= 0x200000
    if imm != 0:
        return None, ('the shim\'s `adr x1, _Head` is not 0, so its entry point is not the first '
                      'instruction and it cannot know its own load address')
    if (br & 0xFC000000) != 0x14000000:
        return None, 'the shim\'s second instruction is not a branch (got %08x)' % br
    # B is relative to the branch instruction itself, which sits at offset 4.
    target = 4 + (br & 0x3FFFFFF) * 4
    if target & 0x8000000:
        target -= 0x10000000
    if target + 8 > len(blob):
        return None, 'the shim branches to %#x, past its end' % target
    add = struct.unpack_from('<I', blob, target)[0]
    if (add & 0xFF800000) != 0x91000000 or (add & 0x1F) != 1 or ((add >> 5) & 0x1F) != 1:
        return None, ('the shim\'s body at %#x does not start with `add x1, x1, #SHIM_SIZE` '
                      '(got %08x); a shim packed by this repository does' % (target, add))
    if (add >> 22) & 3:
        return None, 'the shim adds its slot shifted, which the packer does not expect'
    return (add >> 10) & 0xFFF, None


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
    # The firmware volume proper starts 0x28 bytes into the FD: the first bytes are the stub
    # that the firmware's own build puts in front of the FV header.
    fv_off = fvh - 0x28
    fv_len = struct.unpack_from('<Q', blob, fv_off + 0x20)[0]
    print('  firmware volume at 0x%x, FvLength 0x%x, so the shim is %d bytes'
          % (fv_off, fv_len, fv_off))

    ok = True

    # The shim has to find the FD where it really is. This is the check that the 8-byte
    # literal-pool shift would have failed.
    slot, why = decode_slot(blob)
    if why:
        print("  the shim's FD offset: %s" % why)
        ok = False
    elif slot != fv_off:
        print('  the shim computes its FD offset as 0x%x, but the firmware volume is at 0x%x'
              % (slot, fv_off))
        print('  -> the shim would copy the volume to FD_BASE from %d bytes out of place:'
              % (fv_off - slot))
        print('     its header would be garbage and the firmware would never start')
        ok = False
    else:
        print('  the shim computes its FD offset as 0x%x, which is where the volume is' % slot)

    ins = decode_constants(blob, fv_off)
    movz = [(o, v) for o, k, v in ins if k == 'movz']
    ldr = [(o, v) for o, k, v in ins if k == 'ldr']
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

    for name, value in checks:
        # A 64-bit constant may arrive as one MOVZ (if it fits) or several instructions, and
        # as an LDR from the literal pool. Accept either representation.
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
    # adds FD_SIZE to the FD's address. The packer pads the blob out to a 16-byte boundary, so
    # the tail may carry up to 15 zero bytes beyond the payload itself.
    pay = blob[fv_off + fv_len:]
    print('  payload after the FD: %d bytes, starts %r' % (len(pay), pay[:4]))
    if pay[:4] != b'XNUP':
        print('  the payload is not at FD + FD_SIZE where the shim looks for it')
        ok = False
    elif payload_size is not None:
        pad = len(pay) - payload_size
        if not 0 <= pad < 16 or pay[payload_size:].strip(b'\0'):
            print('  PAYLOAD_SIZE is %d but the payload is %d bytes (padding %d)'
                  % (payload_size, len(pay), pad))
            ok = False
        else:
            print('  PAYLOAD_SIZE matches the payload (%d bytes, %d of padding)'
                  % (payload_size, pad))

    print()
    print('  RESULT:', 'the shim places the FD and the payload as intended' if ok
          else 'PROBLEMS ABOVE')
    return 0 if ok else 1


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('usage: check-shim.py boot.img --expect-payload-base 0xA0000000 '
              '--fd-base 0x9FC00000 --fd-size 0x220000', file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv))
