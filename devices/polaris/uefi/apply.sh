#!/bin/bash
# Make ../Mu-Silicium: a clone of Project-Silicium/Mu-Silicium at the commit our port was made on,
# with the polaris port (this directory) put into it. See README.md here for how to build it.
set -e
cd "$(dirname "$(readlink -f "$0")")"
MU=../../../Mu-Silicium
[ -e $MU ] && { echo "uefi/apply.sh: $MU already exists"; exit 1; }
git clone https://github.com/Project-Silicium/Mu-Silicium.git $MU
git -C $MU checkout -q "$(cat BASE)"
git -C $MU submodule update --init --recursive
git -C $MU apply --whitespace=nowarn "$PWD/mu-silicium.patch"
# files/ overwrites the platform package upstream already ships for polaris at this commit.
# Upstream's copy builds as it stands; ours drops the ACPI set, adds the "XNU Payload"
# reservation and switches the display driver. See README.md, "What differs from upstream".
cp -r files/. $MU/
echo "uefi/apply.sh: done; next: devices/polaris/uefi/README.md"
