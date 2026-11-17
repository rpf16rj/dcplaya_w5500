"""Patch NET.LEZ (dcplaya 2.0b2 network plugin): lwIP TCP_SYNMAXRTX 4 -> 2.

lwIP's tcp_slowtmr() drops a connection in SYN_SENT after TCP_SYNMAXRTX
retransmissions.  With 4 (and RTO 3s doubling) a connect() to a dead server
blocks ~45 s; with 2 it gives up after ~9 s.  Established connections are not
affected (TCP_MAXRTX stays 12).

The instruction is `cmp/eq #4,r0` (bytes 04 88) at .text+0xea04 inside
_tcp_slowtmr, right after `mov.b @(pcb->nrtx),r0` in the SYN_SENT branch.

usage: python patch_netlez.py NET.LEZ.orig NET.LEZ
"""
import gzip
import struct
import sys

TEXT_OFF_IN_FUNC = 0xEA04
NEW_SYNMAXRTX = 2


def text_file_offset(elf: bytes) -> int:
    shoff = struct.unpack_from('<I', elf, 0x20)[0]
    shentsize, shnum, shstrndx = struct.unpack_from('<HHH', elf, 0x2E)
    strtab_off = struct.unpack_from('<I', elf, shoff + shstrndx * shentsize + 16)[0]
    for i in range(shnum):
        base = shoff + i * shentsize
        name_off = struct.unpack_from('<I', elf, base)[0]
        name = elf[strtab_off + name_off:elf.index(b'\0', strtab_off + name_off)]
        if name == b'.text':
            return struct.unpack_from('<I', elf, base + 16)[0]
    raise ValueError('.text not found')


def main(src, dst):
    elf = bytearray(gzip.decompress(open(src, 'rb').read()))
    o = text_file_offset(bytes(elf)) + TEXT_OFF_IN_FUNC
    # context check: add #66,r3 / mov.b @r3,r0 / cmp/eq #4,r0 / bt.s
    assert elf[o - 4:o + 4] == bytes.fromhex('4273306004880' + '28d'), elf[o - 4:o + 4].hex()
    elf[o] = NEW_SYNMAXRTX
    open(dst, 'wb').write(gzip.compress(bytes(elf), compresslevel=9, mtime=0))
    print(f'patched TCP_SYNMAXRTX at file offset {o:#x}: 4 -> {NEW_SYNMAXRTX}')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
