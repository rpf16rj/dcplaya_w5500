#!/bin/bash
# Build BBA.LEZ (W5500 driver for dcplaya 2.0b2) - run inside the KOS WSL distro.
set -euo pipefail
cd "$(dirname "$0")"
export PATH=/opt/toolchains/dc/sh-elf/bin:$PATH

CFLAGS="-O2 -ml -m4-single-only -ffreestanding -fno-builtin -fno-common \
 -fno-pic -fno-pie -fno-function-sections -fno-data-sections \
 -fno-reorder-blocks-and-partition -fno-tree-loop-distribute-patterns \
 -fno-asynchronous-unwind-tables -fno-unwind-tables -fno-stack-protector \
 -Wall -Wextra -Wno-unused-parameter"

mkdir -p out
sh-elf-gcc $CFLAGS -c w5500_bba.c -o out/w5500_bba.o
sh-elf-gcc $CFLAGS -c dhcp.c -o out/dhcp.o
# Same recipe as dcplaya's Makefile.inc (%.lef rule)
sh-elf-ld -b elf32-shl -EL -r -x -S out/w5500_bba.o out/dhcp.o -o out/bba.lef
sh-elf-strip --strip-unneeded \
  -R .comment -R .note.GNU-stack -R .eh_frame out/bba.lef
gzip --best -n -c out/bba.lef > out/BBA.LEZ
sh-elf-readelf -S out/bba.lef | grep -E '^\s+\['
echo "--- relocation types:"
sh-elf-readelf -r out/bba.lef | awk '/R_SH_/{print $3}' | sort | uniq -c
echo "--- undefined symbols:"
sh-elf-nm -u out/bba.lef | awk '{print $2}' | tr '\n' ' '; echo
echo "--- exported:"
sh-elf-nm -g --defined-only out/bba.lef
ls -l out/BBA.LEZ

# --- netprobe.lez (async http stream probe, Lua plugin)
sh-elf-gcc $CFLAGS -c netprobe.c -o out/netprobe.o
sh-elf-ld -b elf32-shl -EL -r -x -S out/netprobe.o -o out/netprobe.lef
sh-elf-strip --strip-unneeded -R .comment -R .note.GNU-stack -R .eh_frame out/netprobe.lef
gzip --best -n -c out/netprobe.lef > out/NETPROBE.LEZ
echo "--- netprobe relocs:"; sh-elf-readelf -r out/netprobe.lef | awk '/R_SH_/{print $3}' | sort | uniq -c
echo "--- netprobe undefined:"; sh-elf-nm -u out/netprobe.lef | awk '{print $2}' | tr '\n' ' '; echo
sh-elf-nm -g --defined-only out/netprobe.lef
ls -l out/NETPROBE.LEZ
