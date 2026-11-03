#!/bin/sh
# Refresh devices/polaris/reference/ from the three device trees that describe this phone.
#
# check-board-facts.py reads checked-in copies so it runs offline; this is the way to bring
# them up to date. Nothing here is a fork of this work, and no file in this repository was
# used to produce them -- that is the point of keeping them.
#
#   LineageOS/android_device_xiaomi_polaris        48 stars, lineage-15.1 .. lineage-23.2
#   LineageOS/android_device_xiaomi_sdm845-common  the SDM845 board config polaris inherits
#   TeamWin/android_device_xiaomi_polaris          16 stars, branch android-9.0
#   notsyncing/android_device_xiaomi_polaris       29 stars, the older TWRP tree
#
# The LineageOS trees are still maintained (through Android 16), so the branch names move.
# Override them rather than editing this file.
set -eu

LIN_POL_BRANCH=${LIN_POL_BRANCH:-lineage-22.2}
LIN_845_BRANCH=${LIN_845_BRANCH:-lineage-22.2}
TWRP_BRANCH=${TWRP_BRANCH:-android-9.0}

RAW=https://raw.githubusercontent.com
REF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)/devices/polaris/reference

mkdir -p "$REF_DIR"

fetch() { # url dest
	[ -n "${1:-}" ] || return 0
	echo "  $2"
	curl -fsSL --retry 3 --max-time 60 "$1" -o "$REF_DIR/$2"
}

echo "sdm845-common @ $LIN_845_BRANCH"
fetch "$RAW/LineageOS/android_device_xiaomi_sdm845-common/$LIN_845_BRANCH/BoardConfigCommon.mk" \
	BoardConfigCommon.mk

echo "polaris @ $LIN_POL_BRANCH"
fetch "$RAW/LineageOS/android_device_xiaomi_polaris/$LIN_POL_BRANCH/BoardConfig.mk" \
	polaris-BoardConfig-lineage.mk
fetch "$RAW/LineageOS/android_device_xiaomi_polaris/$LIN_POL_BRANCH/init/fstab.qcom" \
	fstab.qcom

echo "polaris @ $TWRP_BRANCH (TWRP)"
fetch "$RAW/TeamWin/android_device_xiaomi_polaris/$TWRP_BRANCH/BoardConfig.mk" \
	polaris-BoardConfig.mk

# polaris-partition-sizes.txt is not fetchable: the numbers are the sizes of the .img files
# inside Xiaomi's own fastboot package. Derive them from the package with
#   ls -l polaris_images_*/images/*.img
# and keep the header in the file.
echo
echo "checked in files now:"
ls -l "$REF_DIR"
