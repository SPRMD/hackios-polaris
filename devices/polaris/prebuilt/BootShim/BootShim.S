/*
 * BootShim for HACKiOS: place the UEFI firmware volume and the XNUP payload where the
 * firmware will look for them, then jump to the firmware.
 *
 * The upstream shim copies FD_SIZE bytes to FD_BASE and jumps. That is enough only if the
 * firmware reads its payload from where the boot image left it. This firmware does not: it
 * reads XNU_PAYLOAD_BASE (Silicon/Silicium/SiliciumPkg/Library/BootManagerLib/XnuLoader.h),
 * a fixed address. The payload rides in the Android boot image immediately after the FD, so
 * it arrives at (_Payload + FD_SIZE) in whatever memory the bootloader unpacked the kernel
 * to -- not at XNU_PAYLOAD_BASE. On polaris the firmware said so itself:
 *
 *     HACKIOS POLARIS: XNUP at 0xA0000000, magic FFFB77FF, size 4294966269
 *
 * 0xFFFB77FF is not "XNUP" and 4294966269 is -1027 read unsigned: untouched memory. So this
 * shim copies the payload to XNU_PAYLOAD_BASE as well.
 *
 *   FD       _Payload          -> FD_BASE            (FD_SIZE bytes)
 *   payload  _Payload + FD_SIZE -> XNU_PAYLOAD_BASE  (PAYLOAD_SIZE bytes)
 *
 * Both destinations are read from literals, so this works wherever the image was loaded.
 * The payload copy walks forward, which is the safe direction when destination < source;
 * when the two are equal it is skipped, and when the bootloader happened to unpack the
 * image above XNU_PAYLOAD_BASE the copy is still downward because
 * _Payload + FD_SIZE > XNU_PAYLOAD_BASE for any plausible load address.
 *
 * Parameters are -D defines from the Makefile: FD_BASE, FD_SIZE, XNU_PAYLOAD_BASE,
 * PAYLOAD_SIZE.
 */

_Head:
  adr   x1, _Payload    /* x1 = end of this shim = where the FD begins */
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
  mov   x4, x1          /* x4 = _Payload: the FD starts here */
  ldr   x10, =FD_SIZE
  add   x10, x4, x10    /* x10 = where the payload is now: just past the FD */

  /* ---- the firmware volume ---- */
  mov   x11, x4         /* x11 = FD source cursor */
  ldr   x5, =FD_BASE
  ldr   x6, =FD_SIZE
  cmp   x11, x5
  beq   _PayloadPass    /* already at FD_BASE: nothing to copy */

_CopyFd:
  ldp   x2, x3, [x11], #16
  stp   x2, x3, [x5], #16
  subs  x6, x6, #16
  b.ne  _CopyFd

  /* ---- the XNUP payload ---- */
_PayloadPass:
  ldr   x7, =PAYLOAD_SIZE
  cbz   x7, _Entry      /* this image carries no payload */
  ldr   x9, =XNU_PAYLOAD_BASE
  cmp   x10, x9
  beq   _Entry          /* already where the firmware reads it */

_CopyPayload:
  cmp   x7, #16
  b.lt  _CopyPayloadTail
  ldp   x2, x3, [x10], #16
  stp   x2, x3, [x9], #16
  sub   x7, x7, #16
  b     _CopyPayload

_CopyPayloadTail:
  /* The tail is shorter than 16 bytes; copy it bytewise so PAYLOAD_SIZE needs no
     rounding by whoever packs the image. */
  cbz   x7, _Entry
  ldrb  w2, [x10], #1
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
