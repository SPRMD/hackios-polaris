"""Check that the extracted iOS root filesystem can actually be a root filesystem.

Extraction succeeding is not the same as the tree being usable. Two things go wrong
quietly on Windows and both produce a tree that looks populated:

  * 7-Zip writes 'usr\\libexec' instead of 'usr/libexec'. select-system.py then matches
    nothing and emits "0 files, 0 MB" -- which is why the listing is normalised first.
  * files stored with LZBITMAP come out as 0 bytes with a ':com.apple.decmpfs' sidecar.
    fetch.sh finishes those with unzbm; without it they are silently empty.

So this checks, against the manifest rather than against expectations: how many of the
chosen files exist, how many are empty, whether the Mach-O programs are real, and
whether the pieces iOS needs to reach a home screen are present.

usage: check_system_image.py ROOT MANIFEST [--cryptex DIR]
"""
import collections
import os
import stat
import struct
import sys

# The files that decide whether a boot reaches a picture. Each is either a Mach-O on
# disk or an entry in the dyld shared cache; a missing one on both is a real gap.
MACHO_MAGICS = {b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe',
                b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'}
ON_DISK = [
    ('System/Library/CoreServices/SpringBoard.app/SpringBoard', 'the home screen'),
    ('sbin/launchd', 'process 1'),
    ('usr/lib/dyld', 'the dynamic linker'),
    # backboardd lives in usr/libexec on iOS 16, not in CoreServices
    ('usr/libexec/backboardd', 'the window server'),
]
# These are expected to be absent from disk because the dyld shared cache carries them.
IN_CACHE = ['UIKit', 'Foundation', 'libobjc.A.dylib', 'BackBoardServices',
            'CoreFoundation', 'libSystem.B.dylib']


def read_manifest(path):
    """mode(octal, with type bits) uid gid path [link target], tab separated.

    The first field is a full st_mode in octal, not a type character: '40775' is a
    directory, '100775' a regular file, '120777' a symlink. Reading it as a character
    yields '4'/'1' and every count comes out wrong, so decode it with stat.
    """
    entries = []
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            parts = line.rstrip('\n').split('\t')
            if len(parts) >= 4:
                try:
                    mode = int(parts[0], 8)
                except ValueError:
                    continue
                entries.append((mode, parts[3]))
    return entries


def kind(mode):
    if stat.S_ISDIR(mode):
        return 'd'
    if stat.S_ISLNK(mode):
        return 'l'
    if stat.S_ISREG(mode):
        return '-'
    return '?'


def main(root, manifest, cryptex=None):
    entries = read_manifest(manifest)
    kinds = collections.Counter(kind(m) for m, _ in entries)
    print('  manifest: %d entries  (%s)'
          % (len(entries), ', '.join('%s=%d' % (k, v) for k, v in sorted(kinds.items()))))

    regular = [p for m, p in entries if kind(m) == '-']
    missing, empty, ok = [], [], 0
    for p in regular:
        full = os.path.join(root, p)
        if not os.path.isfile(full):
            missing.append(p)
        elif os.path.getsize(full) == 0:
            empty.append(p)
        else:
            ok += 1

    print('  regular files in the manifest: %d' % len(regular))
    print('    present and non-empty : %d' % ok)
    print('    missing               : %d' % len(missing))
    print('    present but empty     : %d' % len(empty))

    # Applications/ icons and localisations are media the selector deliberately leaves out,
    # and three iOS filenames contain '*' or '?' which Windows cannot create. Neither is a
    # failure of the extraction, so separate them from a genuinely absent system file --
    # otherwise a complete tree reports thousands of "missing" files and the number means
    # nothing.
    app_missing = [p for p in missing if p.startswith('Applications/')]
    illegal = [p for p in missing if any(c in '<>:"|?*' for c in p)]
    real = [p for p in missing if p not in app_missing and p not in illegal]
    if app_missing:
        print('      of which Applications/ resources (not needed to boot): %d'
              % len(app_missing))
    if illegal:
        print('      of which names Windows cannot create (* or ?):          %d'
              % len(illegal))
    if real:
        print('      GENUINELY MISSING system files:                        %d' % len(real))
        for p in real[:8]:
            print('        %s' % p)
    missing = real
    if empty:
        # LZBITMAP leaves these as 0 bytes plus a ':com.apple.decmpfs' sidecar; fetch.sh
        # runs unzbm over them. Group by extension so the report says what kind of loss it is.
        by_ext = collections.Counter(os.path.splitext(p)[1] or '(none)' for p in empty)
        print('    empty by extension    : %s'
              % ', '.join('%s=%d' % (e, n) for e, n in by_ext.most_common(6)))
    if missing:
        print('    first missing         : %s' % missing[0])
    print()
    print('  on-disk Mach-O programs:')
    bad = 0
    for rel, what in ON_DISK:
        full = os.path.join(root, rel)
        if not os.path.isfile(full):
            print('    %-52s MISSING (%s)' % (rel, what))
            bad += 1
            continue
        with open(full, 'rb') as f:
            head = f.read(4)
        size = os.path.getsize(full)
        if head in MACHO_MAGICS:
            print('    %-52s %9d bytes  Mach-O  (%s)' % (rel, size, what))
        else:
            print('    %-52s %9d bytes  NOT Mach-O %r  (%s)' % (rel, size, head, what))
            bad += 1

    print()
    print('  frameworks that must come from the dyld shared cache:')
    for name in IN_CACHE:
        found = False
        for dirpath, _, files in os.walk(root):
            if name in files:
                found = True
                break
        # Absent from disk is the normal, correct arrangement.
        print('    %-22s %s' % (name, 'also on disk' if found else 'in the cache only'))

    if cryptex:
        print()
        cache_dir = os.path.join(cryptex, 'System/Library/Caches/com.apple.dyld')
        if os.path.isdir(cache_dir):
            files = [f for f in os.listdir(cache_dir)]
            nonempty = [f for f in files
                        if os.path.getsize(os.path.join(cache_dir, f)) > 0]
            total = sum(os.path.getsize(os.path.join(cache_dir, f)) for f in nonempty)
            print('  dyld shared cache: %d files, %d non-empty, %.2f GB'
                  % (len(files), len(nonempty), total / 1e9))
            main_cache = os.path.join(cache_dir, 'dyld_shared_cache_arm64')
            if os.path.isfile(main_cache):
                with open(main_cache, 'rb') as f:
                    head = f.read(4)
                print('    dyld_shared_cache_arm64: %d bytes, magic %s'
                      % (os.path.getsize(main_cache), head.hex()))
            if len(nonempty) < 2 or total < 1e9:
                print('    PROBLEM: the cache looks truncated')
                bad += 1
        else:
            print('  dyld cache directory not found under %s' % cryptex)
            bad += 1

    print()
    if missing or bad:
        print('  RESULT: NOT READY -- %d missing, %d bad program(s)' % (len(missing), bad))
        return 1
    if empty:
        print('  RESULT: usable, but %d file(s) are empty (LZBITMAP; run unzbm).' % len(empty))
        return 0
    print('  RESULT: the tree is complete and its programs are real Mach-O.')
    return 0


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print('usage: check_system_image.py ROOT MANIFEST [--cryptex DIR]', file=sys.stderr)
        sys.exit(2)
    cryptex = None
    if '--cryptex' in sys.argv:
        cryptex = sys.argv[sys.argv.index('--cryptex') + 1]
    sys.exit(main(sys.argv[1], sys.argv[2], cryptex))
