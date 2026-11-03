"""Audit the polaris UEFI firmware volume: structure, checksums, and what is really inside.

Why this exists: the volume holds the firmware compressed inside a GUID-defined section, so a
plain string search for our overlay's text ("HACKIOS POLARIS", "XNU Payload") finds nothing
even when the overlay is in there. That is not a defect in the firmware, it is a defect in the
test -- and a "the overlay is missing" verdict is the kind that would send someone rewriting a
working port. So this walks the volume, follows the sections, decompresses the LZMA one, and
looks for the markers in the decompressed bytes.

Checks, in the order a failure would hurt:
  1. the FV header is well formed and its checksum adds up (a corrupt header means the firmware
     never starts, which on a phone looks exactly like a dead device)
  2. FvLength matches the file, and the block map agrees with it
  3. every FFS file header checksum is valid and the files fit inside the volume
  4. the overlay is compiled in: the strings our XnuLoader prints, and the "XNU Payload" entry
     we added to the platform memory map, together with its base address

Two rules this file gets right, both learned the hard way:
  * an FFS header checksum is an 8-bit sum over the 24 header bytes with IntegrityCheck (2 bytes)
    and State (1 byte) taken as zero -- not a 16-bit sum, and not the raw bytes
  * the compression section's data starts at its DataOffset, which is 0x14 here, not 0x18

usage: check-fv.py [firmware.fd]
"""
import lzma
import struct
import sys
import uuid

PATH = sys.argv[1] if len(sys.argv) > 1 else 'devices/polaris/prebuilt/SILICIUM_UEFI.fd'
d = open(PATH, 'rb').read()
print('file: %s, %d bytes (0x%x)' % (PATH, len(d), len(d)))

problems = []


def u16(b, o):
    return struct.unpack_from('<H', b, o)[0]


def u32(b, o):
    return struct.unpack_from('<I', b, o)[0]


def u64(b, o):
    return struct.unpack_from('<Q', b, o)[0]


def guid(b, o):
    return str(uuid.UUID(bytes_le=bytes(b[o:o + 16])))


# ---- 1. the FV header ------------------------------------------------------------------
sig = d.find(b'_FVH')
if sig < 0:
    sys.exit('no _FVH: this is not a firmware volume')
fv = sig - 0x28            # the signature sits at 0x28 inside EFI_FIRMWARE_VOLUME_HEADER
print('FV header at 0x%x (signature at 0x%x)' % (fv, sig))
fv_len = u64(d, fv + 0x20)
hdr_len = u16(d, fv + 0x30)
checksum = u16(d, fv + 0x32)
print('  FileSystemGuid: %s' % guid(d, fv + 0x10))
print('  FvLength=0x%x Attributes=0x%x HeaderLength=0x%x Checksum=0x%x Revision=%d'
      % (fv_len, u32(d, fv + 0x2C), hdr_len, checksum, d[fv + 0x37]))

if fv_len != len(d) - fv:
    problems.append('FvLength 0x%x != the 0x%x bytes after the header' % (fv_len, len(d) - fv))
if not (0x38 <= hdr_len <= fv_len and hdr_len % 2 == 0):
    problems.append('HeaderLength 0x%x is not plausible' % hdr_len)
else:
    total = sum(u16(d, fv + i) for i in range(0, hdr_len, 2)) & 0xFFFF
    print('  header checksum: 16-bit sum over 0x%x bytes = 0x%04x %s'
          % (hdr_len, total, '(valid)' if total == 0 else '(INVALID)'))
    if total != 0:
        problems.append('the FV header checksum does not add up')

bm = fv + 0x38
blocks = []
while bm + 8 <= fv + hdr_len:
    num, length = u32(d, bm), u32(d, bm + 4)
    bm += 8
    if num == 0 and length == 0:
        break
    blocks.append((num, length))
print('  block map: %s' % ', '.join('%d x 0x%x' % b for b in blocks))
covered = sum(n * l for n, l in blocks)
if covered != fv_len:
    problems.append('the block map covers 0x%x, FvLength says 0x%x' % (covered, fv_len))

# ---- 2. the FFS files ------------------------------------------------------------------
print('FFS files:')
off = fv + hdr_len
files = 0
bodies = []
while off + 24 <= fv + fv_len:
    if d[off:off + 16] == b'\xff' * 16:
        print('  free space from 0x%x: %d bytes' % (off, (fv + fv_len) - off))
        break
    ftype = d[off + 18]
    fattr = d[off + 19]
    size = d[off + 20] | (d[off + 21] << 8) | (d[off + 22] << 16)
    hdr = bytearray(d[off:off + 24])
    hdr[17] = hdr[23] = 0      # IntegrityCheck.File and State: see the note at the top
    csum = sum(hdr) & 0xFF
    if csum != 0:
        problems.append('FFS file at 0x%x: header checksum is 0x%02x, not 0' % (off, csum))
    if size < 24 or off + size > fv + fv_len:
        problems.append('FFS file at 0x%x: implausible size 0x%x' % (off, size))
        break
    files += 1
    print('  at 0x%06x type=0x%02x attr=0x%02x size=0x%06x name=%s'
          % (off, ftype, fattr, size, guid(d, off)))
    bodies.append((off + 24, size - 24))
    off += (size + 7) & ~7
print('  %d files (0x03 = DXE core, 0x0B = MM standalone / the compressed FV wrapper)' % files)

# ---- 3. sections, and what is inside them ----------------------------------------------
print('sections:')
plain = bytearray(d)      # raw bytes, for anything stored uncompressed
unpacked = bytearray()    # decompressed bytes, for the markers
for start, length in bodies:
    o = start
    end = start + length
    while o + 4 <= end:
        ssize = d[o] | (d[o + 1] << 8) | (d[o + 2] << 16)
        stype = d[o + 3]
        if ssize < 4 or o + ssize > end:
            print('  at 0x%06x type=0x%02x size=0x%x (last section)' % (o, stype, ssize))
            break
        name = {0x01: 'COMPRESSION', 0x02: 'GUID_DEFINED', 0x10: 'PE32', 0x11: 'PIC',
                0x12: 'TE', 0x13: 'DXE_DEPEX', 0x14: 'VERSION', 0x15: 'UI', 0x17: 'FREEFORM',
                0x18: 'RAW', 0x19: 'RAW', 0x1B: 'PEI_DEPEX'}.get(stype, '?')
        extra = ''
        payload = d[o + 4:o + ssize]
        if stype == 0x02 and len(payload) >= 20:
            doff = u16(payload, 16)
            # DataOffset counts from the start of the section header, not from the end of the
            # fixed part: reading it relative to the payload starts four bytes late and LZMA
            # then rejects the stream.
            body = d[o + doff:o + ssize]
            extra = ' guid=%s data_offset=0x%x' % (guid(payload, 0), doff)
            # The stream is a bare LZMA1 stream with the 13-byte header the EDK2 tool emits
            # (5 property bytes, 8 size bytes). FORMAT_ALONE wants exactly that, but its size
            # field may be zero, so fall back to a raw stream with the properties from byte 0.
            out = None
            try:
                out = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(body)
            except Exception:
                props = body[0]
                if props < 225:
                    lc = props % 9
                    lp = (props // 9) % 5
                    pb = props // 45
                    ds = struct.unpack_from('<I', body, 1)[0]
                    filt = [{'id': lzma.FILTER_LZMA1, 'lc': lc, 'lp': lp, 'pb': pb,
                             'dict_size': max(ds, 1 << 12)}]
                    try:
                        out = lzma.LZMADecompressor(format=lzma.FORMAT_RAW,
                                                    filters=filt).decompress(body[13:])
                        extra += ' (raw lc=%d lp=%d pb=%d dict=%d)' % (lc, lp, pb, ds)
                    except Exception as e:
                        extra += ' -> not LZMA (%s)' % type(e).__name__
                else:
                    extra += ' -> not LZMA (bad properties byte 0x%02x)' % props
            if out is not None:
                unpacked += out
                extra += ' -> %d -> %d bytes' % (len(body), len(out))
        print('  at 0x%06x type=0x%02x %-12s size=0x%06x%s' % (o, stype, name, ssize, extra))
        o += (ssize + 3) & ~3

if not unpacked:
    problems.append('the volume has no compressed payload: nothing was decompressed')
print('  decompressed %d bytes' % len(unpacked))

# ---- 4. is our overlay in there? --------------------------------------------------------
print('markers:')
# The strings XnuLoader.c prints are CHAR16 (wide); the memory map names are CHAR8.
for text, wide in (('HACKIOS POLARIS', True), ('starting resident loader', True),
                   ('no resident loader running', True), ('XNU Payload', False)):
    at = unpacked.find(text.encode('utf-16-le') if wide else text.encode())
    print('  %-30s %s' % (text, ('at 0x%x' % at) if at >= 0 else 'NOT PRESENT'))
    if at < 0:
        problems.append('%r is not in the decompressed firmware' % text)

# The memory map entry we added must carry PAYLOAD_BASE, or the firmware would read the payload
# from somewhere else. Layout: name[32], base u64, size u64, ...
i = unpacked.find(b'XNU Payload')
if i >= 0:
    base = u64(unpacked, i + 32)
    size = u64(unpacked, i + 40)
    print('  the "XNU Payload" region: base=0x%x size=0x%x' % (base, size))
    if base != 0xA0000000:
        problems.append('the payload region is based at 0x%x, not 0xa0000000' % base)

print()
if problems:
    print('PROBLEMS:')
    for p in problems:
        print('  - %s' % p)
    sys.exit(1)
print('RESULT: the volume is structurally sound and carries this repository\'s overlay')
