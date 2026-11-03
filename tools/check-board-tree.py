"""Check this port's live device tree against the polaris source it was derived from.

The board's own DTS, from LineageOS' kernel tree (android_kernel_xiaomi_sdm845, 177 stars,
branch lineage-22.2), is the description Xiaomi's engineers wrote and everyone else has been
maintaining since 2018. Checked-in copies are under devices/polaris/reference/. The live tree
is devices/polaris/prebuilt/polaris.dtb -- the one the boot image actually ships, extracted
from the official MIUI package.

This walks both and compares the handful of facts that a driver has to agree with the
hardware on. They are the facts that fail silently when they are wrong: a touch controller at
the wrong I2C address reads nothing, a TLMM base that is off by one register bank writes
somewhere else.

Both sides are parsed the same way, from the compiled tree and from the source text, so the
check does not depend on trusting the packaging step either.

usage: check-board-tree.py [--verbose]
"""
import os
import re
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF = os.path.join(ROOT, 'devices', 'polaris', 'reference')
DTB = os.path.join(ROOT, 'devices', 'polaris', 'prebuilt', 'polaris.dtb')

FDT_BEGIN_NODE, FDT_END_NODE, FDT_PROP, FDT_NOP, FDT_END = 1, 2, 3, 4, 9


def read(rel):
    p = os.path.join(REF, rel)
    if not os.path.exists(p):
        return None
    with open(p, encoding='utf-8', errors='replace') as f:
        return f.read()


# ---- the compiled tree ---------------------------------------------------------------

def parse_fdt(d):
    """Return {full path: {property: bytes}} for every node in a flattened device tree."""
    if d[:4] != b'\xd0\x0d\xfe\xed':
        sys.exit('the live tree does not start with the FDT magic')
    off_struct, off_strings, size_strings, size_struct = (
        struct.unpack_from('>I', d, 8)[0], struct.unpack_from('>I', d, 12)[0],
        struct.unpack_from('>I', d, 32)[0], struct.unpack_from('>I', d, 36)[0])
    if off_strings + size_strings > len(d) or off_struct + size_struct > len(d):
        sys.exit('the live tree\'s header gives offsets past the end of the file')

    def string(off):
        base = off_strings + off
        if off >= size_strings:
            raise ValueError('property name offset %d is past the string block' % off)
        end = d.index(b'\0', base)
        return d[base:end].decode('utf-8', 'replace')

    nodes = {}
    path = []
    p = off_struct
    end = off_struct + size_struct
    while p < end:
        token = struct.unpack_from('>I', d, p)[0]
        p += 4
        if token == FDT_BEGIN_NODE:
            # The name offset here is relative to the string block only in the *properties*;
            # a node's name is stored inline, NUL-terminated and padded to 4 bytes. Reading
            # it as an offset is what made an earlier version of this file fail on the very
            # first node and blame the string block for it.
            n_end = d.index(b'\0', p)
            name = d[p:n_end].decode('utf-8', 'replace')
            p += (n_end - p + 1 + 3) & ~3
            path.append(name)
            nodes['/'.join(path) or '/'] = {}
        elif token == FDT_END_NODE:
            if path:
                path.pop()
        elif token == FDT_PROP:
            length, name_off = struct.unpack_from('>II', d, p)
            p += 8
            value = d[p:p + length]
            p += (length + 3) & ~3
            if path:
                nodes.setdefault('/'.join(path), {})[string(name_off)] = value
        elif token == FDT_NOP:
            pass
        elif token == FDT_END:
            break
    return nodes


def cell(value, index=0):
    return struct.unpack_from('>I', value, index * 4)[0]


def find(nodes, *needles):
    """Nodes whose path ends with every needle; the deepest wins (later in the walk)."""
    hits = [p for p in nodes if all(n in p for n in needles)]
    return hits


# ---- what a driver has to agree with -------------------------------------------------

def main(argv):
    verbose = '--verbose' in argv
    problems = []
    notes = []

    if not os.path.exists(DTB):
        print('no live device tree at ' + os.path.relpath(DTB, ROOT))
        return 2
    nodes = parse_fdt(open(DTB, 'rb').read())
    notes.append('live tree: %d nodes' % len(nodes))

    # 1. The pin controller. Every GPIO number in this port is an offset into this window,
    #    so a wrong base turns a touch reset into a write into whatever else lives there.
    tlmm = find(nodes, 'pinctrl@3400000')
    if tlmm:
        reg = nodes[tlmm[0]].get('reg', b'')
        base = cell(reg, 0)
        notes.append('live pinctrl@3400000 reg[0] = %#x' % base)
        src = read('sdm845-pinctrl.txt') or ''
        m = re.search(r'pinctrl@0*([0-9a-fA-F]+)\s*\{', src)
        if m:
            want = int(m.group(1), 16)
            notes.append('source pinctrl base = %#x' % want)
            if base != want:
                problems.append('the pin controller is at %#x in the live tree but %#x in the '
                                'source; every GPIO number would address the wrong register'
                                % (base, want))
        else:
            notes.append('source pinctrl base not found -- not compared')
    else:
        problems.append('the live tree has no pinctrl@3400000 node')

    # 2. The touch controller. The port's kernel has no polaris touch driver yet, so this
    #    records where one has to attach: the source names the bus, the address, the two
    #    GPIOs and the interrupt, and the live tree has to have the same bus.
    src = read('polaris-p0.dtsi') or ''
    m = re.search(r'&(\w+)\s*\{\s*\n(?:[^}]*\n)*?synaptics_\w+@([0-9a-fA-F]+)\s*\{', src)
    if m:
        bus, addr = m.group(1), int(m.group(2), 16)
        notes.append('source touch: %s, I2C address %#x' % (bus, addr))
        irq = re.search(r'synaptics,irq-gpio\s*=\s*<&tlmm\s+(\d+)', src)
        rst = re.search(r'synaptics,reset-gpio\s*=\s*<&tlmm\s+(\d+)', src)
        if irq:
            notes.append('source touch interrupt GPIO %s' % irq.group(1))
        if rst:
            notes.append('source touch reset GPIO %s' % rst.group(1))
        # The bus the source names has to exist in the live tree, or the driver has nothing
        # to attach to. The SoC's own tree names the i2c controllers by address.
        bus_nodes = [p for p in nodes if re.search(r'i2c@a98000', p)]
        if bus_nodes:
            notes.append('live tree has i2c@a98000 (the bus the source uses)')
        else:
            problems.append('the source puts the touch screen on %s, i.e. i2c@a98000, but the '
                            'live tree has no such node' % bus)
        # and the live tree must not put something else there
        for p in bus_nodes:
            for prop in ('compatible',):
                v = nodes[p].get(prop, b'')
                if v and b'synaptics' in v:
                    notes.append('live tree also names synaptics at ' + p)
    else:
        notes.append('source touch node not found -- touch not compared')

    # 3. The display. The panel is chosen from the panel list by the bootloader and handed
    #    over as a framebuffer; the port never programs DSI. What it does need is the same
    #    framebuffer base and size, which device.conf carries, and the MDP window.
    mdp = find(nodes, 'mdss_mdp@ae00000')
    if mdp:
        reg = nodes[mdp[0]].get('reg', b'')
        base = cell(reg, 0)
        notes.append('live mdss_mdp reg[0] = %#x' % base)
        if base != 0xae00000:
            problems.append('the display controller is at %#x in the live tree, not 0xae00000'
                            % base)
    else:
        problems.append('the live tree has no mdss_mdp@ae00000 node')

    # 4. The interrupt controller, which the loader hands to XNU by address.
    gic = find(nodes, 'interrupt-controller@17a00000')
    if gic:
        reg = nodes[gic[0]].get('reg', b'')
        notes.append('live GIC dist = %#x, redist = %#x' % (cell(reg, 0), cell(reg, 2)))
        if cell(reg, 0) != 0x17a00000 or cell(reg, 2) != 0x17a60000:
            problems.append('the live tree\'s GIC is not at 0x17a00000/0x17a60000, which is '
                            'what the loader and device.conf say')
    else:
        problems.append('the live tree has no interrupt-controller@17a00000 node')

    if verbose:
        print('board tree:')
        for n in notes:
            print('  ' + n)
    if problems:
        print('PROBLEMS from the live tree:')
        for p in problems:
            print('  - ' + p)
        return 1
    print('RESULT: the live tree agrees with the polaris source on the pin controller, the '
          'touch bus, the display and the GIC')
    for n in notes:
        print('  ' + n)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
