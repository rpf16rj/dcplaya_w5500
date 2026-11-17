"""Extract files from the data track of the dcplaya CDI.

usage: python extract_file.py image.cdi DCPLAYA/LUA/NET.LUA out_file [...more pairs]
"""
import sys

from cdi_tool import SECT_AUDIO, SECT_DATA
from iso_tool import BASE_LBA, SS, find_path

AUDIO_SECTORS = 452
DATA_PREGAP = 150
DATA_LEN = 5506


def load_iso(cdi_path):
    d = open(cdi_path, 'rb').read()
    off = AUDIO_SECTORS * SECT_AUDIO
    return b''.join(d[off + (DATA_PREGAP + s) * SECT_DATA + 8:
                      off + (DATA_PREGAP + s) * SECT_DATA + 8 + SS] for s in range(DATA_LEN))


def read_file(img, path):
    r = find_path(img, 16, path.split('/'), False)
    o = (r.lba - BASE_LBA) * SS
    return img[o:o + r.size]


if __name__ == '__main__':
    img = load_iso(sys.argv[1])
    args = sys.argv[2:]
    for i in range(0, len(args), 2):
        data = read_file(img, args[i])
        open(args[i + 1], 'wb').write(data)
        print(f'{args[i]} -> {args[i + 1]} ({len(data)} bytes)')
