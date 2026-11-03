"""Check the loader for code that only works under QEMU, reached without a QEMU gate.

The loader runs in two places: under QEMU on a developer's machine, and on the phone. Code
written for one of them is not merely useless on the other, it faults -- the phone has no
fw_cfg, no ramfb, none of the virt machine's devices. The first polaris boot died exactly
this way: a framebuffer appeared, so the loader called ramfb_phone_screen(), which writes
QEMU's fw_cfg selector at 0x09020008, which on the phone is unmapped.

So: every access to a QEMU-only address must be inside a function whose caller proves it is
QEMU. This script finds the accesses and reports where they are, and checks that the gates it
knows about are present. It reads the source, not the binary, so it runs anywhere.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'xnuloader', 'loader.c')

# Addresses that exist on QEMU's virt machine and nowhere on these phones. Each entry is
# (pattern, what it is, why it is only QEMU's). Assigning the QEMU platform as a *default* is
# not an access; that is checked separately below, because the risk there is different.
QEMU_ONLY = [
    (r'FW_CFG_BASE', 'fw_cfg', 'QEMU’s firmware-config interface; the phone has no such device, '
                               'and 0x09020000 is unmapped there'),
    (r'0xE1000000', 'the virt machine’s framebuffer', 'no phone puts RAM there'),
]


def read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8', errors='replace') as f:
        return f.read()


def strip_comments(text):
    """Blank out comments and string literals, keeping line numbers."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        if text.startswith('/*', i):
            j = text.find('*/', i + 2)
            j = n if j < 0 else j + 2
            out.append(''.join('\n' if c == '\n' else ' ' for c in text[i:j]))
            i = j
        elif text.startswith('//', i):
            j = text.find('\n', i)
            j = n if j < 0 else j
            out.append(' ' * (j - i))
            i = j
        elif text[i] == '"' or text[i] == "'":
            q = text[i]
            j = i + 1
            while j < n and text[j] != q:
                j += 2 if text[j] == '\\' else 1
            j = min(j + 1, n)
            out.append(''.join('\n' if c == '\n' else ' ' for c in text[i:j]))
            i = j
        else:
            out.append(text[i])
            i += 1
    return ''.join(out)


def functions(text):
    """Yield (name, start_line, end_line, body) for top-level function definitions.

    This codebase writes the return type on its own line and the name at column 0, so a
    function starts at a line matching `name(` at column 0 whose next non-empty line opens a
    brace. Brace counting then finds the end. Kept deliberately simple: it only has to
    recognise this file, and a wrong answer is loud ("could not find any functions").
    """
    lines = text.split('\n')
    i = 0
    while i < len(lines):
        m = re.match(r'^([A-Za-z_]\w*)\s*\([^;]*\)\s*$', lines[i])
        if not m:
            i += 1
            continue
        name = m.group(1)
        # the body starts at the next line that is just `{`
        j = i + 1
        while j < len(lines) and lines[j].strip() == '':
            j += 1
        if j >= len(lines) or lines[j].strip() != '{':
            i += 1
            continue
        depth = 0
        k = j
        while k < len(lines):
            depth += lines[k].count('{') - lines[k].count('}')
            if depth == 0:
                break
            k += 1
        yield name, i + 1, k + 1, '\n'.join(lines[i:k + 1])
        i = k + 1


def main():
    src = read('xnuloader/loader.c')
    code = strip_comments(src)
    problems = []
    findings = []

    funcs = list(functions(code))
    if not funcs:
        problems.append('could not find any functions in loader.c; the parser needs updating')

    for pat, what, why in QEMU_ONLY:
        homes = [(n, a, b) for n, a, b, body in funcs if re.search(pat, body)]
        findings.append(f'{what}: referenced in {len(homes)} function(s) '
                        f'({", ".join(n for n, _, _ in homes) or "none"})')
        for name, a, b in homes:
            if name in ('fw_cfg_present',):
                continue        # the probe is supposed to touch it: that is how it asks
            if name == 'efi_main':
                problems.append(f'{what} is referenced directly in efi_main (line {a}): QEMU-only '
                                f'accesses must be behind a gate, not inline -- {why}')
            elif name == 'ramfb_phone_screen':
                continue        # only reachable from efi_main behind the gate, checked below

    # The gate itself: ramfb must be called only inside `if (fw_cfg_present())`.
    m = re.search(r'if\s*\(\s*fw_cfg_present\s*\(\s*\)\s*\)\s*\{?\s*\n?(.{0,400})', code, re.S)
    if not m:
        problems.append('efi_main does not gate ramfb_phone_screen() on fw_cfg_present()')
    elif 'ramfb_phone_screen' not in m.group(1):
        problems.append('the fw_cfg_present() branch does not call ramfb_phone_screen(); the '
                        'gate and the code it guards have come apart')
    else:
        findings.append('ramfb_phone_screen() is called only inside `if (fw_cfg_present())`')

    # A probe that faults is pointless unless a handler turns the fault into an answer.
    if 'fw_cfg_probe_active' not in code:
        problems.append('fw_cfg_present() cannot survive the fault it takes on hardware: '
                        'fw_cfg_probe_active is gone')
    elif not re.search(r'fw_cfg_probe_active[^;]*\)\s*\{', code):
        problems.append('fw_cfg_probe_active is set but the exception handler never tests it, so '
                        'a fault during the probe resets the machine instead of answering "no"')
    else:
        findings.append('the exception handler answers the probe fault instead of resetting')

    # The handler skips one faulting instruction, so the probe has to stop at the first fault.
    # A probe that makes several QEMU-only accesses and checks the flag only at the end walks
    # into the next fault as soon as the first one is skipped -- which is exactly what happened
    # on the second polaris boot, at the same function as the first crash.
    m = re.search(r'\nstatic int\s*\nfw_cfg_present\(void\)\s*\n\{(.*?)\n\}', code, re.S)
    if not m:
        problems.append('could not find fw_cfg_present(); the check needs updating')
    else:
        body = m.group(1)
        accesses = len(re.findall(r'FW_CFG_BASE', body))
        checks = len(re.findall(r'fw_cfg_probe_faulted', body))
        findings.append(f'fw_cfg_present() makes {accesses} QEMU-only access(es) and checks the '
                        f'fault flag {checks} time(s)')
        if accesses > 1 and checks < accesses:
            problems.append(f'fw_cfg_present() makes {accesses} accesses but checks the fault flag '
                            f'only {checks} time(s): the first fault is skipped by the handler and '
                            f'the probe then walks straight into the next one, which resets the '
                            f'machine. Check after every access.')
    # And the handler must not clear the flag itself: the probe owns its lifetime.
    m = re.search(r'if\s*\(\s*fw_cfg_probe_active\b.*?\n(\t.*?\n)+?\t\}', code, re.S)
    if m and re.search(r'fw_cfg_probe_active\s*=\s*0', m.group(0)):
        problems.append('the exception handler clears fw_cfg_probe_active itself, so only the '
                        'first fault of the probe is handled and later ones reset the machine')

    # The hook has to be installed before anything that can fault.
    main_start = None
    for name, a, b, body in funcs:
        if name == 'efi_main':
            main_start = (a, b, body)
    if main_start:
        a, b, body = main_start
        hook = body.find('install_exception_hook()')
        gop = body.find('locate_protocol')
        if hook < 0:
            problems.append('efi_main never installs the exception hook')
        elif gop >= 0 and hook > gop:
            problems.append('efi_main installs the exception hook after it starts touching the '
                            'display, so a fault there still goes unreported')
        else:
            findings.append('the exception hook is installed before the display setup')

    # The platform has to be decided before it is used. PLAT starts as the QEMU platform, so
    # unless the payload names a real one, the loader would drive virt's GIC at the phone.
    # This one looks at the raw text, because it is about a string literal.
    if 'bootarg("platform=")' not in src:
        problems.append('the loader never reads platform= from the payload, so on a phone it '
                        'would drive the virt machine’s GIC instead of polaris’')
    elif 'platform_polaris' not in src:
        problems.append('platform= is read but platform_polaris is not among the choices')
    else:
        findings.append('the platform is named by payload.cmdline platform=polaris')

    print('QEMU-only surface:')
    for f in findings:
        print('  ' + f)
    print()
    if problems:
        print('PROBLEMS:')
        for p in problems:
            print('  - ' + p)
        return 1
    print('RESULT: QEMU-only code is reachable only when QEMU has been detected')
    return 0


if __name__ == '__main__':
    sys.exit(main())
