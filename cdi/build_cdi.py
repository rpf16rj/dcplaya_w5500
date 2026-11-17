"""Build a patched dcplaya CDI that plays IPTV through your dc-tv-proxy.

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
  EXTRA/TV.M3U (+ more)             channel playlist(s) pointing at your proxy

Where the channel list comes from (first that applies):
  --playlist FILE   a ready dcplaya playlist (one per --proxy, same order)
  --channels FILE   your IPTV m3u, converted locally (same order as on the proxy!)
  (default)         downloaded from the running proxy: http://HOST/TOKEN/tv.m3u

--proxy may be repeated, e.g. a LAN address and an internet hostname:
  --proxy 192.168.1.20:8765 --proxy tv.example.com
gives EXTRA/TV.M3U and EXTRA/TV2.M3U.  Add =NAME to choose the file name:
  --proxy 192.168.1.20:8765=TV_LAN.M3U
"""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from extract_file import load_iso, read_file  # noqa: E402

KNOWN_MD5 = 'eb75eeb78184c54b9f6d2bde7657b15a'   # DCPlaya Scrivani VideoCast_192_168_1_1.cdi
ORIGINALS = {
    'NET.LUA.orig': 'DCPLAYA/LUA/NET.LUA',
    'SONG_BROWSER.LUA.orig': 'DCPLAYA/LUA/SONG_BROWSER.LUA',
    'KEYBOARD_EMU.LUA.orig': 'DCPLAYA/LUA/KEYBOARD_EMU.LUA',
    'NET.LEZ.orig': 'DCPLAYA/PLUGINS/EXE/NET/NET.LEZ',
}
HOST_RE = re.compile(r'^[A-Za-z0-9.-]+(:\d{1,5})?$')
NAME_RE = re.compile(r'^[A-Z0-9_]{1,8}\.M3U$')
TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]+$')


def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def parse_proxies(specs):
    out = []
    named = {s.partition('=')[2].upper() for s in specs}
    free = (n for n in ['TV.M3U'] + [f'TV{k}.M3U' for k in range(2, 10)] if n not in named)
    for spec in specs:
        host, _, name = spec.partition('=')
        name = (name or next(free)).upper()
        if not HOST_RE.match(host):
            sys.exit(f'bad --proxy host: {host!r} (use HOST or HOST:PORT, no http://)')
        if not NAME_RE.match(name):
            sys.exit(f'bad playlist name: {name!r} (8.3 upper case, e.g. TV_LAN.M3U)')
        if name.lower() <= 'radio.m3u':
            # new directory entries are appended, so they must sort after the
            # disc's own EXTRA/RADIO.M3U
            sys.exit(f'bad playlist name: {name!r} must sort after RADIO.M3U (start with S..Z, e.g. TV_LAN.M3U)')
        out.append((host, name))
    if len({n for _, n in out}) != len(out):
        sys.exit('playlist names must be different')
    return out


def check_playlist(text, where):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    entries = [ln for ln in lines if ln.startswith('/http/')]
    if not entries or not any(ln.startswith('#EXTINF:') for ln in lines):
        sys.exit(f'{where}: does not look like a dcplaya playlist '
                 '(expected #EXTINF lines and /http/... entries)')
    try:
        text.encode('ascii')
    except UnicodeEncodeError:
        sys.exit(f'{where}: playlist must be plain ASCII')
    return len(entries)


def fetch_playlist(host, token):
    url = f'http://{host}/{token}/tv.m3u?host={host}'
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return r.read().decode('ascii')
    except Exception as e:  # noqa: BLE001 - report any network/HTTP failure
        sys.exit(f'could not download the playlist from {url.replace(token, "<TOKEN>")}: {e}\n'
                 'Is the proxy running and reachable from this PC? '
                 'Otherwise use --channels or --playlist.')


def local_playlist(host, token, channels):
    sys.path.insert(0, os.path.join(HERE, '..', 'proxy'))
    import server   # noqa: E402  (the proxy's own playlist generator)
    chans = server.parse_m3u(channels)
    if not chans:
        sys.exit(f'{channels}: no channels found')
    return server.dc_playlist(host, token, chans)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('original', help='original dcplaya CDI')
    ap.add_argument('output', help='CDI to write')
    ap.add_argument('--proxy', action='append', required=True, metavar='HOST[:PORT][=NAME.M3U]',
                    help='address the Dreamcast uses to reach the proxy (repeatable)')
    ap.add_argument('--token', required=True, help='TOKEN from the proxy .env')
    src = ap.add_mutually_exclusive_group()
    src.add_argument('--channels', metavar='IPTV.m3u', help='build the playlist from this IPTV list')
    src.add_argument('--playlist', action='append', metavar='DC.m3u',
                     help='use a ready dcplaya playlist (one per --proxy)')
    ap.add_argument('--keep-work', action='store_true', help='keep the temporary work folder')
    a = ap.parse_args()

    proxies = parse_proxies(a.proxy)
    if not TOKEN_RE.match(a.token) or a.token == 'change-me':
        sys.exit('set a real --token (letters, digits, - and _ only)')
    if a.playlist and len(a.playlist) != len(proxies):
        sys.exit('give one --playlist per --proxy')
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

        # 4. playlists
        adds = []
        for i, (host, name) in enumerate(proxies):
            if a.playlist:
                text = open(a.playlist[i], encoding='ascii', errors='strict').read()
                where = a.playlist[i]
            elif a.channels:
                text, where = local_playlist(host, a.token, a.channels), a.channels
            else:
                text, where = fetch_playlist(host, a.token), f'proxy {host}'
            n = check_playlist(text, where)
            path = os.path.join(work, name)
            with open(path, 'w', encoding='ascii', newline='\n') as f:
                f.write(text)
            adds.append((name.lower(), ['--add', f'EXTRA/{name}={path}']))
            print(f'{name}: {n} channels via {host}')
        adds = [arg for _, pair in sorted(adds) for arg in pair]   # ISO order

        # 5. the image
        subprocess.run([sys.executable, os.path.join(HERE, 'patch_cdi.py'), a.original, a.output,
                        '--out-dir', out, '--lua-dir', work] + adds, check=True)
    finally:
        if a.keep_work:
            print(f'work folder kept: {work}')
        else:
            shutil.rmtree(work, ignore_errors=True)

    print(f'\ndone: {a.output}')
    print('Burn it (DiscJuggler/ImgBurn) or load it in an ODE, then on the Dreamcast open '
          '/cd/extra/ and play ' + ', '.join(n.lower() for _, n in proxies) + '.')


if __name__ == '__main__':
    main()
