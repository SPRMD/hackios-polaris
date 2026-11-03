#!/bin/bash
# Build the XNU loader as a freestanding AArch64 UEFI application.
set -e
cd "$(dirname "$0")"
# the iPhone's identity for XNU, from the device tree of the builder's own IPSW (not in the repository)
[ -f identity.h ] || ./mkidentity.sh

# PAYLOAD_BASE is per board and must equal, exactly, both XNU_PAYLOAD_BASE in the UEFI
# firmware's XnuLoader.h and the address the boot shim copies the payload to. raphael and
# redwood use 0xF0000000; polaris uses 0xA0000000 because 0xF0000000 is above its DDR.
# The caller (devices/<name>/device.conf via build.sh) exports it; the default in loader.c
# stays raphael's.
PAYLOAD_DEFINE=""
if [ -n "$PAYLOAD_BASE" ]; then
  PAYLOAD_DEFINE="-DPAYLOAD_BASE=$PAYLOAD_BASE"
fi
# The loader's own PE base sits above the payload on polaris (0xA0000000 + 64 MiB region),
# so it is derived rather than fixed: a loader linked into the payload's reservation would
# overwrite the container it is reading.
PE_BASE=${LOADER_PE_BASE:-0xA1000000}

clang --target=aarch64-unknown-windows -ffreestanding -fno-builtin -fno-stack-protector -fshort-wchar \
  -mgeneral-regs-only -O1 -Wall -Wextra -Wno-unused-parameter $PAYLOAD_DEFINE -c loader.c -o loader.obj
# /base: UEFI loads an image at its preferred address when that is free. The PE default (0x140000000)
# is in the middle of the phone's largest block of RAM, which XNU needs in one piece.
lld-link /subsystem:efi_application /entry:efi_main /nodefaultlib /base:$PE_BASE /out:loader.efi loader.obj
ls -la loader.efi
