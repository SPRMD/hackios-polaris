"""Rewrite the Path values of a 7-Zip technical listing to use forward slashes.

sysimage/select-system.py matches paths with patterns like 'usr/libexec/' and
'System/Library/Fonts/', which is correct for the listing 7-Zip writes on Linux.
On Windows the same 7-Zip writes 'usr\\libexec' and the patterns match nothing, so
the selector reports "0 files, 0 MB" while still emitting 138737 directory and
symlink entries -- a silent near-empty system image rather than an error.

Normalising the listing keeps the upstream script untouched and makes the run
identical on both platforms. Only the value after 'Path = ' is changed; every other
field, including sizes and attributes, is passed through byte for byte.

usage: normalize_slt.py IN.slt OUT.slt
"""
import sys


def main(src, dst):
    changed = 0
    total = 0
    with open(src, 'rb') as f, open(dst, 'wb') as out:
        for line in f:
            if line.startswith(b'Path = '):
                total += 1
                value = line[7:].rstrip(b'\r\n')
                if b'\\' in value:
                    changed += 1
                    line = b'Path = ' + value.replace(b'\\', b'/') + b'\n'
            out.write(line)
    print('  Path entries: %d, rewritten with forward slashes: %d' % (total, changed))
    return 0


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print('usage: normalize_slt.py IN.slt OUT.slt', file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
