"""Minimal ISO9660/Joliet reader for in-place patching of the dcplaya image.
The image is a multisession-style selfboot ISO: extents are absolute LBAs
starting at BASE_LBA (11702), image sector 0 == LBA 11702."""
import struct

BASE_LBA = 11702
SS = 2048


class Rec:
    def __init__(self, img, off):
        self.off = off                       # absolute byte offset in image
        b = img[off:off + img[off]]
        self.raw = bytes(b)
        self.len = b[0]
        self.lba = struct.unpack('<I', b[2:6])[0]
        self.size = struct.unpack('<I', b[10:14])[0]
        self.flags = b[25]
        nl = b[32]
        self.ident = bytes(b[33:33 + nl])
        self.sysuse = bytes(b[33 + nl + (0 if nl % 2 else 1):])

    @property
    def is_dir(self):
        return bool(self.flags & 2)

    def name(self, joliet):
        if self.ident in (b'\x00', b'\x01'):
            return {b'\x00': '.', b'\x01': '..'}[self.ident]
        n = self.ident.decode('utf-16-be') if joliet else self.ident.decode('ascii')
        return n.split(';')[0]


def read_dir(img, lba, size):
    out = []
    start = (lba - BASE_LBA) * SS
    pos = start
    end = start + size
    while pos < end:
        l = img[pos]
        if l == 0:
            pos = (pos // SS + 1) * SS
            continue
        out.append(Rec(img, pos))
        pos += l
    return out


def root_of(img, vd_sector):
    vd = img[vd_sector * SS:(vd_sector + 1) * SS]
    r = Rec(vd, 156)
    return r.lba, r.size


def find_path(img, vd_sector, path, joliet):
    lba, size = root_of(img, vd_sector)
    rec = None
    for comp in path:
        recs = read_dir(img, lba, size)
        rec = next((r for r in recs if r.name(joliet).upper() == comp.upper()), None)
        if rec is None:
            raise FileNotFoundError('/'.join(path))
        lba, size = rec.lba, rec.size
    return rec
