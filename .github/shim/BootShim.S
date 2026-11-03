/*
 * BootShim for HACKiOS: place both the UEFI firmware volume and the XNUP payload
 * where the firmware will look for them, then jump to the firmware.
 *
 * The upstream shim copies FD_SIZE bytes to FD_BASE and jumps. That is enough for a
 * firmware that reads its payload from wherever the boot image put it, but not for
 * this one: the payload rides in the Android boot image directly after the FD, so it
 * arrives at FD_BASE + FD_SIZE, while the firmware reads XNU_PAYLOAD_BASE
 * (Silicon/Silicium/SiliciumPkg/Library/BootManagerLib/XnuLoader.h). On raphael those
 * happened to be close enough that the mismatch went unnoticed; on polaris the
 * firmware printed
 *
 *     HACKIOS POLARIS: XNUP at 0xA0000000, magic FFFB77FF, size 4294966269
 *
 * -- 0xFFFB77FF is not "XNUP" and 4294966269 is -1027 read as unsigned, which is what
 * an untouched page of RAM looks like. The payload was at 0x9FE20000.
 *
 * So copy the payload too:
 *
 *   FD      FD_BASE .. FD_BASE + FD_SIZE          (as upstream)
 *   payload FD_BASE + FD_SIZE .. XNU_PAYLOAD_BASE + PAYLOAD_SIZE
 *
 * PAYLOAD_SIZE is the number of bytes copied: from just past the FD up to the end of
 * whatever the boot image carried. It is passed in as a constant because the shim runs
 * before anything can compute it. Copying DOWN from 0x9FE20000 to 0xA0000000 means
 * destination is above source, and the regions do not overlap (0xA0000000 is 0x1E0000
 * beyond the end of the FD copy).
 *
 * Everything here is position independent: it runs at whatever address the bootloader
 * loaded it to, with the MMU and caches as the bootloader left them.
 */

#define PAYLOAD_DEST XNU_PAYLOAD_BASE

_Head:
  adr   x1, _Payload    /* x1 = where this image ends = the FD that follows */
  b     _Start

#if REQUIRES_KERNEL_HEADER == 1
_StackBase:
  .quad FD_BASE         /* Text Offset */

_StackSize:
  .quad FD_SIZE         /* Image Size */
  .quad 0               /* Flags */
  .quad 0               /* Reserved */
  .quad 0               /* Reserved */
  .quad 0               /* Reserved */
  .ascii "ARM\x64"      /* ARM64 Magic */
  .long 0               /* Reserved */
#endif

_Start:
  mov   x4, x1          /* x4 = source: the FD lives immediately after this shim */
  ldr   x5, =FD_BASE
  cmp   x4, x5
  beq   _PayloadPass    /* already in place: skip the FD copy */
  ldr   x6, =FD_SIZE

_CopyFd:
  ldp   x2, x3, [x4], #16
  stp   x2, x3, [x5], #16
  subs  x6, x6, #16
  b.ne  _CopyFd

_PayloadPass:
  ldr   x7, =PAYLOAD_SIZE
  cbz   x7, _Entry      /* no payload in this image */
  ldr   x8, =FD_BASE
  ldr   x9, =FD_SIZE
  add   x8, x8, x9      /* x8 = FD_BASE + FD_SIZE: where the payload is now */
  ldr   x9, =PAYLOAD_DEST
  cmp   x8, x9
  beq   _Entry          /* already where the firmware wants it */

_CopyPayload:
  /* Copy 16 bytes at a time; the tail (< 16 bytes) is copied bytewise so that
     PAYLOAD_SIZE needs no rounding by whoever packs the image. */
  cmp   x7, #16
  b.lt  _CopyPayloadTail
  ldp   x2, x3, [x8], #16
  stp   x2, x3, [x9], #16
  sub   x7, x7, #16
  b     _CopyPayload

_CopyPayloadTail:
  cbz   x7, _Entry
  ldrb  w2, [x8], #1
  strb  w2, [x9], #1
  sub   x7, x7, #1
  b     _CopyPayloadTail

_Entry:
  ldr   x5, =FD_BASE
  br    x5

_Dead:
  b     _Dead

.text
.align 4

_Payload:
