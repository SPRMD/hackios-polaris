##
#  Copyright (c) 2011 - 2022, ARM Limited. All rights reserved.
#  Copyright (c) 2014, Linaro Limited. All rights reserved.
#  Copyright (c) 2015 - 2020, Intel Corporation. All rights reserved.
#  Copyright (c) 2018, Bingxing Wang. All rights reserved.
#  Copyright (c) Microsoft Corporation.
#
#  SPDX-License-Identifier: BSD-2-Clause-Patent
##

################################################################################
#
# Defines Section - statements that will be processed to create a Makefile.
#
################################################################################
[Defines]
  PLATFORM_NAME                  = polaris
  PLATFORM_GUID                  = 659EDC0F-E61F-47CA-910B-869BDA57A64D
  PLATFORM_VERSION               = 0.1
  DSC_SPECIFICATION              = 0x00010005
  OUTPUT_DIRECTORY               = Build/polarisPkg
  SUPPORTED_ARCHITECTURES        = AARCH64
  BUILD_TARGETS                  = RELEASE|DEBUG
  SKUID_IDENTIFIER               = DEFAULT
  FLASH_DEFINITION               = polarisPkg/polaris.fdf

  #
  # upstream ships 1 (Qualcomm DisplayDxe). hackios uses 0 (SiliciumPkg SimpleFbDxe):
  # Device-Binaries has no panel XML for polaris, so the custom driver has no panel to
  # load, and hackios only needs the "Display Reserved" mapping to already be on screen.
  #
  USE_CUSTOM_DISPLAY_DRIVER      = 0

!include NapaliPkg/NapaliPkg.dsc.inc

[PcdsFixedAtBuild]
  #
  # DDR Memory
  #
  gArmTokenSpaceGuid.PcdSystemMemoryBase|0x80000000

  #
  # UEFI Stack
  #
  gArmPlatformTokenSpaceGuid.PcdCPUCoresStackBase|0x9FF90000
  gArmPlatformTokenSpaceGuid.PcdCPUCorePrimaryStackSize|0x40000

  #
  # SMBIOS
  #
  gSiliciumPkgTokenSpaceGuid.PcdSmbiosSystemManufacturer|"Xiaomi"
  gSiliciumPkgTokenSpaceGuid.PcdSmbiosSystemModel|"Mi Mix 2S"
  gSiliciumPkgTokenSpaceGuid.PcdSmbiosSystemRetailModel|"polaris"
  gSiliciumPkgTokenSpaceGuid.PcdSmbiosSystemRetailSku|"Mi_Mix_2S_polaris"
  gSiliciumPkgTokenSpaceGuid.PcdSmbiosSystemBoardModel|"Mi Mix 2S"

  #
  # Simple Frame Buffer
  #
  gSiliciumPkgTokenSpaceGuid.PcdFrameBufferWidth|1080
  gSiliciumPkgTokenSpaceGuid.PcdFrameBufferHeight|2160
  gSiliciumPkgTokenSpaceGuid.PcdFrameBufferColorDepth|32

  #
  # Platform PEI
  #
  gQcomPkgTokenSpaceGuid.PcdPlatformType|"LA"

[LibraryClasses]
  #
  # Memory Libraries
  #
  MemoryMapLib|polarisPkg/Library/MemoryMapLib/MemoryMapLib.inf

  #
  # QCOM Libraries
  #
  ConfigurationMapLib|polarisPkg/Library/ConfigurationMapLib/ConfigurationMapLib.inf

[Components]
  #
  # ACPI Tables
  #
  # Dropped from upstream. polaris/AcpiTables.inf is four prebuilt .aml blobs in the
  # Silicium-ACPI submodule (SSDT/APIC/DSDT_Minimal/FACP/GTDT) describing an SDM845 that
  # boots Windows; XNU gets its hardware description from the device tree in the payload
  # instead, and a stale MADT/GTDT would only be something for the kernel to disagree with.
  # The two ACPI DXE drivers stay in polaris.fdf (raphael keeps them too) so a driver can still
  # publish a table at runtime; only the prebuilt table FILE is dropped.
  # To put it back: re-add `polaris/AcpiTables.inf` here and uncomment the matching
  # RuleOverride line in polaris.fdf.

[BuildOptions]
  #
  # The UEFI's own BDS does not boot anything: it hands over to devices/../XnuLoader, which
  # reads the "XNUP" container the boot shim left at PAYLOAD_BASE. Same flags as raphael.
  #
  *_CLANGPDB_AARCH64_CC_FLAGS = -DXNU_NO_DISK_LOG -DXNU_PAYLOAD_BOOT
