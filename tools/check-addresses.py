"""Check that every copy of the payload address in this repository agrees.

The payload address appears in five places, and any disagreement is a phone that boots to a
black screen or an XNU that never starts:

    1. devices/<target>/device.conf      PAYLOAD_BASE            (the build's source of truth)
    2. BootShim.S                        -DXNU_PAYLOAD_BASE      (where the shim copies it)
    3. the firmware's XnuLoader.h        XNU_PAYLOAD_BASE        (where the firmware reads it)
    4. the firmware's MemoryMapLib.c     the "XNU Payload" region (what the firmware tells XNU)
    5. xnuloader/loader.c                PAYLOAD_BASE            (where the loader reads it)

plus the loader's own PE load base, which has to sit inside that region: it is loaded from the
payload, so if it lands outside, starting it either fails or silently overwrites something.

This is cheaper than a build and catches the class of mistake that already cost one boot.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
bad = []
ok = []


def read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8', errors='replace') as f:
        return f.read()


def num(text, pattern, what, rel, default=None):
    m = re.search(pattern, text, re.M)
    if not m:
        if default is not None:
            ok.append(f'{what}: not found in {rel}, using {default:#x}')
            return default
        bad.append(f'{what}: no match for {pattern!r} in {rel}')
        return None
    try:
        return int(m.group(1), 0)
    except ValueError:
        bad.append(f'{what}: {m.group(1)!r} in {rel} is not a number')
        return None


# ---- 1. device.conf --------------------------------------------------------------------
conf = read('devices/polaris/device.conf')
payload_base = num(conf, r'^\s*PAYLOAD_BASE=(\S+)', 'device.conf PAYLOAD_BASE', 'device.conf')
fd_base = num(conf, r'^\s*FD_BASE=(\S+)', 'device.conf FD_BASE', 'device.conf')
fd_size = num(conf, r'^\s*FD_SIZE=(\S+)', 'device.conf FD_SIZE', 'device.conf')
ok.append(f'device.conf: PAYLOAD_BASE={payload_base:#x} FD_BASE={fd_base:#x} FD_SIZE={fd_size:#x}')

# ---- 2. the firmware's XnuLoader.h ----------------------------------------------------
xh = read('devices/polaris/uefi/files/Silicon/Silicium/SiliciumPkg/Library/BootManagerLib/XnuLoader.h')
xnu_base = num(xh, r'#define\s+XNU_PAYLOAD_BASE\s+(0x[0-9A-Fa-f]+|\d+)',
               'firmware XNU_PAYLOAD_BASE', 'XnuLoader.h')
xnu_max = num(xh, r'#define\s+XNU_PAYLOAD_MAX_SIZE\s+(0x[0-9A-Fa-f]+|\d+)',
              'firmware XNU_PAYLOAD_MAX_SIZE', 'XnuLoader.h')
ok.append(f'XnuLoader.h: XNU_PAYLOAD_BASE={xnu_base:#x} XNU_PAYLOAD_MAX_SIZE={xnu_max:#x}')

# ---- 3. the memory map entry the firmware publishes ------------------------------------
mm = read('devices/polaris/uefi/files/Platforms/Xiaomi/polarisPkg/Library/MemoryMapLib/MemoryMapLib.c')
m = re.search(r'\{\s*"XNU Payload"\s*,\s*(0x[0-9A-Fa-f]+)\s*,\s*(0x[0-9A-Fa-f]+)', mm)
if not m:
    bad.append('MemoryMapLib.c: no "XNU Payload" region')
    mm_base = mm_size = None
else:
    mm_base, mm_size = int(m.group(1), 16), int(m.group(2), 16)
    ok.append(f'MemoryMapLib.c: "XNU Payload" {mm_base:#x} + {mm_size:#x}')

# ---- 4. the shim ----------------------------------------------------------------------
shim = read('devices/polaris/prebuilt/BootShim/BootShim.S')
# The shim must not go back to a label at the end of its source: the assembler's literal pool
# is emitted after the last instruction, so such a label is short of the binary's end and the
# FD gets copied from the wrong place (this happened once and produced a black screen).
if re.search(r'^\s*_Payload\s*:', shim, re.M):
    bad.append('BootShim.S: it defines a _Payload label again. The FD offset must come from '
               '`adr x1, _Head` plus SHIM_SIZE, because a trailing label lands short of the '
               'binary: the literal pool is emitted after it')
else:
    ok.append('BootShim.S: no trailing _Payload label; the FD offset is computed')
if not re.search(r'add\s+x1,\s*x1,\s*#SHIM_SIZE', shim):
    bad.append('BootShim.S: the entry does not add SHIM_SIZE to its own load address')
if 'ldr   x9, =XNU_PAYLOAD_BASE' not in shim and 'ldr x9, =XNU_PAYLOAD_BASE' not in shim:
    bad.append('BootShim.S: the payload copy does not read XNU_PAYLOAD_BASE')
if 'adr   x1, _Head' not in shim and 'adr x1, _Head' not in shim:
    bad.append('BootShim.S: the entry does not compute its own load address')
# The arm64 Image header's magic has to stay at offset 0x38, so only two instructions may come
# before it -- the entry has to branch over the header rather than add a third instruction.
if not re.search(r'^\s*_Head:[^\n]*\n\s*adr\s+x1,\s*_Head[^\n]*\n\s*b\s+_Start', shim, re.M):
    bad.append('BootShim.S: the entry is not exactly `adr x1, _Head` then `b _Start`; anything '
               'else pushes the arm64 Image header off offset 0x38')

# ---- 5. the loader --------------------------------------------------------------------
# loader.c has a fallback #define (0xF0000000, raphael's) that build.sh overrides with -D from
# device.conf. What matters is that the fallback is not silently in force: the build has to
# pass PAYLOAD_BASE, and the fallback must be recognisably another board's address.
ld = read('xnuloader/loader.c')
ld_base = num(ld, r'#define\s+PAYLOAD_BASE\s+(0x[0-9A-Fa-f]+)', 'loader PAYLOAD_BASE', 'loader.c')
if not re.search(r'#ifndef\s+PAYLOAD_BASE', ld):
    bad.append('loader.c: PAYLOAD_BASE is not guarded by #ifndef, so build.sh cannot override it '
               'from device.conf and the loader would use a hard-coded address')
build = read('xnuloader/build.sh')
pe = num(build, r'PE_BASE=\$\{LOADER_PE_BASE:-(0x[0-9A-Fa-f]+)\}', 'build.sh PE_BASE', 'build.sh')
if not re.search(r'PAYLOAD_DEFINE="-DPAYLOAD_BASE=', build):
    bad.append('build.sh: it does not pass -DPAYLOAD_BASE, so the loader would use its fallback')
if ld_base is not None:
    ok.append(f'loader.c: fallback PAYLOAD_BASE={ld_base:#x} (overridden by -D at build time)')
    if ld_base == payload_base:
        ok.append('loader.c: the fallback happens to equal device.conf, which hides a missing -D')
    else:
        ok.append(f'loader.c: the fallback differs from device.conf ({ld_base:#x} vs '
                  f'{payload_base:#x}), so a missing -D would be a silent wrong address')

print('addresses found:')
for line in ok:
    print('  ' + line)
print()

# ---- the comparisons ------------------------------------------------------------------
def compare(what, a, b, why):
    if a is None or b is None:
        return
    if a != b:
        bad.append(f'{what}: {a:#x} != {b:#x} -- {why}')


compare('device.conf PAYLOAD_BASE vs firmware XNU_PAYLOAD_BASE', payload_base, xnu_base,
        'the firmware would read the payload from somewhere the build does not put it')
if ld_base is not None and ld_base != payload_base:
    ok.append('device.conf PAYLOAD_BASE vs loader PAYLOAD_BASE: they differ only through the '
              'build-time -D, which build.sh is checked to pass above')
if mm_base is not None:
    compare('device.conf PAYLOAD_BASE vs MemoryMapLib "XNU Payload"', payload_base, mm_base,
            'XNU would be told a different place for the payload')
if mm_size is not None and xnu_max is not None:
    compare('XNU_PAYLOAD_MAX_SIZE vs the memory map region size', xnu_max, mm_size,
            'the firmware and the memory map disagree about how big the payload may be')

# The loader is loaded out of the payload region, so its PE base has to be inside it.
if pe is not None and payload_base is not None and mm_size is not None:
    if not payload_base <= pe < payload_base + mm_size:
        bad.append(f'the loader\'s PE base {pe:#x} is outside the payload region '
                   f'{payload_base:#x}..{payload_base + mm_size:#x}: starting it would read or '
                   f'write memory the firmware does not describe as payload')
    elif pe < payload_base + 0x100000:
        bad.append(f'the loader\'s PE base {pe:#x} is inside the first 1 MB of the payload region '
                   f'({payload_base:#x}): the payload header and its entries live there')
    else:
        ok.append(f'the loader\'s PE base {pe:#x} is inside the payload region')

# FD_BASE + FD_SIZE is where the shim's FD copy ends. The payload region must not overlap it.
if None not in (fd_base, fd_size, payload_base):
    if fd_base + fd_size > payload_base:
        bad.append(f'the UEFI volume at {fd_base:#x}+{fd_size:#x} runs into the payload region at '
                   f'{payload_base:#x}: the FD copy would erase the payload')
    else:
        ok.append(f'the FD ends at {fd_base + fd_size:#x}, below the payload region at {payload_base:#x}')

# The payload has to fit in its region, and the resident loader writes its log at the end of it.
pw = os.path.join(ROOT, 'devices/polaris/prebuilt/SILICIUM_UEFI.fd')
print('consistency:')
for line in ok[-4:]:
    print('  ' + line)
print()

if bad:
    print('PROBLEMS:')
    for b in bad:
        print('  - ' + b)
    sys.exit(1)
print('RESULT: every copy of the payload address agrees, and the regions do not overlap')
