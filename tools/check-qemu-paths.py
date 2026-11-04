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


def strip_comments(text, keep_strings=False):
    """Blank out comments, and string literals unless keep_strings, keeping line numbers.

    The other checks want literals gone: a 64-bit address spelled inside a string is not an
    access to it, and counting those produced false positives. But a check that looks for a
    particular literal has to keep them, or it silently finds nothing.
    """
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
            out.append(text[i:j] if keep_strings
                       else ''.join('\n' if c == '\n' else ' ' for c in text[i:j]))
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
        problems.append('efi_main does not gate ramfb on fw_cfg_present()')
    elif 'ramfb_phone_screen' not in m.group(1):
        problems.append('the fw_cfg_present() branch does not call ramfb_phone_screen(); the '
                        'gate and the code it guards have come apart')
    else:
        findings.append('ramfb_phone_screen() is reached only through fw_cfg_present()')

    # ...and in turn the probe must be inside `if (PLAT == &platform_qemu_virt)`. Reading the
    # board's name out of the cmdline cannot fault; asking a device whether it exists can, and
    # the phone answers by faulting. The board name is the primary gate and the probe is only
    # the secondary one.
    m2 = re.search(r'if\s*\(\s*PLAT\s*==\s*&platform_qemu_virt\s*\)\s*\{(.{0,600})', code, re.S)
    if not m2:
        problems.append('ramfb is not gated on the platform being QEMU’s: the platform is known '
                        'from payload.cmdline, so use it instead of probing the device')
    elif 'ramfb_phone_screen' not in m2.group(1):
        problems.append('the PLAT == &platform_qemu_virt branch does not lead to '
                        'ramfb_phone_screen()')
    else:
        findings.append('ramfb_phone_screen() is reached only when the platform is QEMU’s')

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

        # And the hook can only save a fault to storage if the loader has already decided to
        # keep its log there. flush_log() does nothing when persist_logs is 0, so a fault
        # between the hook and that assignment is written nowhere -- which is what happened on
        # polaris: the crash was photographed on the screen and the logdump partition stayed
        # empty. Both assignments have to be above the hook, and the platform above them,
        # because persist_logs is derived from it.
        #
        # Positions are taken from the body with comments removed but string literals KEPT.
        # strip_comments() blanks string literals too -- which is what the other checks want,
        # since an address written in a string is not an access -- but here the thing being
        # looked for is a literal ("platform="), so blanking it made this check find nothing
        # and report the order as wrong no matter how the code was written.
        a, b, body = main_start
        plain = strip_comments('\n'.join(src.split('\n')[a - 1:b]), keep_strings=True)
        log_at = plain.find('persist_logs =')
        plat_at = plain.find('bootarg(')
        hook_at = plain.find('install_exception_hook()')
        if log_at < 0:
            problems.append('efi_main never sets persist_logs, so flush_log() from the exception '
                            'handler writes nothing and a crash leaves no log behind')
        elif hook_at >= 0 and hook_at < log_at:
            problems.append('efi_main installs the exception hook before it decides whether to '
                            'keep the log on storage (persist_logs), so a fault in between is '
                            'flushed to nothing')
        elif plat_at < 0 or plat_at > log_at:
            problems.append('efi_main sets persist_logs before it reads platform= from the '
                            'payload, so persist_logs is derived from the wrong platform')
        else:
            findings.append('the platform and the log policy are decided before the exception '
                            'hook can need them')

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

    # ... but asking for platform= is not enough: bootarg() itself has to be able to answer.
    # It used to require a space or the end of the line after the prefix, so every key=value
    # argument -- platform=, ufs=, loader_return_after= -- came back NULL. The loader then
    # stayed on the QEMU platform, printed "platform qemu-virt (log console only)", kept its
    # log on the console instead of the logdump partition, and a phone that crashed there left
    # nothing but a photograph. Seen on the phone, not deduced: the screen said qemu-virt while
    # the payload said platform=polaris. This checks the matcher's actual behaviour.
    unexpected = []
    body = src[src.find('bootarg(const char *prefix)'):]
    body = strip_comments(body, keep_strings=True)
    if "prefix[want - 1] == '='" not in body:
        unexpected.append(
            'bootarg() does not treat a prefix ending in "=" as a whole key, so '
            'bootarg("platform=") cannot return the value -- this is the bug that made the '
            'phone report "platform qemu-virt (log console only)"')
    elif "c[i] == '='" not in body:
        unexpected.append('bootarg() no longer matches a bare word prefix before "="')
    # Only the arguments this file itself asks for. touchscreen= is not among them: the loader
    # passes it through to the kernel, which is what reads it.
    if unseen := [p for p in ('platform=', 'ufs=') if p not in src]:
        unexpected.append('the loader stopped asking for %s' % unseen[0])
    if unexpected:
        problems.extend(unexpected)
    else:
        findings.append('bootarg() reads key=value arguments, which is what platform= and the '
                        'rest are')

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
