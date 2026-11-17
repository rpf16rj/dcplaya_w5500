"""Helpers for the dcplaya selfboot CDI (DiscJuggler 3.5, audio+data, 2 sessions).

Layout of the original image (verified by parsing its header):
  session 1 / track 1: audio, 150 pregap + 302 sectors, 2352 bytes/sector
  session 2 / track 2: mode 2, 150 pregap + 5506 sectors, 2336 bytes/sector,
                       start LBA 11702 (msinfo 0,11702)
Track data is stored back to back from offset 0; the header sits at the end.
"""
import struct

SECT_AUDIO = 2352
SECT_DATA = 2336
DATA_LBA = 11702


def parse_layout(cdi: bytes):
    hdr_size = struct.unpack('<I', cdi[-4:])[0]
    ver = struct.unpack('<I', cdi[-8:-4])[0]
    hdr_off = len(cdi) - hdr_size
    return ver, hdr_off


# ---------------------------------------------------------------- EDC / ECC
def _build_tables():
    ecc_f = [0] * 256
    ecc_b = [0] * 256
    edc = [0] * 256
    for i in range(256):
        j = (i << 1) ^ (0x11D if i & 0x80 else 0)
        ecc_f[i] = j & 0xFF
        ecc_b[i ^ j & 0xFF] = i
        e = i
        for _ in range(8):
            e = (e >> 1) ^ (0xD8018001 if e & 1 else 0)
        edc[i] = e
    return ecc_f, ecc_b, edc


ECC_F, ECC_B, EDC_T = _build_tables()


def edc_compute(data: bytes) -> int:
    e = 0
    for b in data:
        e = (e >> 8) ^ EDC_T[(e ^ b) & 0xFF]
    return e


def _ecc_pq(sector: bytearray, major_count, minor_count, major_mult, minor_inc, out_off):
    # sector is the 2352-byte raw sector; ECC computed over bytes 12..
    size = major_count * minor_count
    for major in range(major_count):
        index = (major >> 1) * major_mult + (major & 1)
        ecc_a = 0
        ecc_b = 0
        for _ in range(minor_count):
            temp = sector[12 + index]
            index += minor_inc
            if index >= size:
                index -= size
            ecc_a ^= temp
            ecc_b ^= temp
            ecc_a = ECC_F[ecc_a]
        ecc_a = ECC_B[ECC_F[ecc_a] ^ ecc_b]
        sector[out_off + major] = ecc_a
        sector[out_off + major + major_count] = ecc_a ^ ecc_b


def mode2_form1_2336(subheader: bytes, user: bytes) -> bytes:
    """Build a 2336-byte Mode 2 Form 1 sector (subheader+data+EDC+ECC).
    ECC for mode 2 is computed with the 4 header bytes treated as zero."""
    assert len(subheader) == 8 and len(user) == 2048
    raw = bytearray(2352)
    # header (12..15) left zero for ECC computation
    raw[16:24] = subheader
    raw[24:2072] = user
    edc = edc_compute(bytes(raw[16:2072]))
    raw[2072:2076] = struct.pack('<I', edc)
    _ecc_pq(raw, 86, 24, 2, 86, 2076)   # P
    _ecc_pq(raw, 52, 43, 86, 88, 2076 + 172)  # Q
    return bytes(raw[16:2352])
