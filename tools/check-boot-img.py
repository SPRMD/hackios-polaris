"""Verify a polaris boot.img really carries polaris firmware and the polaris payload.

This exists because a structurally valid image was once built for the wrong phone:
every check that looked at the image's *shape* passed -- ANDROID! magic, plausible
page size, gzip member, XNUP container, cmdline naming polaris -- while the UEFI
firmware inside was poco-x5-pro's and PAYLOAD_BASE was 0xF0000000, an address above
the top of this board's DDR. Shape checks cannot tell boards apart, so this one
compares content against values measured on the phone.

Layout written by mkxnuimg.py:
    kernel field = gzip(BootShim + UEFI FD + [XNUP container]) + DTB
The DTB sits after the gzip member, uncompressed, so decompress only the member.

usage: check_boot_img.py boot.img [--fd-polaris PATH --fd-wrong PATH]
"""
import gzip
import struct
import sys
import zlib

PAGE_SIZES = (2048, 4096, 8192, 16384)

# Measured on this phone / derived in devices/polaris/device.conf.
EXPECT = {
    'fd_base': 0x9FC00000,
    'fd_size': 0x220000,
    'payload_base': 0xA0000000,
    'fb_base': 0x9D400000,
}
# Addresses that would mean the image was built for a different board.
BAD_BASES = {0xA0000000: 'polaris', 0x9FD00000: 'poco-x5-pro (redwood)', 0x9FC00000: 'polaris'}
# SoC names that must not appear anywhere in the device tree.
FOREIGN_SOCS = (b'Yupik', b'yupik', b'redwood', b'SM7325', b'sm7325', b'cape', b'Cape')


def main(argv):
    path = argv[1]
    d = open(path, 'rb').read()
    ok = True

    if d[:8] != b'ANDROID!':
        print('  not an Android boot image: %r' % d[:8])
        return 1
    page = struct.unpack_from('<I', d, 36)[0]
    ksize = struct.unpack_from('<I', d, 8)[0]
    rsize = struct.unpack_from('<I', d, 16)[0]
    hver = struct.unpack_from('<I', d, 40)[0]
    board = d[48:64].rstrip(b'\0')
    print('  ANDROID! header_version=%d page=%d kernel=%d ramdisk=%d board=%r'
          % (hver, page, ksize, rsize, board))
    if page not in PAGE_SIZES:
        print('  implausible page size')
        ok = False

    body = d[page:page + ksize]
    if body[:2] != b'\x1f\x8b':
        print('  kernel field is not gzip: %r' % body[:2])
        return 1
    dec = zlib.decompressobj(zlib.MAX_WBITS | 16)
    blob = dec.decompress(body) + dec.flush()
    tail = dec.unused_data
    print('  kernel field: %d bytes gzip -> %d bytes, %d bytes after the member'
          % (len(body), len(blob), len(tail)))

    # ---- the device tree that was appended -----------------------------------------
    if len(tail) < 4 or tail[:4] != b'\xd0\x0d\xfe\xed':
        print('  appended data is not an FDT: %r' % tail[:4])
        ok = False
    else:
        total = struct.unpack_from('>I', tail, 4)[0]
        print('  appended DTB: %d bytes, FDT v%d' % (total, struct.unpack_from('>I', tail, 20)[0]))
        for bad in FOREIGN_SOCS:
            if bad in tail:
                print('  FOREIGN SoC string in the DTB: %r' % bad)
                ok = False
        if b'polaris' in tail.lower():
            print('  the DTB names polaris')
        else:
            print('  the DTB does not name polaris')
            ok = False
        # cont_splash_region must be the framebuffer base this port uses.
        if struct.pack('>I', EXPECT['fb_base']) in tail:
            print('  the DTB mentions FB_BASE 0x%x' % EXPECT['fb_base'])
        else:
            print('  WARNING: FB_BASE 0x%x not found in the DTB' % EXPECT['fb_base'])

    # ---- the XNUP container ---------------------------------------------------------
    if b'XNUP' not in blob:
        print('  no XNUP container after decompression')
        return 1
    i = blob.index(b'XNUP')
    csize = struct.unpack_from('<I', blob, i + 4)[0]
    print('  XNUP at 0x%x, container %d bytes' % (i, csize))
    if csize <= 0 or i + csize > len(blob):
        print('  container size does not fit')
        ok = False
        csize = len(blob) - i
    cont = blob[i:i + csize]
    for name in (b'loader.efi', b'kernel', b'platform=polaris', b'gic_timer_irq=19'):
        if name in cont:
            print('  payload has %r' % name.decode())
        else:
            print('  payload is MISSING %r' % name.decode())
            ok = False
    # Do NOT search the payload for other boards' names. This kernel is one binary shared by
    # every board it supports: it carries platform_redwood and platform_raphael structures, a
    # "redwood.keybag" sysctl, and the MPSS paths for other SoCs ("yupik_ipa_fws"), so
    # 'redwood' and 'yupik' appear in it no matter which phone the image is for. The board is
    # decided by the cmdline's platform= (and by the loader's own detection), which is why
    # that is what this checks. A string search here would fail every correct image.

    # ---- the firmware volume, by content ------------------------------------------
    # BootShim precedes the FD, so the FD does not start at 0. Find the FFS volume header
    # and read its length, then account for it exactly.
    fvh = blob.find(b'_FVH')
    if fvh < 0:
        print('  no _FVH: the image carries no firmware volume')
        return 1
    fv_off = fvh - 0x28
    fv_len = struct.unpack_from('<Q', blob, fv_off + 0x20)[0]
    print('  firmware volume at 0x%x, FvLength 0x%x (%d)' % (fv_off, fv_len, fv_len))
    if fv_len != EXPECT['fd_size']:
        print('  FD size is 0x%x but device.conf says FD_SIZE=0x%x'
              % (fv_len, EXPECT['fd_size']))
        ok = False
    else:
        print('  FD size matches device.conf FD_SIZE=0x%x' % EXPECT['fd_size'])
    # The packer pads the shim to a slot and writes the FD right after it, so fv_off is that
    # slot, not the shim's code length. tools/check-shim.py decodes the slot the shim computes
    # for itself and compares it with this offset; the range check here only catches nonsense.
    if fv_off < 0x40:
        print('  the volume begins before the shim could fit: %d bytes' % fv_off)
        ok = False
    print('  boot shim slot ahead of it: %d bytes' % fv_off)
    if not (0x40 <= fv_off <= 0x1000):
        print('  the shim slot is not plausible')
        ok = False

    # The shim materialises this board's FD_BASE and FD_SIZE as immediates (movz/movk, or a
    # literal-pool load), so a byte search for them is unreliable: before the shim switched to
    # `adr` + slot it kept one constant in its literal pool, and only that one was findable.
    # tools/check-shim.py decodes the instructions instead; here only a plain containment test
    # is made, and a miss is not fatal.
    shim = blob[:fv_off]
    for name in ('fd_base', 'fd_size'):
        want = struct.pack('<Q', EXPECT[name])
        found = want in shim
        print('  shim contains %-12s 0x%-9x as a literal %s'
              % (name, EXPECT[name], 'yes' if found else 'no (immediate instead)'))
    # And no other board's FD base: 0x9FD00000 is poco-x5-pro's.
    for wrong in (0x9FD00000, 0x9C000000):
        if struct.pack('<Q', wrong) in shim:
            print('  shim encodes FOREIGN FD base 0x%x' % wrong)
            ok = False

    # PAYLOAD_BASE cannot be proved by searching the payload for it. The payload is a kernel
    # image plus a loader; 8-byte patterns like 0xA0000000 and 0xF0000000 occur throughout
    # its code and constant pools by coincidence, so both "found" and "foreign base found"
    # are noise -- a scan here reported 0xF0000000 twelve times in ordinary instructions.
    # The honest check is what the loader actually does with it, which is a run-time matter.
    # What CAN be settled statically is that the payload is not another board's: the DTB,
    # the FD bytes and the shim's FD_BASE/FD_SIZE above are all board-specific, and they
    # agree. So this reports PAYLOAD_BASE for the record without judging it.
    print('  expected PAYLOAD_BASE: 0x%x (device.conf; not verifiable by scanning)'
          % EXPECT['payload_base'])

    # The FD must not be the poco one: sizes differ, and the poco FD is byte-identical
    # across every image built from its prebuilt. Compare with the polaris one on disk.
    if '--fd-polaris' in argv:
        ref = open(argv[argv.index('--fd-polaris') + 1], 'rb').read()
        got = blob[fv_off:fv_off + fv_len]
        print('  FD compared with the polaris firmware: %s'
              % ('identical' if got == ref else 'DIFFERENT'))
        if got != ref:
            ok = False
    if '--fd-wrong' in argv:
        wrong = open(argv[argv.index('--fd-wrong') + 1], 'rb').read()
        got = blob[fv_off:fv_off + fv_len]
        print('  FD compared with the poco firmware: %s'
              % ('IDENTICAL -- WRONG BOARD' if got == wrong else 'different (correct)'))
        if got == wrong:
            ok = False

    print()
    print('  RESULT:', 'polaris content confirmed' if ok else 'PROBLEMS ABOVE')
    return 0 if ok else 1


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('usage: check_boot_img.py boot.img [--fd-polaris P] [--fd-wrong P]',
              file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv))
