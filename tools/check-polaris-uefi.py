#!/usr/bin/env python3
"""Internal consistency checks for devices/polaris/uefi.

Deliberately toolchain-free: it reads the files this repository owns and the numbers
they claim about each other. It does not build, does not need EDK2, does not need
network, and never touches a phone. What it cannot check is whether the numbers match
the hardware -- only polaris' own xbl.img/DTB can say that, and that is recorded in
devices/polaris/uefi/README.md.

Run: python3 tools/check-polaris-uefi.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UEFI = ROOT / "devices" / "polaris" / "uefi"
PKG = UEFI / "files" / "Platforms" / "Xiaomi" / "polarisPkg"

errors: list[str] = []
warnings: list[str] = []


def bad(where: str, message: str) -> None:
    errors.append(f"{where}: {message}")


def warn(where: str, message: str) -> None:
    warnings.append(f"{where}: {message}")


def read(path: Path) -> str:
    if not path.is_file():
        bad(str(path.relative_to(ROOT)), "missing")
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def parse_conf(path: Path) -> dict[str, str]:
    """KEY=VALUE lines of a devices/*/device.conf (sourced by bash, so keep it dumb)."""
    out: dict[str, str] = {}
    for line in read(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"')
    return out


def num(text: str) -> int:
    return int(text, 0)


def code_only(text: str) -> str:
    """Drop comments so that prose explaining why a value is wrong is not flagged as the value.

    Handles C/C++ (// and /* */), shell/TOML/Python (#) and .inf/INI (;). Good enough for the
    files here; these checks are a safety net for edits, not a lexer.
    """
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    out = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("#", ";")):
            continue
        out.append(re.sub(r"//.*$", " ", line))
    return "\n".join(out)


# --------------------------------------------------------------------------------------- #
# 1. the shape of the package                                                               #
# --------------------------------------------------------------------------------------- #
REQUIRED = [
    "BASE",
    "README.md",
    "apply.sh",
    "export.sh",
    "mu-silicium.patch",
    "polaris-firmware-patches.json",
    "files/Resources/Configs/polaris.toml",
    "files/Platforms/Xiaomi/polarisPkg/polaris.dsc",
    "files/Platforms/Xiaomi/polarisPkg/polaris.fdf",
    "files/Platforms/Xiaomi/polarisPkg/polaris.dec",
    "files/Platforms/Xiaomi/polarisPkg/DeviceBuild.py",
    "files/Platforms/Xiaomi/polarisPkg/Include/APRIORI.inc",
    "files/Platforms/Xiaomi/polarisPkg/Include/DXE.inc",
    "files/Platforms/Xiaomi/polarisPkg/Include/RAW.inc",
    "files/Platforms/Xiaomi/polarisPkg/Library/MemoryMapLib/MemoryMapLib.c",
    "files/Platforms/Xiaomi/polarisPkg/Library/MemoryMapLib/MemoryMapLib.inf",
    "files/Platforms/Xiaomi/polarisPkg/Library/ConfigurationMapLib/ConfigurationMapLib.c",
    "files/Platforms/Xiaomi/polarisPkg/Library/ConfigurationMapLib/ConfigurationMapLib.inf",
    "files/Silicon/Silicium/SiliciumPkg/Library/BootManagerLib/XnuLoader.c",
    "files/Silicon/Silicium/SiliciumPkg/Library/BootManagerLib/XnuLoader.h",
]
for name in REQUIRED:
    if not (UEFI / name).is_file():
        bad("devices/polaris/uefi", f"missing {name}")

# No fabricated binaries. raphael's rule, and the one that matters most here.
for path in UEFI.rglob("*"):
    if path.is_file() and path.suffix in (".efi", ".depex", ".bin", ".fd", ".dtb"):
        bad(
            str(path.relative_to(ROOT)),
            "binary in the port tree: polaris has no build output here (see README.md, "
            "'what is still missing'); do not commit placeholders",
        )
    if path.is_file() and path.stat().st_size == 0 and path.suffix not in (".dec",):
        warn(str(path.relative_to(ROOT)), "empty file")

# --------------------------------------------------------------------------------------- #
# 2. what the device profile and the package say about the same addresses                    #
# --------------------------------------------------------------------------------------- #
conf = parse_conf(ROOT / "devices" / "polaris" / "device.conf")
fd_base = num(conf["FD_BASE"]) if "FD_BASE" in conf else None
fd_size = num(conf["FD_SIZE"]) if "FD_SIZE" in conf else None
payload_base = num(conf["PAYLOAD_BASE"]) if "PAYLOAD_BASE" in conf else None
fb_base = num(conf["FB_BASE"]) if "FB_BASE" in conf else None
for key, value in (("FD_BASE", fd_base), ("FD_SIZE", fd_size), ("PAYLOAD_BASE", payload_base), ("FB_BASE", fb_base)):
    if value is None:
        bad("devices/polaris/device.conf", f"{key} not set")

# device.conf is the single source of these numbers; the UEFI port must not restate them.
# If an address has to change, it changes there first (that file also carries the measurement
# and its provenance), and the two or three places that consume it follow.

# the .toml build_uefi.py reads ([uefi_fd] base/size drives the FDF and BootShim)
toml_text = read(UEFI / "files" / "Resources" / "Configs" / "polaris.toml")
try:
    import tomllib

    toml = tomllib.loads(toml_text)
except ModuleNotFoundError:  # pragma: no cover - CI has 3.11+
    bad("polaris.toml", "python3.11+ (tomllib) is required")
    toml = {}
except Exception as exc:  # noqa: BLE001 - report and continue
    bad("polaris.toml", f"not valid TOML: {exc}")
    toml = {}

uefi_fd = toml.get("uefi_fd", {})
if uefi_fd:
    if uefi_fd.get("base") != fd_base:
        bad("polaris.toml", f"[uefi_fd] base {uefi_fd.get('base'):#x} != device.conf FD_BASE {fd_base:#x}")
    if uefi_fd.get("size") != fd_size:
        bad(
            "polaris.toml",
            f"[uefi_fd] size {uefi_fd.get('size'):#x} != device.conf FD_SIZE {fd_size:#x}. "
            "The FDF gets FD_SIZE as the FD image size and BootShim's make gets it as "
            "FD_SIZE, while mkxnuimg.py asserts len(fd) == FD_SIZE.",
        )
if fd_size is not None and fd_size % 0x1000:
    bad("devices/polaris/device.conf", f"FD_SIZE {fd_size:#x} is not a multiple of the 0x1000 block size")
if toml.get("boot_shim", {}).get("requires_kernel_header") is not True:
    bad("polaris.toml", "[boot_shim] requires_kernel_header must be true (BootShim expects a kernel header)")
if toml.get("boot_image_kernel", {}).get("append_dtb") is not True:
    bad("polaris.toml", "[boot_image_kernel] append_dtb must be true: XNU is given the DTB from the image")

# XnuLoader.h must agree; the loader writes PAYLOAD_BASE from inside UEFI
loader_h = read(UEFI / "files" / "Silicon" / "Silicium" / "SiliciumPkg" / "Library" / "BootManagerLib" / "XnuLoader.h")
m = re.search(r"#define\s+XNU_PAYLOAD_BASE\s+(0x[0-9A-Fa-f]+)", loader_h)
h_payload = num(m.group(1)) if m else None
if h_payload is None:
    bad("XnuLoader.h", "XNU_PAYLOAD_BASE not found")
elif h_payload != payload_base:
    bad("XnuLoader.h", f"XNU_PAYLOAD_BASE {h_payload:#x} != device.conf PAYLOAD_BASE {payload_base:#x}")
m = re.search(r"#define\s+XNU_PAYLOAD_MAX_SIZE\s+(0x[0-9A-Fa-f]+)", loader_h)
h_max = num(m.group(1)) if m else None
m = re.search(r'#define\s+XNU_LOG_PARTITION\s+L"([^"]+)"', loader_h)
h_partition = m.group(1) if m else None
if h_partition is None:
    bad("XnuLoader.h", "XNU_LOG_PARTITION not found")

# --------------------------------------------------------------------------------------- #
# 3. the memory map: entries, overlaps, and the regions hackios depends on                  #
# --------------------------------------------------------------------------------------- #
MAP = UEFI / "files" / "Platforms" / "Xiaomi" / "polarisPkg" / "Library" / "MemoryMapLib" / "MemoryMapLib.c"
map_text = read(MAP)
entry_re = re.compile(
    r'\{\s*"([^"]+)"\s*,\s*(0x[0-9A-Fa-f]+)\s*,\s*(0x[0-9A-Fa-f]+)\s*,\s*'
    r"(AddMem|AddDev|NoHob)\s*,\s*([A-Z_]+)\s*,\s*([A-Z_]+)\s*,\s*([A-Za-z]+)\s*,\s*([A-Z_]+)\s*\}"
)
entries: dict[str, tuple[int, int, str, str]] = {}
for line in map_text.splitlines():
    if line.strip().startswith("//"):
        continue
    for hit in entry_re.finditer(line):
        name, addr, size, hob, rtype, rattr, mtype, attr = hit.groups()
        if name in entries:
            bad("MemoryMapLib.c", f"duplicate region name {name!r}")
        entries[name] = (num(addr), num(size), hob, rtype)
if not entries:
    bad("MemoryMapLib.c", "no memory descriptors parsed")

# The four regions hackios itself depends on. A clash here is a boot failure, not a style issue.
def region(name: str) -> tuple[int, int] | None:
    got = entries.get(name)
    return (got[0], got[1]) if got else None


def expect(name: str, addr: int | None, size: int | None) -> None:
    got = region(name)
    if got is None:
        bad("MemoryMapLib.c", f"missing region {name!r} (hackios needs it; upstream polarisPkg may not ship it)")
        return
    if addr is not None and got[0] != addr:
        bad("MemoryMapLib.c", f"{name!r} at {got[0]:#x}, expected {addr:#x}")
    if size is not None and got[1] != size:
        bad("MemoryMapLib.c", f"{name!r} size {got[1]:#x}, expected {size:#x}")


expect("UEFI FD", fd_base, 0x00300000)  # the reserved region, NOT the image (polaris.toml says 0x220000)
expect("XNU Payload", payload_base, 0x04000000)
expect("Display Reserved", fb_base, 0x02400000)

# UEFI Stack must match the .dsc, or the primary core starts on someone else's memory.
dsc = read(PKG / "polaris.dsc")
m = re.search(r"PcdCPUCoresStackBase\|(0x[0-9A-Fa-f]+)", dsc)
dsc_stack = num(m.group(1)) if m else None
m = re.search(r"PcdCPUCorePrimaryStackSize\|(0x[0-9A-Fa-f]+)", dsc)
dsc_stack_size = num(m.group(1)) if m else None
if dsc_stack is None or dsc_stack_size is None:
    bad("polaris.dsc", "PcdCPUCoresStackBase / PcdCPUCorePrimaryStackSize not found")
else:
    expect("UEFI Stack", dsc_stack, dsc_stack_size)

if fd_base is not None and fd_size is not None:
    uefi_fd_region = region("UEFI FD")
    if uefi_fd_region and fd_size > uefi_fd_region[1]:
        bad(
            "polaris.toml",
            f"FD image size {fd_size:#x} does not fit the 'UEFI FD' reservation {uefi_fd_region[1]:#x}; "
            "the two are different quantities and this is the one relation between them",
        )

if payload_base is not None and h_max:
    ddr = [(a, s) for a, s, _h, t in entries.values() if t == "SYS_MEM" or t == "MEM_RES"]
    ddr_top = max(a + s for a, s in ddr) if ddr else 0
    if payload_base < 0x80000000 or payload_base + h_max > ddr_top:
        bad(
            "XnuLoader.h",
            f"payload {payload_base:#x}+{h_max:#x} is outside DDR (0x80000000..{ddr_top:#x}); "
            "the boot shim writes there with no mapping of its own",
        )

# Overlaps. Upstream's own table has some (MDSS/DISP_CC); only flag the ones that would matter.
CRITICAL = {"XNU Payload", "UEFI FD", "Display Reserved", "UEFI Stack", "SEC Heap", "MMU PageTables"}
items = sorted(entries.items(), key=lambda kv: kv[1][0])
for i, (name_a, (addr_a, size_a, _ha, _ta)) in enumerate(items):
    for name_b, (addr_b, size_b, _hb, _tb) in items[i + 1 :]:
        if addr_b >= addr_a + size_a:
            break
        where = f"MemoryMapLib.c: {name_a!r} [{addr_a:#x},{addr_a + size_a:#x}) overlaps {name_b!r} [{addr_b:#x},{addr_b + size_b:#x})"
        if name_a in CRITICAL or name_b in CRITICAL:
            bad(where, "region hackios depends on")
        else:
            warn(where, "inherited from upstream; verify before relying on either region")

# --------------------------------------------------------------------------------------- #
# 4. no SM8150 values left behind, and every unknown is marked TODO(polaris)                #
# --------------------------------------------------------------------------------------- #
# raphael (SM8150) / cepheus values that must not appear as a polaris value anywhere.
FOREIGN = {
    "0x9FFB0000": "raphael SM8150 UEFI stack",
    "0x9FD00000": "redwood FD base",
    "HanaPkg": "SM8150 SoC package",
    "Binaries/raphael": "raphael's firmware modules",
    "Binaries/cepheus": "cepheus' firmware modules",
    "HACKIOS RAPHAEL": "raphael's banner string",
}
for path in sorted(UEFI.rglob("*")):
    if not path.is_file() or path.suffix in (".json",):
        continue
    text = code_only(read(path))
    rel = str(path.relative_to(ROOT))
    for needle, why in FOREIGN.items():
        if needle in text:
            bad(rel, f"contains {needle!r} ({why})")

# PAYLOAD_BASE history: 0xF0000000 is raphael's and is outside DDR here.
for path in sorted(UEFI.rglob("*")):
    if path.is_file() and path.suffix in (".c", ".h", ".toml", ".dsc", ".fdf"):
        lowered = code_only(read(path)).lower()
        if "0xf0000000" in lowered:
            bad(str(path.relative_to(ROOT)), "0xF0000000 is raphael's PAYLOAD_BASE and is MMIO on polaris")

# polaris's SoC package is NapaliPkg (SDM845). HanaPkg is SM8150 and is caught above.
if "NapaliPkg/NapaliPkg.dsc.inc" not in dsc:
    bad("polaris.dsc", "does not include NapaliPkg/NapaliPkg.dsc.inc (the SDM845 SoC package)")
if "PcdSystemMemoryBase|0x80000000" not in dsc:
    bad("polaris.dsc", "PcdSystemMemoryBase is not 0x80000000")
if "polarisPkg/Library/MemoryMapLib/MemoryMapLib.inf" not in dsc:
    bad("polaris.dsc", "does not use its own MemoryMapLib")

# The FDF has to build the FD out of the flags build_uefi.py passes.
fdf = read(PKG / "polaris.fdf")
for needle in ("$(FD_BASE)", "$(FD_SIZE)", "$(FD_BLOCKS)", "Include/APRIORI.inc", "Include/DXE.inc", "Include/RAW.inc"):
    if needle not in fdf:
        bad("polaris.fdf", f"missing {needle}")

# --------------------------------------------------------------------------------------- #
# 5. the driver lists reference polaris' own modules                                        #
# --------------------------------------------------------------------------------------- #
for inc in ("APRIORI.inc", "DXE.inc", "RAW.inc"):
    text = read(PKG / "Include" / inc)
    for line in text.splitlines():
        line = line.strip()
        if "Binaries/" in line and "Binaries/polaris/" not in line:
            bad(f"Include/{inc}", f"{line!r} does not use Binaries/polaris")
    if inc == "RAW.inc":
        raws = re.findall(r'FILE FREEFORM = ([0-9A-Fa-f-]+) \{', text)
        uis = re.findall(r'SECTION UI = "([^"]+)"', text)
        if len(raws) != len(uis):
            bad("Include/RAW.inc", f"{len(raws)} FREEFORM files but {len(uis)} UI names")
        for ui in uis:
            if f"Binaries/polaris/RawFiles/{ui}" not in text:
                bad("Include/RAW.inc", f"{ui!r} has no SECTION RAW under Binaries/polaris/RawFiles")
        if not raws:
            bad("Include/RAW.inc", "no FREEFORM files at all: the battery/charger provisioning is missing")
        else:
            warn("Include/RAW.inc", f"{len(raws)} FREEFORM files: exist only once Binaries/polaris is present")

if "XNU Payload" not in map_text:
    bad("MemoryMapLib.c", "the 'XNU Payload' reservation is gone; the boot shim would write into unmapped memory")
if "XNU_PAYLOAD_BASE" not in read(
    UEFI / "files" / "Silicon" / "Silicium" / "SiliciumPkg" / "Library" / "BootManagerLib" / "XnuLoader.c"
):
    bad("XnuLoader.c", "does not use XNU_PAYLOAD_BASE")

# --------------------------------------------------------------------------------------- #
# 6. the Mu-Silicium patch: expected files only, polaris strings only                       #
# --------------------------------------------------------------------------------------- #
patch = read(UEFI / "mu-silicium.patch")
touched = re.findall(r"^diff --git a/(\S+)", patch, re.M)
expected = {
    "BootShim/BootShim.S",
    "Silicon/Silicium/SiliciumPkg/Library/BootManagerLib/BootManager.c",
    "Silicon/Silicium/SiliciumPkg/Library/BootManagerLib/BootManagerLib.inf",
    "Silicon/Silicium/SiliciumPkg/Sec/Sec.c",
}
if set(touched) != expected:
    bad("mu-silicium.patch", f"touches {sorted(touched)}, expected {sorted(expected)}")
for needle in ("PAYLOAD_BASE", "XnuLoaderMain", "XNU_PAYLOAD_BOOT"):
    if needle not in patch:
        bad("mu-silicium.patch", f"does not mention {needle}")
if re.search(r"^new file mode", patch, re.M):
    bad("mu-silicium.patch", "adds files; new files belong in files/ (see export.sh)")

base = (UEFI / "BASE").read_text(encoding="utf-8").strip()
if not re.fullmatch(r"[0-9a-f]{40}", base):
    bad("devices/polaris/uefi/BASE", f"{base!r} is not a full commit id")

# --------------------------------------------------------------------------------------- #
# 7. firmware patch manifest: hashes are hashes, and nothing claims to be applied here       #
# --------------------------------------------------------------------------------------- #
manifest_path = UEFI / "polaris-firmware-patches.json"
try:
    manifest = json.loads(read(manifest_path))
except Exception as exc:  # noqa: BLE001
    bad("polaris-firmware-patches.json", f"invalid JSON: {exc}")
    manifest = {}
for mod in manifest.get("patched_modules", []):
    for field in ("module", "patched_file", "unpatched_sha256", "patched_sha256"):
        if not mod.get(field):
            bad("polaris-firmware-patches.json", f"{mod.get('module', '?')}: {field} missing")
    for field in ("unpatched_sha256", "patched_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(mod.get(field, ""))):
            bad("polaris-firmware-patches.json", f"{mod.get('module', '?')}: {field} is not a sha256")

# --------------------------------------------------------------------------------------- #
# 8. TODOs: every one names polaris and says what is unknown                                #
# --------------------------------------------------------------------------------------- #
todo_re = re.compile(r"TODO\(([^)]*)\)")
for path in sorted(UEFI.rglob("*")):
    if path.is_file() and path.suffix in (".c", ".h", ".dsc", ".fdf", ".inc", ".md", ".py", ".json", ".toml", ".sh"):
        for lineno, line in enumerate(read(path).splitlines(), 1):
            for who in todo_re.findall(line):
                if who != "polaris":
                    bad(f"{path.relative_to(ROOT)}:{lineno}", f"TODO({who}) in a polaris file")
                if ":" not in line.split(f"TODO({who})", 1)[1]:
                    warn(f"{path.relative_to(ROOT)}:{lineno}", "TODO(polaris) does not say what is unknown")

# --------------------------------------------------------------------------------------- #
for message in warnings:
    print(f"warning: {message}")
for message in errors:
    print(f"error: {message}", file=sys.stderr)
print(f"check-polaris-uefi: {len(entries)} memory regions, {len(errors)} errors, {len(warnings)} warnings")
sys.exit(1 if errors else 0)
