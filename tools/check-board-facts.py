"""Compare this port's board constants against two authoritative polaris sources.

The port's addresses were derived and, in several places, inherited from raphael and redwood
before the board was verified. Two independent, long-maintained device trees describe the same
hardware, and neither is a fork of this work:

  LineageOS/android_device_xiaomi_polaris        (14 branches, through lineage-23.2)
  LineageOS/android_device_xiaomi_sdm845-common  (the shared SDM845 board config)
  TeamWin/android_device_xiaomi_polaris          (TWRP)

Where they agree with each other and disagree with this repository, this repository is wrong
until a phone proves otherwise. The loader's ufs_references table is the clear case: it is the
size of each Android partition's *image*, which cannot be right for two boards at once, and
against polaris' own package every entry is wrong.

This reads checked-in copies of those files, so it runs offline. Update the copies with
tools/fetch-reference-trees.sh when the reference trees move.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF = os.path.join(ROOT, 'devices', 'polaris', 'reference')


def read(rel):
    p = os.path.join(ROOT, rel)
    if not os.path.exists(p):
        return None
    with open(p, encoding='utf-8', errors='replace') as f:
        return f.read()


def office(fn):
    return read(os.path.join('devices', 'polaris', 'reference', fn))


def main():
    problems = []
    notes = []

    conf = read('devices/polaris/device.conf') or ''
    ref_fstab = office('fstab.qcom')
    ref_board = office('BoardConfigCommon.mk')
    ref_dev = office('polaris-BoardConfig.mk')

    if ref_fstab is None and ref_board is None:
        print('reference/: not populated. Run tools/fetch-reference-trees.sh first.')
        return 2

    # ---- 1. the kernel's own load addresses ------------------------------------------
    # The kernel is given these in the Android boot image. LineageOS and TWRP both write
    # base 0 with offset 0x8000; the official MIUI image in the workspace agrees
    # (kernel_addr 0x8000, tags_addr 0x100, page_size 4096).
    ours = {}
    for name in ('FD_BASE', 'FD_SIZE', 'PAYLOAD_BASE', 'FB_BASE'):
        m = re.search(r'^%s=(\S+)' % name, conf, re.M)
        if m:
            ours[name] = int(m.group(1), 0)
    notes.append('device.conf: ' + ', '.join(f'{k}={v:#x}' for k, v in ours.items()))

    # ---- 2. the page size ------------------------------------------------------------
    # The official polaris boot.img says 4096 and so does every SDM845 board config.
    # mkbootimg's default is 2048, so an image built without --pagesize silently claims
    # 2048. This does not by itself stop ABL from loading it, but it is wrong, and it makes
    # the image differ from every image this phone has ever booted.
    #
    # Check what the packer passes, not what mkbootimg defaults to: the default lives in a
    # Mu-Silicium submodule and in another device's prebuilt, neither of which is present on
    # every build host, so probing the default only ever tested that the file was missing.
    page_sizes = {'lineage sdm845-common': None, 'twrp polaris': None}
    m = re.search(r'^BOARD_KERNEL_PAGESIZE\s*:=\s*(\d+)', ref_board or '', re.M)
    if m:
        page_sizes['lineage sdm845-common'] = int(m.group(1))
    m = re.search(r'^BOARD_KERNEL_PAGESIZE\s*:=\s*(\d+)', ref_dev or '', re.M)
    if m:
        page_sizes['twrp polaris'] = int(m.group(1))
    ref_pages = {v for v in page_sizes.values() if v}
    if len(ref_pages) > 1:
        problems.append('the reference trees disagree on the page size: '
                        + ', '.join(f'{k} says {v}' for k, v in page_sizes.items() if v))
    elif ref_pages:
        want = ref_pages.pop()
        notes.append(f'reference trees say the page size is {want}')
        im = read('mkxnuimg.py') or ''
        m = re.search(r"""'--pagesize',\s*(?:str\(\s*)?['"]?(\d+)""", im)
        got = int(m.group(1)) if m else None
        if got is None:
            problems.append(
                'mkxnuimg.py does not pass --pagesize, so mkbootimg uses its own default '
                f'(2048) and the image claims a page size no polaris image has ever used. '
                f'The reference trees say {want}. Pass --pagesize {want}.')
        elif got != want:
            problems.append(f'mkxnuimg.py builds with page size {got}, but the reference '
                            f'trees say {want}')
        else:
            notes.append(f'mkxnuimg.py passes --pagesize {got}')
    else:
        notes.append('no page size in the reference trees -- check not performed')

    # ---- 3. the loader's partition-size table ----------------------------------------
    # Its entries are meant to be the length of each partition's image, which is what the
    # hash covers. Comparing them with polaris' own package shows every one of them belongs
    # to another board.
    ld = read('xnuloader/loader.c') or ''
    m = re.search(r'ufs_references\[\]\s*=\s*\{(.*?)\};', ld, re.S)
    if m:
        table = dict((n, int(s)) for n, s in re.findall(r'\{\s*"(\w+)",\s*(\d+)\s*\}', m.group(1)))
        pol = office('polaris-partition-sizes.txt')
        if pol:
            sizes = dict((n, int(s)) for n, s in
                         (l.split() for l in pol.split('\n') if l.strip() and not l.startswith('#')))
            wrong = [n for n in table if n in sizes and table[n] != sizes[n]]
            notes.append(f'loader ufs_references: {len(table)} entries, {len(wrong)} disagree '
                         f'with polaris\' own package')
            if wrong:
                problems.append(
                    'xnuloader/loader.c ufs_references disagrees with polaris\' own images for '
                    + ', '.join(f'{n} ({table[n]} vs {sizes[n]})' for n in wrong)
                    + '. Those sizes are another board\'s; against polaris every hash it prints '
                      'is of the wrong length, so the diagnostic is meaningless and the numbers '
                      'invite a false conclusion.')
    # ---- 4. the USB and UFS controller addresses -------------------------------------
    for pat, what in ((r'androidboot\.usbcontroller=([\w.]+)', 'USB controller'),
                      (r'androidboot\.boot_devices=(\S+)', 'UFS host controller')):
        m = re.search(pat, (ref_board or '') + (ref_fstab or ''))
        if m:
            notes.append(f'reference cmdline names the {what} as {m.group(1)}')

    print('board constants:')
    for n in notes:
        print('  ' + n)
    print()
    if problems:
        print('PROBLEMS:')
        for p in problems:
            print('  - ' + p)
        return 1
    print('RESULT: the port agrees with the reference trees on every constant checked')
    return 0


if __name__ == '__main__':
    sys.exit(main())
