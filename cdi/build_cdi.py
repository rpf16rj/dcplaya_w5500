"""Build a patched dcplaya CDI that plays IPTV and YouTube through your dc-tv-proxy.

    python build_cdi.py ORIGINAL.cdi OUTPUT.cdi --proxy HOST[:PORT] --token TOKEN

The original image must be the dcplaya 2.0 beta 2 "Scrivani VideoCast" CDI
(see README).  The output has the same size and layout; only these files
change or are added:

  PLUGINS/EXE/NET/BBA/BBA.LEZ       W5500 driver + DHCP (falls back to the real BBA)
  PLUGINS/EXE/NET/BBA/RTL8139.LEZ   original BBA driver (new name)
  PLUGINS/EXE/NET/NETPROBE.LEZ      background link check for playlists
  PLUGINS/EXE/NET/NET.LEZ           shorter TCP connect timeout
  LUA/NET.LUA                       DHCP by default
  LUA/SONG_BROWSER.LUA              playlists skip dead links without freezing; B stops
  LUA/KEYBOARD_EMU.LUA              Start pauses/resumes
  LUA/PLAYLIST.LUA                  "#DCPLAYA-REMOTE:" playlists fetched from the proxy
  EXTRA/TV_PROXY.M3U                your TV channels, fetched from the proxy when opened
  EXTRA/YOUTUBE.M3U                 latest videos of your YouTube channels, idem

The two playlists hold no channels: every time they are opened the Dreamcast
downloads the current list from the proxy, so channels edited on the proxy
(or in its /admin page) show up without burning a new disc.

--proxy may be repeated: the addresses are tried in order, e.g. the LAN
address first and an internet hostname second:
  --proxy 192.168.1.20:8765 --proxy tv.example.com
"""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from extract_file import load_iso, read_file  # noqa: E402

KNOWN_MD5 = 'eb75eeb78184c54b9f6d2bde7657b15a'   # DCPlaya Scrivani VideoCast_192_168_1_1.cdi
ORIGINALS = {
    'NET.LUA.orig': 'DCPLAYA/LUA/NET.LUA',
    'SONG_BROWSER.LUA.orig': 'DCPLAYA/LUA/SONG_BROWSER.LUA',
    'KEYBOARD_EMU.LUA.orig': 'DCPLAYA/LUA/KEYBOARD_EMU.LUA',
    'PLAYLIST.LUA.orig': 'DCPLAYA/LUA/PLAYLIST.LUA',
    'NET.LEZ.orig': 'DCPLAYA/PLUGINS/EXE/NET/NET.LEZ',
}
HOST_RE = re.compile(r'^[A-Za-z0-9.-]+(:\d{1,5})?$')
TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]+$')
# CD name -> proxy path (new names must sort after the disc's EXTRA/RADIO.M3U)
PLAYLISTS = {'TV_PROXY.M3U': 'tv.m3u', 'YOUTUBE.M3U': 'yt/all.m3u'}


def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def remote_playlist(hosts, token, path):
    """Stub read by the patched PLAYLIST.LUA: try each address in order."""
    lines = [f'#DCPLAYA-REMOTE:/http/{h}/{token}/{path}?host={h}' for h in hosts]
    return '\n'.join(lines + ['#EXTM3U']) + '\n'


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('original', help='original dcplaya CDI')
    ap.add_argument('output', help='CDI to write')
    ap.add_argument('--proxy', action='append', required=True, metavar='HOST[:PORT]',
                    help='address the Dreamcast uses to reach the proxy (repeatable, tried in order)')
    ap.add_argument('--token', required=True, help='TOKEN from the proxy .env')
    ap.add_argument('--no-youtube', action='store_true', help='leave out YOUTUBE.M3U')
    ap.add_argument('--keep-work', action='store_true', help='keep the temporary work folder')
    a = ap.parse_args()

    for h in a.proxy:
        if not HOST_RE.match(h):
            sys.exit(f'bad --proxy host: {h!r} (use HOST or HOST:PORT, no http://)')
    if not TOKEN_RE.match(a.token) or a.token == 'change-me':
        sys.exit('set a real --token (letters, digits, - and _ only)')
    if os.path.abspath(a.original) == os.path.abspath(a.output):
        sys.exit('output must be a different file')

    digest = md5(a.original)
    if digest != KNOWN_MD5:
        print(f'WARNING: {a.original} md5 {digest} is not the tested image '
              f'({KNOWN_MD5}); continuing, the layout check will stop if it does not fit.')

    work = tempfile.mkdtemp(prefix='dcplaya_build_')
    try:
        # 1. original files from the CD
        img = load_iso(a.original)
        for local, path in ORIGINALS.items():
            with open(os.path.join(work, local), 'wb') as f:
                f.write(read_file(img, path))
        shutil.copy(os.path.join(HERE, 'net_autoconnect.lua'), work)

        # 2. patched Lua (make_lua.py works on the current directory)
        subprocess.run([sys.executable, os.path.join(HERE, 'make_lua.py')], cwd=work, check=True)

        # 3. plugins: prebuilt W5500 driver + probe, patched NET.LEZ
        out = os.path.join(work, 'out')
        os.makedirs(out)
        for name in ('BBA.LEZ', 'NETPROBE.LEZ'):
            shutil.copy(os.path.join(HERE, 'prebuilt', name), out)
        subprocess.run([sys.executable, os.path.join(HERE, 'patch_netlez.py'),
                        os.path.join(work, 'NET.LEZ.orig'), os.path.join(out, 'NET.LEZ')], check=True)

        # 4. remote playlists
        adds = []
        for name, path in sorted(PLAYLISTS.items()):
            if a.no_youtube and name == 'YOUTUBE.M3U':
                continue
            local = os.path.join(work, name)
            with open(local, 'w', encoding='ascii', newline='\n') as f:
                f.write(remote_playlist(a.proxy, a.token, path))
            adds += ['--add', f'EXTRA/{name}={local}']

        # 5. the image
        subprocess.run([sys.executable, os.path.join(HERE, 'patch_cdi.py'), a.original, a.output,
                        '--out-dir', out, '--lua-dir', work] + adds, check=True)
    finally:
        if a.keep_work:
            print(f'work folder kept: {work}')
        else:
            shutil.rmtree(work, ignore_errors=True)

    names = [n.lower() for n in sorted(PLAYLISTS) if not (a.no_youtube and n == 'YOUTUBE.M3U')]
    print(f'\ndone: {a.output}')
    print('Burn it (DiscJuggler/ImgBurn) or load it in an ODE, then on the Dreamcast open '
          '/cd/extra/ and play ' + ' or '.join(names) + '.')


if __name__ == '__main__':
    main()
