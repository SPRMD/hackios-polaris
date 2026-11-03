#ifndef _XNU_LOADER_H_
#define _XNU_LOADER_H_

//
// The boot shim copies everything appended after the UEFI FD to this address.
// The region is marked reserved in the device memory map (polarisPkg's
// MemoryMapLib, entry "XNU Payload") and must stay in sync with PAYLOAD_BASE in
// devices/polaris/device.conf, FD_BASE/FD_SIZE in the same file, and the
// [uefi_fd] section of Resources/Configs/polaris.toml.
//
// polaris: 0xA0000000, not raphael's 0xF0000000. polaris' DDR ends at 0xB9400000
// (MLVM, the last reserved region, ends there); 0xF0000000 is inside the MMIO hole
// below 4 GiB, so a payload copied there would go nowhere. 0xA0000000 is also
// exactly where "Display Reserved" ends (0x9D400000 + 0x02400000).
//
#define XNU_PAYLOAD_BASE      0xA0000000ULL
#define XNU_PAYLOAD_MAX_SIZE  0x04000000ULL
#define XNU_PAYLOAD_MAGIC     0x50554E58 // "XNUP"

typedef struct {
  UINT32 Magic;
  UINT32 Size;
} XNU_PAYLOAD_HEADER;

// The header is followed by entries; an empty name ends the list.
typedef struct {
  CHAR8  Name[16];
  UINT32 Size;
  UINT32 Reserved;
  // UINT8 Data[Size], padded to 16 bytes
} XNU_PAYLOAD_ENTRY;

// Raw GPT partition that receives the text log (read back from Android).
// Verified present on polaris' own GPT ("logdump", 65536 KB).
#define XNU_LOG_PARTITION  L"logdump"

#define XNU_MARKER_MAGIC  0x584E5521 // "XNU!"

VOID
XnuLoaderMain ();

#endif /* _XNU_LOADER_H_ */
