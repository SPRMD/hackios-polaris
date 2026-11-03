#!/usr/bin/env python3
"""
Pack the redwood UEFI (with the hook that starts the XNU loader) and an optional payload
into an Android boot image for `fastboot boot`.

  kernel = gzip(BootShim + UEFI FD + ["XNUP" container]) + DTB

The boot shim copies the FD to its base and the payload to PAYLOAD_BASE.

The UEFI pieces are taken from a Mu-Silicium tree built here (Mu-Silicium/, see uefi/README.md)
or, when there is none, from prebuilt/uefi/.

usage: mkxnuimg.py [-p payload.bin] [-o out.img]

Another device (common/devices/<name>.conf, read by common/build.sh) sets these in the environment:
  UEFI_TARGET   Mu-Silicium device name (default redwood)    UEFI_FD, DTB   files to use instead
  FD_BASE, FD_SIZE, PAYLOAD_BASE                              addresses from its UEFI port's memory map
"""
import argparse, gzip, os, struct, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
MU = os.path.join(HERE, 'Mu-Silicium')
PREBUILT = os.path.join(HERE, 'devices', 'poco-x5-pro', 'prebuilt')
def env_int(name, default):
    v = os.environ.get(name, '')
    return int(v, 0) if v else default
TARGET = os.environ.get('UEFI_TARGET') or 'redwood'
FD_BASE, FD_SIZE = env_int('FD_BASE', 0x9FD00000), env_int('FD_SIZE', 0x200000)
PAYLOAD_BASE, PAYLOAD_MAX = env_int('PAYLOAD_BASE', 0xF0000000), 0x04000000

# The shim is padded to this many bytes and the FD is written right after the padding, so the
# FD's offset in the image is a constant the shim can compute from its own load address. It
# cannot use a label at the end of its source file: the assembler emits the literal pool for
# the `ldr =...` instructions after the last instruction, so such a label lands short of the
# binary's end and the FD is then copied from the wrong place. 16-byte aligned, so the LDP/STP
# copy in the shim never runs unaligned, and with room for the shim to grow.
SHIM_SLOT = 0x400


def shim_slot_from_code(shim):
    """Decode the shim's entry and return the slot it adds to its own load address.

    The shape is `adr x1, _Head; b _Start` at offset 0 (code0/code1 of the arm64 Image header,
    so that the header's magic stays at offset 0x38) followed by
    `add x1, x1, #SHIM_SIZE` at the branch target. This is the contract with
    devices/<target>/prebuilt/BootShim/BootShim.S: the shim finds the FD by adding its slot to
    its own load address, and the packer writes the FD at that exact offset. The two have to
    agree, or the firmware volume is copied from the wrong place, its header is garbage, and
    the phone shows a black screen.
    """
    if len(shim) < 0x48:
        sys.exit('mkxnuimg: the boot shim is too short to hold its entry and the arm64 header')
    adr, br = struct.unpack_from('<II', shim, 0)
    if (adr & 0x9F000000) != 0x10000000 or (adr & 0x1F) != 1:
        sys.exit('mkxnuimg: the boot shim does not begin with `adr x1, _Head`')
    imm = (((adr >> 5) & 0x7FFFF) << 2) | ((adr >> 29) & 3)
    if imm & 0x100000:
        imm -= 0x200000
    if imm != 0:
        sys.exit('mkxnuimg: the boot shim\'s `adr x1, _Head` is not 0, so its entry point is not '
                 'the first instruction')
    if (br & 0xFC000000) != 0x14000000:
        sys.exit('mkxnuimg: the second instruction of the boot shim is not a branch, which would '
                 'push the arm64 Image header off its fixed offset')
    # B is relative to the branch instruction itself, which sits at offset 4.
    target = 4 + (br & 0x3FFFFFF) * 4
    if target & 0x8000000:
        target -= 0x10000000
    if target + 8 > len(shim):
        sys.exit(f'mkxnuimg: the boot shim branches to {target:#x}, past its end')
    add = struct.unpack_from('<I', shim, target)[0]
    if (add & 0xFF800000) != 0x91000000 or (add & 0x1F) != 1 or ((add >> 5) & 0x1F) != 1:
        sys.exit(f'mkxnuimg: the boot shim does not begin its body at {target:#x} with '
                 '`add x1, x1, #SHIM_SIZE`')
    if (add >> 22) & 3:
        sys.exit('mkxnuimg: the boot shim\'s slot is added shifted; expected a plain add')
    return (add >> 10) & 0xFFF

ap = argparse.ArgumentParser()
ap.add_argument('-p', '--payload')
ap.add_argument('-o', '--output', default=os.path.join(HERE, 'xnu-redwood.img'))
ap.add_argument('--mode', default='RELEASE')
args = ap.parse_args()

fd_path = os.environ.get('UEFI_FD') or os.path.join(MU, f'Build/{TARGET}Pkg/{args.mode}_CLANGPDB/FV/SILICIUM_UEFI.fd')

# The payload has to be read before the shim is assembled: the shim copies it to
# XNU_PAYLOAD_BASE and therefore needs its size as a constant. It rides directly after
# the FD in the image, so the shim finds it at (shim load address + SHIM_SLOT + FD_SIZE).
payload = b''
if args.payload:
    payload = open(args.payload, 'rb').read()
    assert payload[:4] == b'XNUP' and len(payload) <= PAYLOAD_MAX - 0x1000, 'bad or oversized payload'

# The shim's job is to place the UEFI volume at FD_BASE and the payload at PAYLOAD_BASE,
# then jump to the firmware. It must be assembled with THIS board's addresses, which is
# what makes a prebuilt shim from another device unusable and a purpose-built one
# necessary. A full Mu-Silicium tree is one place to get it;
# devices/<name>/prebuilt/BootShim is the other, so an image can be rebuilt from the
# committed firmware alone. Without that fallback the kernel workflow's pack step could
# not run at all: it has no Mu-Silicium tree.
SHIM_FALLBACK = os.path.join(HERE, 'devices', TARGET, 'prebuilt', 'BootShim')
shim_dir = os.path.join(MU, 'BootShim')
if not os.path.isdir(shim_dir) and os.path.isfile(os.path.join(SHIM_FALLBACK, 'BootShim.S')):
    shim_dir = SHIM_FALLBACK
# Owned by this build: the shim was assembled here, so its slot is the one the packer pads to.
# A shim taken from another device's prebuilt is not checked against SHIM_SLOT.
shim_built_here = True
if os.path.isfile(fd_path) and os.path.isdir(shim_dir):
    for f in ('BootShim.elf', 'BootShim.bin'):
        try: os.unlink(os.path.join(shim_dir, f))
        except FileNotFoundError: pass
    # The shim's own Makefile only knows FD_BASE/FD_SIZE (upstream's shim copies the FD
    # alone). SHIM_SIZE, PAYLOAD_BASE and PAYLOAD_SIZE are passed through CFLAGS as defines;
    # the shim source materialises them as immediates, so they must be build-time constants.
    shim_cflags = ('--target=aarch64-linux-gnu '
                   f'-DSHIM_SIZE={SHIM_SLOT:#x} '
                   f'-DPAYLOAD_BASE={PAYLOAD_BASE:#x} -DXNU_PAYLOAD_BASE={PAYLOAD_BASE:#x} '
                   f'-DPAYLOAD_SIZE={len(payload):#x}')
    subprocess.check_call(['make', 'REQUIRES_KERNEL_HEADER=1', f'FD_BASE={FD_BASE:#x}', f'FD_SIZE={FD_SIZE:#x}',
                           f'CFLAGS={shim_cflags}'],
                          cwd=shim_dir, stdout=subprocess.DEVNULL)
    # The DTB and mkbootimg come from the Mu-Silicium tree when there is one; otherwise take
    # the DTB the caller exported (device.conf names it, and prebuilt supplies it).
    dtb_path = os.path.join(MU, f'Resources/DTBs/{TARGET}.dtb')
    if not os.path.isfile(dtb_path):
        dtb_path = os.environ.get('DTB') or dtb_path
    parts = {'shim': os.path.join(shim_dir, 'BootShim.bin'), 'fd': fd_path,
             'dtb': dtb_path, 'ramdisk': os.path.join(MU, 'Resources/ramdisk'),
             'mkbootimg': os.path.join(MU, 'Resources/Scripts/mkbootimg.py')}
    if not os.path.isfile(parts['mkbootimg']):
        parts['mkbootimg'] = os.path.join(PREBUILT, 'mkbootimg.py')
    if not os.path.isfile(parts['ramdisk']):
        parts['ramdisk'] = os.path.join(PREBUILT, 'ramdisk')
else:
    if TARGET != 'redwood':
        sys.exit(f'mkxnuimg: {TARGET} needs its own built UEFI and Mu-Silicium/BootShim; refusing redwood prebuilts')
    shim_built_here = False
    parts = {'shim': os.path.join(PREBUILT, 'BootShim.bin'), 'fd': os.path.join(PREBUILT, 'SILICIUM_UEFI.fd'),
             'dtb': os.path.join(PREBUILT, 'device-tree.dtb'), 'ramdisk': os.path.join(PREBUILT, 'ramdisk'),
             'mkbootimg': os.path.join(PREBUILT, 'mkbootimg.py')}
    if os.environ.get('UEFI_FD'): parts['fd'] = os.environ['UEFI_FD']
shim = open(parts['shim'], 'rb').read()
fd = open(parts['fd'], 'rb').read()
assert len(fd) == FD_SIZE, hex(len(fd))

if shim_built_here:
    # The packer's half of the contract with the shim: the FD starts SHIM_SLOT bytes into the
    # image, and the shim computes that same offset from its own load address. Check both
    # sides here so a disagreement fails this build instead of a phone with a black screen.
    if len(shim) > SHIM_SLOT:
        sys.exit(f'mkxnuimg: the boot shim is {len(shim)} bytes, over its {SHIM_SLOT:#x} byte slot')
    add_imm = shim_slot_from_code(shim)
    if add_imm != SHIM_SLOT:
        sys.exit(f'mkxnuimg: the boot shim adds {add_imm:#x} to its load address to find the FD, but '
                 f'the packer writes it at {SHIM_SLOT:#x}. Rebuild the shim with '
                 f'-DSHIM_SIZE={SHIM_SLOT:#x}, or change SHIM_SLOT here.')
    print(f'shim: {len(shim)} bytes, padded to {SHIM_SLOT:#x}; the FD follows at image offset '
          f'{SHIM_SLOT:#x}, which is what the shim encodes')
    shim = shim.ljust(SHIM_SLOT, b'\0')

blob = shim + fd
if payload:
    blob += payload
    blob += b'\0' * (-len(blob) % 16)
    print(f'payload: {len(payload)} bytes, bytesum={sum(payload) & 0xffffffff:08x}')
    # The shim copies it from (shim + SHIM_SLOT + FD_SIZE) to PAYLOAD_BASE. State the
    # addresses so a mismatch with the firmware's XNU_PAYLOAD_BASE is visible in the log
    # rather than only on the phone's screen.
    if TARGET != 'redwood':
        print(f'payload at image offset {len(shim) + FD_SIZE:#x}; the shim copies it to '
              f'PAYLOAD_BASE={PAYLOAD_BASE:#x}; the firmware volume goes to FD_BASE={FD_BASE:#x}')

if os.environ.get('DTB'): parts['dtb'] = os.environ['DTB']
dtb = open(parts['dtb'], 'rb').read()
kernel = args.output + '.kernel~'
open(kernel, 'wb').write(gzip.compress(blob, 6) + dtb)
try:
    # board "HACKiOS": how the loader of an installed phone tells which A/B slot's boot partition is its own
    # (xnuloader/flashboot.c, ab_switch_back) -- an Android boot image never has that name
    subprocess.check_call([sys.executable, parts['mkbootimg'], '--kernel', kernel, '--ramdisk', parts['ramdisk'],
                           '--header_version', '1', '--board', 'HACKiOS', '-o', args.output])
finally:
    os.unlink(kernel)
print(f'{args.output}: {os.path.getsize(args.output)} bytes (shim {len(shim)}, uncompressed {len(blob)})')
