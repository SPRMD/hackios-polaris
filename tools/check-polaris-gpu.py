#!/usr/bin/env python3
"""Polaris (SDM845 / Adreno 630) must be read-only, and must read the right windows.

Nothing here touches a phone or a device register: the test reads
xnu/files/bsd/dev/hakios.adreno.c and asserts the properties the port promises.

  * the windows the polaris branch sets are the phone's own device tree's `reg` values
  * no step reachable on polaris writes a GPU, GMU, GPUCC or SMMU register
  * every write step refuses, by name, and the ladder does not treat a refusal as a failure
  * the end-of-run cleanup writes nothing on polaris

The window values are evaluated numerically rather than pattern-matched, so a macro that
changes value is caught; and they are exercised by a compiled program whenever a C compiler
is present (this test does not require one -- the properties are about which code can run and
what it writes, which is read off the source).
"""
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
src = (root / 'xnu/files/bsd/dev/hakios.adreno.c').read_text()

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


def body(name):
    """The whole of a function definition, by matching braces from its opening one."""
    m = re.search(r'\n' + name + r'\([^;{]*\)\n\{', src)
    if m is None:
        raise SystemExit('could not find the definition of ' + name + '()')
    start = src.index('{', m.end() - 1)
    depth = 0
    for i in range(start, len(src)):
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise SystemExit('unbalanced braces in ' + name + '()')


def polaris_arm(text):
    """The `if (polaris) { ... }` block of a function, or None."""
    m = re.search(r'if \(polaris\) \{(.*?)\n\t\}', text, re.S)
    return None if m is None else m.group(1)


# ---------------------------------------------------------------- constants, evaluated

def macro_value(name):
    m = re.search(r'#define ' + name + r'\s+([^\n/]+)', src)
    if m is None:
        return None
    return evaluate(m.group(1).strip())


def evaluate(expression):
    """Evaluate a constant C expression: hex/decimal, + - * << | and names of macros.

    Macro names are substituted first (repeatedly, for macros of macros), then the arithmetic is
    left to Python, so that precedence is right: `0x05000000 + 4 * 0x3c00` must not be read
    left-to-right.
    """
    expression = expression.strip()
    # a ternary with polaris/a640 selects by the SoC we are asking about: take the true arm
    t = re.match(r'([^?]+)\?\s*([^:]+):\s*(.+)$', expression, re.S)
    if t:
        return evaluate(t.group(2))
    # substitute macros, innermost first, a few times over
    for _ in range(8):
        names = set(re.findall(r'\b[A-Za-z_]\w*\b', expression))
        names -= {'u', 'U', 'l', 'L', 'ull', 'ULL', 'ul', 'UL'}
        if not names:
            break
        substituted = expression
        for name in names:
            value = macro_value(name)
            if value is None:
                return None
            if re.fullmatch(r'\d+|0[xX][0-9a-fA-F]+', str(value)):
                substituted = re.sub(r'\b%s\b' % re.escape(name), str(value), substituted)
            else:
                substituted = re.sub(r'\b%s\b' % re.escape(name), '(%s)' % value, substituted)
        if substituted == expression:
            break
        expression = substituted
    expression = re.sub(r'(?<=[0-9a-fA-F])(?:[uU][lL]{0,2}|[lL]{1,2})\b', '', expression)
    if not re.fullmatch(r'[\d\s()+\-*|<>xXa-fA-F]+', expression):
        return None
    try:
        return int(eval(expression, {'__builtins__': {}}, {}))       # the expression is already constrained above
    except Exception:
        return None


m = re.search(r'else if \(hakios_is_polaris\(\)\) \{(.*?)\n\t\}', src, re.S)
if m is None:
    raise SystemExit('could not find the polaris branch in hakios_adreno_init()')
polaris_init = m.group(1)

assigned = {}
for a in re.finditer(r'^\t\t(\w+)\.(\w+) = ([^;]+);', polaris_init, re.M):
    assigned['%s.%s' % (a.group(1), a.group(2))] = evaluate(a.group(3))

# Every one of these addresses is a `reg` of the phone's own MIUI 12.5 device tree.
WINDOWS = [
    ('gpu.phys', 0x05000000), ('gpucc.phys', 0x05090000), ('gmu.phys', 0x0506a000),
    ('gmu.size', 0x30000), ('cx_misc.phys', 0x0509e000), ('smmu.phys', 0x05040000),
    ('smmu.size', 0x10000), ('pdc.phys', 0x0b200000), ('pdc.size', 0x300000),
    ('gbif.phys', 0x05000000 + 4 * 0x3c00),
    ('qfprom.phys', 0x00784000), ('qfprom.size', 0x8ff),
]
for window, value in WINDOWS:
    got = assigned.get(window)
    check(got == value, 'the polaris branch must set %s to %#x (it sets %s)' % (window, value, got))

check('rscc.phys' not in assigned, 'the polaris branch must not guess an rscc window')
check(assigned.get('smmu.size') != 0x20000, 'sdm845\'s SMMU window is 0x10000, not sc7280\'s 0x20000')
check(assigned.get('qfprom.phys') != 0x00780000,
      '0x780000 is qcom,msm-core on polaris, not the fuses')

# a640 must stay false on polaris: it is set only by hakios_is_raphael().
check(re.search(r'a640\s*=\s*hakios_is_raphael\(\)', src) is not None,
      'a640 must be set from hakios_is_raphael() alone')
check('a640 = hakios_is_polaris' not in src and 'a640 = polaris' not in src,
      'a640 must not be set from polaris')
check(re.search(r'polaris\s*=\s*hakios_is_polaris\(\)', src) is not None,
      'polaris must be set from hakios_is_polaris()')

# ---------------------------------------------------------------- soC-specific offsets

check('polaris ? 0x52004u' in src, 'both sdm845 GPU PLL votes live at 0x52004')
check(macro_value('GCC_DDRSS_GPU_AXI_CBCR') == 0x44038,
      'gcc_ddrss_gpu_axi_clk is 0x44038 on sdm845, 0x71154 on sc7280')
check(macro_value('GCC_PLL_VOTE') == 0x52004, 'the sdm845 GPU PLL vote is at 0x52004')
check(macro_value('CMD_DB_DESCRIPTOR_PHYS') == 0x0c3f000c,
      'sdm845\'s cmd-db is a descriptor at 0x0c3f000c, and the table is wherever it points')
check('CMD_DB_DESCRIPTOR_PHYS' in body('cmd_db_map'),
      'the cmd-db descriptor must be read before the table is mapped')

# The A630 streams, and they are not sc7280's (0/1/2/5).
for name, value in [('SDM845_STREAM_GFX3D_USER', 0), ('SDM845_STREAM_GFX3D_SECURE', 2),
                    ('SDM845_STREAM_GFX3D_SECURE2', 1), ('SDM845_STREAM_GMU_USER', 4),
                    ('SDM845_STREAM_GMU_KERNEL', 5)]:
    check(macro_value(name) == value,
          '%s must be %d (sdm845-gpu.dtsi iommus)' % (name, value))

# The GMU's register offsets are word offsets from the GPU base and are A630's already;
# a change to any of these would be a silent misread.
check(re.search(r'#define GMU_WORD\(n\)\s+\(4u \* \(n\)\)', src) is not None,
      'GMU_WORD must stay 4*n: the GMU\'s registers are word-addressed')
gmu_offsets = [('GMU_CM3_SYSRESET', 0x5000), ('GMU_CM3_BOOT_CONFIG', 0x5001),
               ('GMU_CM3_FW_INIT_RESULT', 0x501c), ('GMU_ALWAYS_ON_COUNTER_L', 0x5088),
               ('GMU_SPTPRAC_PWR_CLK_STATUS', 0x50d0), ('GMU_RPMH_POWER_STATE', 0x50ec),
               ('GMU_HFI_CTRL_STATUS', 0x5180), ('GMU_AO_HOST_INTERRUPT_STATUS', 0x9305),
               ('GMU_AHB_FENCE_STATUS', 0x9313)]
for name, word in gmu_offsets:
    m = re.search(r'#define ' + name + r'\s+GMU_WORD\(([^)]+)\)', src)
    check(m is not None, '%s must stay a GMU_WORD() offset' % name)
    if m:
        got = evaluate(m.group(1))
        check(got == word, '%s must be GMU_WORD(%#x) (it is GMU_WORD(%s))' % (name, word, m.group(1)))

# The GMU window must contain the highest of them.
check(assigned.get('gmu.size', 0) >= 4 * 0x9313 + 4,
      'the GMU window must cover the AHB fence status register at word 0x9313')

# ---------------------------------------------------------------- nothing writes on polaris

WRITES = re.compile(r'\b(?:put|set|wr)\s*\(')


# The write steps must refuse before they touch anything. Step 7 is a read step that still has
# to refuse, because the window it would read is another SoC's; it is listed separately below.
for name, step_no in [('step_cx_power', 4), ('step_clocks', 5), ('step_gmu_memory', 10),
                      ('step_rscc', 7)]:
    b = body(name)
    r = polaris_arm(b)
    check(r is not None, 'step %d (%s) must refuse on polaris' % (step_no, name))
    if r is None:
        continue
    check('NOT IMPLEMENTED FOR POLARIS' in r,
          'step %d (%s) must say NOT IMPLEMENTED FOR POLARIS' % (step_no, name))
    check(re.search(r'step_refused = true;\s*\n\s*return false;', r) is not None,
          'step %d (%s) must set step_refused and return false' % (step_no, name))
    check(WRITES.search(r) is None,
          'step %d (%s) must not write in its polaris path' % (step_no, name))

# The write steps must refuse before their first write in the function as a whole, so that no
# write can be reached ahead of the refusal.
for name, step_no in [('step_cx_power', 4), ('step_clocks', 5), ('step_gmu_memory', 10)]:
    b = body(name)
    r = polaris_arm(b)
    if r is None:
        continue
    first_write = WRITES.search(b)
    check(first_write is not None, 'step %d is expected to be a write step' % step_no)
    if first_write is not None:
        check(b.index(r) < first_write.start(),
              'step %d must refuse before its first write, not after it' % step_no)

# Step 7 reads on other SoCs, so it has no writes to be kept away from the phone; what matters
# is that its polaris arm never touches the rscc object, whose window is another SoC's.
rscc_arm = polaris_arm(body('step_rscc'))
check(rscc_arm is not None, 'step 7 must have a polaris arm')
check(rscc_arm is not None and '&rscc' not in rscc_arm and 'rscc.base' not in rscc_arm,
      'step 7 must not read an rscc window on polaris; the object it would read is another SoC\'s')
check(rscc_arm is not None and '0x2c8d000' in rscc_arm,
      'step 7 must say that it is not reading raphael\'s rscc address')

# The read steps must not write in their polaris path.
for name, step_no in [('step_outside', 1), ('step_fuses', 2), ('step_gpucc', 3),
                      ('step_first_words', 6), ('step_smmu', 8), ('step_smmu_context', 9)]:
    b = body(name)
    r = polaris_arm(b)
    if r is not None:
        check(WRITES.search(r) is None,
              'step %d (%s) must not write in its polaris path' % (step_no, name))

# step 3's one legacy write must be kept out of the polaris arm.
gpucc = body('step_gpucc')
check(re.search(r'if \(polaris\) \{(.*?)\n\t\} else \{\s*\n\s*announce', gpucc, re.S) is not None,
      'step 3 must branch on polaris before the gcc_gpu_cfg_ahb set()')
check('polaris does not write it on' in gpucc,
      'step 3 must read gcc_gpu_cfg_ahb on polaris rather than write it')
tail = gpucc[gpucc.index('} else {'):]
check('set(&gcc' in tail, 'the gcc_gpu_cfg_ahb set() must be in the non-polaris arm of step 3')

# The read steps that need CX on other SoCs must still be reachable on polaris, where step 4
# is refused: each must take the polaris arm, not the cx_ready() gate.
for name, step_no in [('step_first_words', 6), ('step_smmu', 8), ('step_smmu_context', 9)]:
    b = body(name)
    check(re.search(r'if \(polaris\) \{(.*?)\n\t\} else (?:if \(!cx_ready\(\)\)|\{)', b, re.S) is not None,
          'step %d must choose its polaris arm before cx_ready(), which polaris never satisfies' % step_no)
    # the polaris arm itself must not call cx_ready()
    r = polaris_arm(b)
    check(r is None or 'cx_ready' not in r,
          'step %d must not require cx_ready() on polaris' % step_no)

# step 10 must not put() to the GMU on polaris.
r = polaris_arm(body('step_gmu_memory'))
check(r is not None and 'put(&gmu' not in r, 'step 10 must not put() to the GMU on polaris')

# ---------------------------------------------------------------- cleanup writes nothing

for name in ('gpu_block_stop', 'stale_stop', 'gx_off', 'core_bus_halt', 'hakios_adreno_quiesce'):
    b = body(name)
    check(re.search(r'if \(polaris\)', b) is not None,
          '%s must return early on polaris' % name)
    r = polaris_arm(b)
    check(r is not None, '%s must have a polaris arm' % name)
    if r is not None:
        check(WRITES.search(r) is None, '%s must not write in its polaris path' % name)
        check(re.search(r'\breturn\b', r) is not None,
              '%s must return from its polaris path' % name)

# The blocks the reporting paths read must be mapped or reached through a helper that maps them.
for name in ('gpu_block_stop', 'stale_stop', 'core_bus_halt'):
    r = polaris_arm(body(name))
    if r is None:
        continue
    if 'gmu.base +' in r or '&gmu' in r:
        check('need(&gmu)' in r, '%s must need(&gmu) before reading it on polaris' % name)
    if 'gbif.base +' in r or '&gbif' in r:
        check('need(&gbif)' in r, '%s must need(&gbif) before reading it on polaris' % name)
    # gpucc is reached through gpucc_answers(), which itself calls need(&gpucc)
    if 'gpucc.base +' in r:
        check('gpucc_answers()' in r or 'need(&gpucc)' in r,
              '%s must map gpucc (directly or through gpucc_answers()) before reading it on polaris' % name)

check(re.search(r'if \(polaris\) \{\s*\n(?:\s*/\*.*?\*/\s*\n)?\s*return;', body('hakios_adreno_quiesce'), re.S)
      is not None, 'hakios_adreno_quiesce() must return at once on polaris')

# ---------------------------------------------------------------- the ladder's refusal rule

ladder = src[src.index('for (step = 1; step <= last_step; step++)'):]
ladder = ladder[:ladder.index('PE_parse_boot_argn("adreno_keep"')]
check('if (polaris && step > 10)' in ladder,
      'the step loop must refuse every step above 10 on polaris, in one place')
check('if (step_refused)' in ladder,
      'the step loop must tell a refusal from a FAILED step')
check(re.search(r'if \(step_refused\) \{\s*\n\s*refused_count\+\+;[\s\S]*?continue;', ladder) is not None,
      'a refused step must be counted and the ladder must go on to the next')
check(re.search(r'if \(step_refused\) \{[\s\S]*?continue;\s*\n\s*\}\s*\n\s*printf\("adreno: step %u: FAILED',
                ladder) is not None,
      'a step that is not refused must still stop the ladder')
check('step_refused = false;' in ladder, 'the refusal flag must be cleared before each step')
check('step %u: FAILED; the steps after it are not attempted' in ladder,
      'the FAILED wording must stay for steps that really failed')

# The read-only ladder must be what a polaris run reports, since it is the only thing asked of it.
for name, step_no in [('step_outside', 1), ('step_fuses', 2), ('step_gpucc', 3),
                      ('step_first_words', 6), ('step_smmu', 8), ('step_smmu_context', 9)]:
    b = body(name)
    check(re.search(r'if \(polaris\)', b) is not None,
          'step %d (%s) must say what it does on polaris' % (step_no, name))

# ---------------------------------------------------------------- the A630 firmware names

start = body('start_by_itself')
for image in ('a630_gmu.bin', 'a630_sqe.fw', 'a630_zap.mdt', 'a630_zap.b02'):
    check('"%s"' % image in start, 'start_by_itself() must name %s for polaris' % image)
for image in ('a640_gmu.bin', 'a660_gmu.bin'):
    check('"%s"' % image in start, 'the other SoCs\' firmware names must be kept (%s)' % image)
check(re.search(r'if \(polaris\) \{\s*\n(?:\s*/\*.*?\*/\s*\n)?\s*printf\("adreno: THE GPU IS NOT STARTED'
                r'[\s\S]*?return false;', start, re.S) is not None,
      'gpu=1 must be refused on polaris, since the write steps are not implemented')
check('no .bcms' in src or 'no .bcms' in start or '.bcms' in start or 'bcms' in src,
      'the absence of a630 .bcms should be noted (A630 does no bus scaling)')

# ---------------------------------------------------------------- the declarations exist

check('static bool polaris;' in src, 'the polaris flag must be declared')
check('static bool step_refused;' in src, 'the refusal flag must be declared')
check('static uint32_t refused_count;' in src, 'refusals must be counted, for the end-of-run line')
check('#include <sys/hakios.input.h>' in src, 'hakios_is_polaris() must be declared')

# ---------------------------------------------------------------- every refusal names a source

# A TODO must not be a bare apology: each says where the value is to come from.
for todo in re.finditer(r'TODO\(polaris\)[^\n]*\n(?:[^\n]*\n){0,4}', src):
    text = todo.group(0)
    if 'polaris-o-oss' in text or 'gpucc-sdm845' in text or 'a6xx' in text:
        continue
    failures.append('a TODO(polaris) does not name where the value comes from: ' + text.strip().splitlines()[0])

if failures:
    print('FAIL: %d polaris-related assertion(s) do not hold' % len(failures))
    for f in failures:
        print('  -', f)
    sys.exit(1)

# ---------------------------------------------------------------- a C harness, when possible

compiler = shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')
compiled = 'skipped (no cc/gcc/clang on this machine)'
if compiler:
    # Compile the polaris window values and the GMU word offsets into a program, so they are
    # exercised as numbers rather than only compared as text.
    harness = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#define GMU_WORD(n) (4u * (n))
int main(void) {
    const uint32_t gpu_phys = 0x05000000, gpucc_phys = 0x05090000, gmu_phys = 0x0506a000;
    const uint32_t gmu_size = 0x30000, cx_misc_phys = 0x0509e000;
    const uint32_t smmu_phys = 0x05040000, smmu_size = 0x10000;
    const uint32_t qfprom_phys = 0x00784000, qfprom_size = 0x8ff;
    const uint32_t gbif_phys = 0x05000000 + 4 * 0x3c00, gbif_word = 0x3c00;
    assert(gbif_phys - gpu_phys == 0xf000                 /* GBIF is the 3D core window's, at word 0x3c00 */);
    assert(gbif_word * 4 == gbif_phys - gpu_phys);
    assert(gpucc_phys - gpu_phys == 0x90000);
    assert(gmu_phys - gpu_phys == 0x6a000);
    assert(cx_misc_phys - gpu_phys == 0x9e000);
    assert(smmu_phys - gpu_phys == 0x40000);
    assert(smmu_size == 0x10000 && smmu_size != 0x20000);  /* not sc7280's */
    assert(qfprom_phys == 0x784000 && qfprom_phys != 0x780000);
    assert(qfprom_size == 0x8ff);
    /* every GMU offset the read steps use, as a byte offset, must be inside the window */
    assert(GMU_WORD(0x5000) < gmu_size && GMU_WORD(0x5001) < gmu_size);
    assert(GMU_WORD(0x501c) < gmu_size && GMU_WORD(0x5088) < gmu_size);
    assert(GMU_WORD(0x50d0) < gmu_size && GMU_WORD(0x50ec) < gmu_size);
    assert(GMU_WORD(0x5180) < gmu_size && GMU_WORD(0x9305) < gmu_size);
    assert(GMU_WORD(0x9313) + 4 <= gmu_size);
    assert(GMU_WORD(0x1c00) == 0x7000);                    /* the DTCM the firmware lands in */
    /* the 3D core window is big enough to hold GBIF, and cx_misc is its own page */
    assert(gbif_phys + 0x1000 <= gpu_phys + 0x40000);
    printf("polaris: gpu %#x gmu %#x (%#x) gpucc %#x smmu %#x (%#x) qfprom %#x (%#x) gbif %#x\n",
           gpu_phys, gmu_phys, gmu_size, gpucc_phys, smmu_phys, smmu_size, qfprom_phys, qfprom_size, gbif_phys);
    return 0;
}
'''
    with tempfile.TemporaryDirectory(prefix='hackios-polaris-') as tmp:
        path = Path(tmp)
        (path / 'test.c').write_text(harness)
        build = subprocess.run([compiler, '-std=c11', '-Wall', '-Wextra', '-Werror',
                                str(path / 'test.c'), '-o', str(path / 'test')],
                               capture_output=True, text=True)
        if build.returncode != 0:
            print('FAIL: the window assertions do not compile')
            print(build.stderr)
            sys.exit(1)
        subprocess.run([str(path / 'test')], check=True)
        compiled = 'ran (%s)' % compiler

print("PASS: polaris (SDM845 / Adreno 630) is read-only.")
print("      Steps 1-3, 6, 8 and 9 read the phone's own device tree windows; steps 4, 5, 7, 10")
print("      and 11-22 refuse by name; the ladder goes on past a refusal and still stops on a")
print("      real failure; the end-of-run cleanup writes nothing. C harness: %s." % compiled)
