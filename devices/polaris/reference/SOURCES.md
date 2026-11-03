# Where devices/polaris/reference/ comes from, and why it outranks this repository.
#
# None of these is a fork of this work. All three describe the same phone, they agree with
# each other, and two of them have been maintained for years -- the LineageOS tree still gets
# releases through lineage-23.2 (Android 16), so the board is well understood by people who
# were not us:
#
#   LineageOS/android_device_xiaomi_polaris        48 stars, branches lineage-15.1 .. lineage-23.2
#   LineageOS/android_device_xiaomi_sdm845-common  the SDM845 board config polaris inherits
#   TeamWin/android_device_xiaomi_polaris          16 stars, branch android-9.0
#   notsyncing/android_device_xiaomi_polaris       29 stars, the older TWRP tree (reference only)
#
# Where they agree with each other and disagree with this repository, this repository is
# wrong until a phone proves otherwise. That is how the loader's ufs_references table and the
# boot image page size were found to be another board's numbers.
#
# Files here are verbatim copies, kept so tools/check-board-facts.py runs offline.
# Refresh them with tools/fetch-reference-trees.sh (it takes the branches as overrides).
#
# polaris-partition-sizes.txt is the one file that is not fetched: its numbers are the sizes
# of the .img files in Xiaomi's own fastboot package, which no git repository has. They are a
# property of the package, not of a branch.

lineage_polaris_branch   lineage-22.2
lineage_sdm845_branch    lineage-22.2
twrp_repo                TeamWin/android_device_xiaomi_polaris
twrp_branch              android-9.0

# Checked-in files and which tree each one is a copy of:
#
#   BoardConfigCommon.mk       LineageOS/android_device_xiaomi_sdm845-common @ lineage-22.2
#   fstab.qcom                 LineageOS/android_device_xiaomi_polaris @ lineage-22.2 (init/fstab.qcom)
#   polaris-BoardConfig.mk     TeamWin/android_device_xiaomi_polaris @ android-9.0
#   polaris-partition-sizes.txt  sizes of the .img files in
#                              polaris_images_V12.5.1.0.QDGCNXM_20210707.0000.00_10.0_cn
