"""Patch the original dcplaya CDI in place (same image size, same DiscJuggler
header) to add W5500 + DHCP + asynchronous stream checks:

  DCPLAYA/PLUGINS/EXE/NET/BBA/BBA.LEZ       new W5500 driver + DHCP client
  DCPLAYA/PLUGINS/EXE/NET/BBA/RTL8139.LEZ   NEW, points at the original BBA.LEZ
                                            sectors (real Broadband Adapter)
  DCPLAYA/PLUGINS/EXE/NET/NETPROBE.LEZ      NEW, async http stream probe (Lua)
  DCPLAYA/PLUGINS/EXE/NET/NET.LEZ           lwIP SYN retries 4 -> 2
  DCPLAYA/LUA/NET.LUA                       DHCP + loads netprobe.lez
  DCPLAYA/LUA/SONG_BROWSER.LUA              playlist checks streams in background
  DCPLAYA/LUA/KEYBOARD_EMU.LUA              Start pauses/resumes playback
  DCPLAYA/LUA/PLAYLIST.LUA                  #DCPLAYA-REMOTE: playlists from the proxy

New data goes into the ISO's free padding (150 zero sectors at the end of the
volume).  Both the ISO9660(+Rock Ridge) and the Joliet trees are updated.
Every modified sector gets its EDC/ECC recomputed.

usage: python patch_cdi.py original.cdi output.cdi [--out-dir out] [--lua-dir .]
"""
import argparse
import os
import struct

from cdi_tool import SECT_AUDIO, SECT_DATA, mode2_form1_2336, parse_layout
from iso_tool import BASE_LBA, SS, Rec, find_path, read_dir

AUDIO_SECTORS = 452          # track 1 (pregap+data)
DATA_PREGAP = 150
DATA_LEN = 5506
NET = ['DCPLAYA', 'PLUGINS', 'EXE', 'NET']


def both(v, fmt):
    return struct.pack('<' + fmt, v) + struct.pack('>' + fmt, v)


def set_extent(raw: bytearray, lba: int, size: int):
    raw[2:10] = both(lba, 'I')
    raw[10:18] = both(size, 'I')


def make_record(template: bytes, ident: bytes, sysuse: bytes, lba: int, size: int) -> bytes:
    body = bytearray(template[:33])
    body[32] = len(ident)
    body += ident
    if len(body) % 2:
        body += b'\x00'
    body += sysuse
    if len(body) % 2:
        body += b'\x00'
    body[0] = len(body)
    set_extent(body, lba, size)
    return bytes(body)


def rr_entries(sysuse: bytes):
    out, i = [], 0
    while i + 4 <= len(sysuse):
        l = sysuse[i + 2]
        if l == 0:
            break
        out.append(sysuse[i:i + l])
        i += l
    return out


class Iso:
    def __init__(self, img: bytearray):
        self.img = img
        self.space = struct.unpack('<I', img[16 * SS + 80:16 * SS + 84])[0]
        self.alloc = self._used_end()

    def _used_end(self):
        end = 0

        def walk(lba, size):
            nonlocal end
            for r in read_dir(self.img, lba, size):
                if r.ident in (b'\x00', b'\x01'):
                    continue
                end = max(end, r.lba - BASE_LBA + (r.size + SS - 1) // SS)
                if r.is_dir:
                    walk(r.lba, r.size)

        for vd in (16, 17):
            root = Rec(self.img[vd * SS:(vd + 1) * SS], 156)
            walk(root.lba, root.size)
        return end

    def store(self, data: bytes) -> int:
        nsec = (len(data) + SS - 1) // SS
        rel = self.alloc
        assert rel + nsec <= self.space, 'not enough padding space'
        assert self.img[rel * SS:(rel + nsec) * SS].count(0) == nsec * SS
        self.img[rel * SS:rel * SS + len(data)] = data
        self.alloc += nsec
        return BASE_LBA + rel

    def lookup(self, parts):
        p = find_path(self.img, 16, parts, False)
        j = find_path(self.img, 17, parts, True)
        assert p.lba == j.lba and p.size == j.size
        return p.lba, p.size

    def repoint(self, parts, lba, size):
        for r in (find_path(self.img, 16, parts, False), find_path(self.img, 17, parts, True)):
            raw = bytearray(r.raw)
            set_extent(raw, lba, size)
            self.img[r.off:r.off + r.len] = raw

    def add(self, dir_parts, name, template_name, lba, size):
        """Add NAME (8.3, upper case) to a one-sector directory in both trees,
        copying date/flags/Rock Ridge entries from TEMPLATE_NAME."""
        for vd, joliet in ((16, False), (17, True)):
            d = find_path(self.img, vd, dir_parts, joliet)
            assert d.size == SS, 'directory spans more than one sector'
            recs = read_dir(self.img, d.lba, d.size)
            names = [r.name(joliet).lower() for r in recs[2:]]
            assert name.lower() not in names, f'{name} already present'
            assert all(n < name.lower() for n in names), 'new name must sort last'
            tpl = next(r for r in recs if r.name(joliet).lower() == template_name.lower())
            if joliet:
                ident, sysuse = name.lower().encode('utf-16-be'), b''
            else:
                ident = name.upper().encode('ascii') + b';1'
                parts = []
                for e in rr_entries(tpl.sysuse):
                    if e[:2] == b'NM':
                        nm = name.lower().encode('ascii')
                        e = b'NM' + bytes([5 + len(nm), 1, 0]) + nm
                    parts.append(e)
                sysuse = b''.join(parts)
            new = make_record(tpl.raw, ident, sysuse, lba, size)
            last = recs[-1]
            ins = last.off + last.len
            assert ins + len(new) <= (d.lba - BASE_LBA + 1) * SS, 'no room in directory sector'
            self.img[ins:ins + len(new)] = new


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src')
    ap.add_argument('dst')
    ap.add_argument('--out-dir', default='out')
    ap.add_argument('--lua-dir', default='.')
    ap.add_argument('--add', action='append', default=[], metavar='DIR/NAME=FILE',
                    help='add a new file, e.g. EXTRA/TV.M3U=tv.m3u (repeatable, sorted order)')
    a = ap.parse_args()

    cdi = bytearray(open(a.src, 'rb').read())
    _, hdr_off = parse_layout(bytes(cdi))
    data_off = AUDIO_SECTORS * SECT_AUDIO
    assert hdr_off == data_off + (DATA_PREGAP + DATA_LEN) * SECT_DATA, 'unexpected CDI layout'

    def sector_off(s):
        return data_off + (DATA_PREGAP + s) * SECT_DATA

    img = bytearray(b''.join(bytes(cdi[sector_off(s) + 8:sector_off(s) + 8 + SS])
                             for s in range(DATA_LEN)))
    orig = bytes(img)
    iso = Iso(img)

    def rd(*p):
        return open(os.path.join(*p), 'rb').read()

    # BBA: new driver, original kept as RTL8139.LEZ
    old_lba, old_size = iso.lookup(NET + ['BBA', 'BBA.LEZ'])
    bba = rd(a.out_dir, 'BBA.LEZ')
    iso.repoint(NET + ['BBA', 'BBA.LEZ'], iso.store(bba), len(bba))
    iso.add(NET + ['BBA'], 'RTL8139.LEZ', 'BBA.LEZ', old_lba, old_size)

    # NET.LEZ with shorter connect timeout
    net = rd(a.out_dir, 'NET.LEZ')
    iso.repoint(NET + ['NET.LEZ'], iso.store(net), len(net))

    # NETPROBE.LEZ (new)
    probe = rd(a.out_dir, 'NETPROBE.LEZ')
    iso.add(NET, 'NETPROBE.LEZ', 'NET.LEZ', iso.store(probe), len(probe))

    # Lua
    for name in ('NET.LUA', 'SONG_BROWSER.LUA', 'KEYBOARD_EMU.LUA', 'PLAYLIST.LUA'):
        data = rd(a.lua_dir, name)
        iso.repoint(['DCPLAYA', 'LUA', name], iso.store(data), len(data))

    # Extra files (e.g. TV playlists); template = first file of the directory
    for spec in a.add:
        dest, src = spec.split('=', 1)
        parts = dest.strip('/').split('/')
        dparts, name = parts[:-1], parts[-1]
        d = find_path(img, 16, dparts, False)
        tpl = next(r.name(False) for r in read_dir(img, d.lba, d.size)[2:] if not r.is_dir)
        data = open(src, 'rb').read()
        iso.add(dparts, name, tpl, iso.store(data), len(data))
        print(f'added {dest} ({len(data)} bytes)')

    changed =[s for s in range(DATA_LEN) if img[s * SS:(s + 1) * SS] != orig[s * SS:(s + 1) * SS]]
    for s in changed:
        o = sector_off(s)
        cdi[o:o + SECT_DATA] = mode2_form1_2336(bytes(cdi[o:o + 8]), bytes(img[s * SS:(s + 1) * SS]))

    open(a.dst, 'wb').write(cdi)
    print(f'{len(changed)} ISO sectors changed, padding used up to sector '
          f'{iso.alloc} of {iso.space}')
    print(f'written {a.dst} ({len(cdi)} bytes, same size as original: '
          f'{len(cdi) == os.path.getsize(a.src)})')


if __name__ == '__main__':
    main()
