"""dcplaya TV proxy: modern ffmpeg on the server, MPEG-1 for the Dreamcast.

The Dreamcast cannot decode today's HD/FHD H.264/H.265 IPTV streams, so this
server pulls each channel and transcodes it live into what dcplaya 2.0b2's
(2004) ffmpeg plugin plays on a real console: MPEG-1 video 208x116 (16:9) @ 15 fps +
MP3 44.1 kHz stereo in an MPEG-1 program stream, served over plain HTTP.

Routes (TOKEN keeps your IPTV account from being usable by anybody):
  GET /<TOKEN>/ch/<n>.mpg   transcoded channel n (1-based, order of the list)
  GET /<TOKEN>/tv.m3u       dcplaya playlist (?host=dc.example.com[:port])
  GET /health               "ok"

Requests whose User-Agent is exactly "dcplaya" come from the patched
playlist's background link check (netprobe.lez): they only test the upstream
URL and answer 200/502 without starting ffmpeg.

dcplaya opens every stream twice and never reads the first connection again;
when the second one arrives the first is reset, otherwise its unread data
fills the console's tiny lwIP buffers and starves playback.

Configuration (environment; the defaults are the profile that played best on
a real Dreamcast, see tune.env):
  TOKEN          required, path secret
  CHANNELS       IPTV m3u                        (default /data/channels.m3u)
  PORT           listen port                     (default 8765)
  WIDTH/HEIGHT   output size                     (default 208x116, 16:9 without black bars)
  FPS            output frame rate               (default 15)
  VBITRATE       video bitrate                   (default 150k)
  GOP            frames between full frames      (default 30)
  ACODEC         libmp3lame or mp2               (default libmp3lame; mp2 sounds broken on the DC)
  ABITRATE       audio bitrate                   (default 128k)
  ACHANNELS      1 = mono, 2 = stereo            (default 2)
  ASRATE         audio sample rate               (default 44100; other rates break A/V sync)
  MUXRATE        MPEG-PS mux rate                (default 600k)
  PACE           send rate = PACE x MUXRATE      (default 1.25, 0 = off)
  MAX_STREAMS    simultaneous transcodes         (default 3)
  STALL_TIMEOUT  seconds without progress before a client is dropped (default 300)
  UPSTREAM_UA    User-Agent sent to the IPTV     (default VLC/3.0.20 LibVLC/3.0.20)
  Advanced: VCODEC, VBUFSIZE, VENC_EXTRA, VDENOISE, VDEINT, AFILTER, AUDIO, VIDEO

CLI:  python server.py --playlist HOST[:PORT] TOKEN [channels.m3u]
      prints the dcplaya playlist (used to put it on the CD image).
"""
import base64
import hmac
import http.server
import json
import os
import re
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.request

TOKEN = os.environ.get('TOKEN', '')
CHANNELS = os.environ.get('CHANNELS', '/data/channels.m3u')
PORT = int(os.environ.get('PORT', '8765'))
WIDTH = int(os.environ.get('WIDTH', '208'))
HEIGHT = int(os.environ.get('HEIGHT', '116'))
FPS = os.environ.get('FPS', '15')
VBITRATE = os.environ.get('VBITRATE', '150k')
ABITRATE = os.environ.get('ABITRATE', '128k')
ACHANNELS = os.environ.get('ACHANNELS', '2')
MUXRATE = os.environ.get('MUXRATE', '600k')
GOP = os.environ.get('GOP', '30')                # frames between I-frames (longer = cheaper to decode)
AFILTER = os.environ.get('AFILTER', 'loudnorm=I=-14:TP=-1.5:LRA=11')  # same loudness for every channel, close to web radios
ACODEC = os.environ.get('ACODEC', 'libmp3lame')        # mp2 or libmp3lame
ASRATE = os.environ.get('ASRATE', '44100')       # keep 44100: other rates break dcplaya's A/V sync
VBUFSIZE = os.environ.get('VBUFSIZE', '1000k')     # rate-control buffer (smaller = steadier quality)
# Encoder tweaks: only change these after testing on a real console.
VENC_EXTRA = os.environ.get('VENC_EXTRA', '-qmin 2')  # RD options measured better but looked worse on the DC
VSCALER = os.environ.get('VSCALER', 'area')    # area averages pixels: no aliasing on 10x downscales (lanczos made blocks)
VDENOISE = os.environ.get('VDENOISE', 'gblur=sigma=0.5,')  # after scaling: drops detail 192x144 can't show (blocky frames 107 -> 4 of 451)
VDEINT = os.environ.get('VDEINT', '')  # e.g. bwdif=mode=send_frame:deint=interlaced, (looked worse on the DC)
VCODEC = os.environ.get('VCODEC', 'mpeg1video')  # mpeg1video (PS), mpeg2video (VOB), mpeg4 (DivX AVI)
VFORMAT = os.environ.get('VFORMAT', 'auto')     # auto (by codec) or mpegts
AUDIO = os.environ.get('AUDIO', '1') != '0'
VIDEO = os.environ.get('VIDEO', '1') != '0'   # VIDEO=0: audio-only stream
PACE = float(os.environ.get('PACE', '1.25'))   # output rate = PACE x MUXRATE (0 = no pacing)
PACE_BURST = int(os.environ.get('PACE_BURST', str(8 * 1024)))  # bytes sent unpaced at start
STALL_TIMEOUT = float(os.environ.get('STALL_TIMEOUT', '300'))  # s without progress -> drop client
SEG = 1460                                     # pace per TCP segment (no bursts)
SNDBUF = int(os.environ.get('SNDBUF', str(16 * 1024)))  # keep the kernel queue short


def rate_bps(v):
    v = v.strip().lower()
    mult = 1000 if v.endswith('k') else 1000000 if v.endswith('m') else 1
    return float(v.rstrip('km')) * mult
MAX_STREAMS = int(os.environ.get('MAX_STREAMS', '3'))
UPSTREAM_UA = os.environ.get('UPSTREAM_UA', 'VLC/3.0.20 LibVLC/3.0.20')
# YouTube (needs yt-dlp + deno in the image); 18 = small mp4 with audio
YT_FORMAT = os.environ.get('YT_FORMAT', '18/best[height<=360][vcodec!=none][acodec!=none]/worst')
YT_LIVE_FORMAT = os.environ.get('YT_LIVE_FORMAT', '91/92/93/best[height<=480]')
YT_MAX = int(os.environ.get('YT_MAX', '20'))   # newest videos played from a channel
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')   # /admin page (disabled when empty)
ADMIN_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'admin.html')
YT_CHANNELS = os.environ.get('YT_CHANNELS', '/data/youtube.m3u')  # one dcplaya playlist per channel
YT_LIST_MAX = int(os.environ.get('YT_LIST_MAX', '25'))            # videos per channel playlist
YT_REFRESH_HOURS = float(os.environ.get('YT_REFRESH_HOURS', '24'))  # background refresh period
STD_FPS = ('23.976', '24', '25', '29.97', '30')
FIRST_BYTE_TIMEOUT = 25
CHUNK = 16 * 1024


def log(msg):
    print(time.strftime('%Y-%m-%d %H:%M:%S'), msg, flush=True)


# ------------------------------------------------------------------ channels

def ascii_name(s):
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii')
    s = re.sub(r'[^\x20-\x7e]', '', s).strip()
    return s or '?'


def parse_m3u(path):
    chans = []
    name = None
    with open(path, encoding='utf-8', errors='replace') as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.startswith('#EXTINF'):
                name = line.rsplit(',', 1)[-1].strip() if ',' in line else None
            elif not line.startswith('#'):
                chans.append({'name': ascii_name(name or line.rsplit('/', 1)[-1]), 'url': line})
                name = None
    return chans


_chan_cache = {'mtime': None, 'list': []}
_chan_lock = threading.Lock()


def channels():
    with _chan_lock:
        try:
            m = os.path.getmtime(CHANNELS)
        except OSError:
            return []
        if m != _chan_cache['mtime']:
            _chan_cache['list'] = parse_m3u(CHANNELS)
            _chan_cache['mtime'] = m
            log(f'loaded {len(_chan_cache["list"])} channels from {CHANNELS}')
        return _chan_cache['list']


def dc_playlist(host, token, chans):
    """dcplaya m3u: '#EXTINF:<digits>,name' + '/http/host/path<endpath>'."""
    out = ['#EXTM3U']
    for i, c in enumerate(chans, 1):
        out.append(f'#EXTINF:0,{c["name"]}')
        out.append(f'/http/{host}/{token}/ch/{i}.mpg<endpath>')
    return '\n'.join(out) + '\n'


# ------------------------------------------------------------------ YouTube

class SourceError(Exception):
    pass


YT_RE = re.compile(r'^https?://(www\.|m\.)?(youtube\.com|youtu\.be)/', re.I)
_yt_cache = {}              # key -> (expires, value)
_yt_lock = threading.Lock()


def yt_kind(url):
    """None (not YouTube), 'video', 'live' or 'list' (channel / playlist)."""
    if not YT_RE.match(url):
        return None
    path = url.split('?', 1)[0].rstrip('/')
    if path.endswith('/live'):
        return 'live'
    if '/watch' in path or 'youtu.be/' in path or '/shorts/' in path or '/live/' in path:
        return 'video'
    return 'list'


def _yt_cached(key, ttl, fn):
    now = time.time()
    with _yt_lock:
        hit = _yt_cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    value = fn()
    with _yt_lock:
        _yt_cache[key] = (now + ttl, value)
    return value


def _ytdlp(args, timeout=60):
    try:
        r = subprocess.run(['yt-dlp', '--no-warnings'] + args,
                           capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise SourceError('yt-dlp is not installed')
    except subprocess.TimeoutExpired:
        raise SourceError('yt-dlp timed out')
    out = [ln for ln in r.stdout.splitlines() if ln.strip()]
    if r.returncode or not out:
        err = (r.stderr.strip().splitlines() or ['no output'])[-1]
        raise SourceError(err[:160])
    return out


def yt_media_url(url, live=False):
    """Direct media URL (they expire after a few hours: cached for less)."""
    fmt = YT_LIVE_FORMAT if live else YT_FORMAT
    return _yt_cached(('url', url, fmt), 60 if live else 2 * 3600,
                      lambda: _ytdlp(['--no-playlist', '-f', fmt, '-g', url])[0])


def yt_list(url):
    """Newest YT_MAX video ids of a channel, or the videos of a playlist."""
    if 'list=' not in url and not re.search(r'/(videos|streams|shorts)/?$', url):
        url = url.rstrip('/') + '/videos'
    return _yt_cached(('list', url), 30 * 60, lambda: _ytdlp(
        ['--flat-playlist', '--playlist-end', str(YT_MAX), '--print', 'id', url]))


def source_args(chan):
    """ffmpeg input arguments for a channel (resolving YouTube links)."""
    if chan.get('yt_id'):
        return ['-i', f"http://127.0.0.1:{PORT}/_yt/{chan['yt_id']}"]
    url = chan['url']
    kind = yt_kind(url)
    net = ['-reconnect', '1', '-reconnect_streamed', '1',
           '-reconnect_on_network_error', '1', '-reconnect_delay_max', '4']
    if kind is None:
        return ['-user_agent', UPSTREAM_UA] + net + ['-i', url]
    if kind == 'live':
        return net + ['-i', yt_media_url(url, live=True)]
    if kind == 'video':
        vid = _ytdlp(['--no-playlist', '--print', 'id', url])[0]
        return ['-i', f'http://127.0.0.1:{PORT}/_yt/{vid}']
    # channel / playlist: play the videos back to back.  Each entry is a local
    # URL that redirects to the media, so a video is only resolved when reached.
    ids = yt_list(url)
    name = re.sub(r'[^A-Za-z0-9]', '_', url)[-80:]
    path = os.path.join('/tmp', f'yt_{name}.ffconcat')
    with open(path, 'w') as f:
        f.write('ffconcat version 1.0\n')
        for vid in ids:
            f.write(f"file 'http://127.0.0.1:{PORT}/_yt/{vid}'\n")
    return ['-f', 'concat', '-safe', '0',
            '-protocol_whitelist', 'file,http,https,tcp,tls,crypto', '-i', path]


# ---------------------------------------------- YouTube channel playlists

_ytl_lock = threading.Lock()
_ytl = {'mtime': None, 'chans': [], 'videos': {}, 'stamp': {}}


def yt_channels():
    """Channels of YT_CHANNELS (m3u: #EXTINF:...,Name + channel URL)."""
    with _ytl_lock:
        try:
            m = os.path.getmtime(YT_CHANNELS)
        except OSError:
            return []
        if m != _ytl['mtime']:
            _ytl['chans'] = parse_m3u(YT_CHANNELS)
            _ytl['mtime'] = m
            _ytl['videos'], _ytl['stamp'] = {}, {}
            log(f'loaded {len(_ytl["chans"])} YouTube channels from {YT_CHANNELS}')
        return list(_ytl['chans'])


def yt_fetch_videos(url):
    if 'list=' not in url and not re.search(r'/(videos|streams|shorts)/?$', url):
        url = url.rstrip('/') + '/videos'
    out = []
    for line in _ytdlp(['--flat-playlist', '--playlist-end', str(YT_LIST_MAX),
                        '--print', '%(id)s\t%(duration)s\t%(title)s', url], timeout=120):
        vid, dur, title = (line.split('\t', 2) + ['', ''])[:3]
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', vid):
            continue
        name = ascii_name(title)[:44]
        try:
            name += f' ({max(1, round(float(dur) / 60))} min)'
        except ValueError:
            pass
        out.append({'id': vid, 'name': name})
    return out


def yt_refresh(n, chan):
    try:
        vids = yt_fetch_videos(chan['url'])
    except SourceError as e:
        log(f'youtube list {n} {chan["name"]!r}: {e}')
        return
    with _ytl_lock:
        _ytl['videos'][n] = vids
        _ytl['stamp'][n] = time.time()
    log(f'youtube list {n} {chan["name"]!r}: {len(vids)} videos')


def yt_videos(n):
    chans = yt_channels()
    if not 1 <= n <= len(chans):
        return None
    with _ytl_lock:
        vids = _ytl['videos'].get(n)
    if vids is None:                      # not fetched yet: do it now
        yt_refresh(n, chans[n - 1])
        with _ytl_lock:
            vids = _ytl['videos'].get(n, [])
    return vids


def yt_refresher():
    """Daily job: refresh every channel's video list."""
    while True:
        for n, chan in enumerate(yt_channels(), 1):
            with _ytl_lock:
                age = time.time() - _ytl['stamp'].get(n, 0)
            if age >= YT_REFRESH_HOURS * 3600:
                yt_refresh(n, chan)
        time.sleep(600)


def yt_playlist(host, token, n):
    """n = channel number, or 0 for every channel in one list."""
    chans = yt_channels()
    if n == 0:
        numbers = range(1, len(chans) + 1)
    elif 1 <= n <= len(chans):
        numbers = [n]
    else:
        return None
    out = ['#EXTM3U']
    for k in numbers:
        prefix = f'{ascii_name(chans[k - 1]["name"])[:18]}: ' if n == 0 else ''
        for v in yt_videos(k) or []:
            out.append(f'#EXTINF:0,{prefix}{v["name"]}')
            out.append(f'/http/{host}/{token}/yt/v/{v["id"]}.mpg<endpath>')
    return '\n'.join(out) + '\n'


# ------------------------------------------------------------------ admin

def m3u_read_full(path):
    """Entries with their #EXTINF attributes, for the editor."""
    items, extinf, header = [], None, '#EXTM3U'
    try:
        lines = open(path, encoding='utf-8', errors='replace').read().splitlines()
    except OSError:
        return header, []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#EXTM3U'):
            header = line
        elif line.startswith('#EXTINF'):
            extinf = line
        elif not line.startswith('#'):
            attrs, name = '', ''
            if extinf:
                body = extinf.split(':', 1)[1]
                quoted, cut = False, -1
                for i, ch in enumerate(body):          # first comma outside quotes
                    if ch == '"':
                        quoted = not quoted
                    elif ch == ',' and not quoted:
                        cut = i
                        break
                if cut >= 0:
                    attrs, name = body[:cut], body[cut + 1:]
                else:
                    attrs = body
                attrs = re.sub(r'^\s*-?\d+(\.\d+)?\s*', '', attrs)
            items.append({'name': name.strip() or line.rsplit('/', 1)[-1],
                          'url': line, 'attrs': attrs.strip()})
            extinf = None
    return header, items


def m3u_write_full(path, header, items):
    """Rewrite in place (keeps the file's owner/mode) after a backup."""
    bdir = os.path.join(os.path.dirname(path), 'backups')
    os.makedirs(bdir, exist_ok=True)
    if os.path.exists(path):
        stamp = time.strftime('%Y%m%d-%H%M%S')
        with open(path, 'rb') as src, \
                open(os.path.join(bdir, f'{os.path.basename(path)}.{stamp}'), 'wb') as dst:
            dst.write(src.read())
        olds = sorted(f for f in os.listdir(bdir) if f.startswith(os.path.basename(path) + '.'))
        for f in olds[:-30]:
            os.remove(os.path.join(bdir, f))
    out = [header if header.startswith('#EXTM3U') else '#EXTM3U']
    for it in items:
        attrs = (' ' + it['attrs']) if it.get('attrs') else ''
        out.append(f'#EXTINF:-1{attrs},{it["name"]}')
        out.append(it['url'])
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(out) + '\n')


def admin_clean(items):
    clean = []
    for it in items:
        name = str(it.get('name', '')).replace('\n', ' ').replace(',', ' ').strip()
        url = str(it.get('url', '')).strip()
        attrs = str(it.get('attrs', '')).replace('\n', ' ').strip()
        if not url:
            continue
        if not re.match(r'^https?://\S+$', url):
            raise ValueError(f'invalid URL: {url[:60]}')
        clean.append({'name': name or url.rsplit('/', 1)[-1], 'url': url, 'attrs': attrs})
    return clean


def admin_state():
    _, tv = m3u_read_full(CHANNELS)
    _, yt = m3u_read_full(YT_CHANNELS)
    with _ytl_lock:
        for n, c in enumerate(yt, 1):
            vids = _ytl['videos'].get(n)
            c['videos'] = len(vids) if vids is not None else None
            c['latest'] = vids[0]['name'] if vids else ''
            stamp = _ytl['stamp'].get(n)
            c['updated'] = time.strftime('%d/%m %H:%M', time.localtime(stamp)) if stamp else ''
    return {'tv': tv, 'yt': yt, 'token': TOKEN, 'refresh_hours': YT_REFRESH_HOURS}


def admin_check(url):
    """Quick test of one source URL for the editor."""
    kind = yt_kind(url)
    if kind == 'list':
        try:
            vids = yt_fetch_videos(url)
            return bool(vids), f'{len(vids)} videos' + (f': {vids[0]["name"]}' if vids else '')
        except SourceError as e:
            return False, str(e)
    if kind:
        try:
            _ytdlp(['--no-playlist', '--print', 'title', url])
            return True, 'YouTube ok'
        except SourceError as e:
            return False, str(e)
    ok, why = check_upstream(url)
    return ok, f'HTTP {why}' if ok else why


_dcstats_prev = {}


def dcstats_log(key, raw):
    """Measurement builds: counters reported by the Dreamcast every ~10 s.

    rx = W5500 RX thread busy time (SPI + delivery), dl = delivery to lwIP,
    idle = counts of a lowest-priority spinner (grows only when the CPU is free).
    """
    try:
        cur = {k: int(v) for k, v in (kv.split('=') for kv in raw.split('_'))}
    except ValueError:
        return log(f'dcstats {key}: bad report {raw[:80]!r}')
    prev = _dcstats_prev.get(key)
    _dcstats_prev[key] = cur
    if not prev:
        return log(f'dcstats {key}: first report')
    d = {k: (cur[k] - prev[k]) & 0xFFFFFFFF for k in cur}
    us = d['us'] or 1
    log(f'dcstats {key}: {us / 1e6:.1f}s rx {100 * d["rx"] / us:.1f}% '
        f'(lwIP delivery {100 * d["dl"] / us:.1f}%) frames {d["fr"] * 1e6 / us:.0f}/s '
        f'{d["by"] * 1e6 / us / 1024:.1f} KiB/s polls {d["po"] * 1e6 / us:.0f}/s '
        f'idle {d["id"] * 1e6 / us:.0f}/s')


def yt_refresh_all_now():
    for n, chan in enumerate(yt_channels(), 1):
        yt_refresh(n, chan)


# ------------------------------------------------------------------ streams

_streams = {}               # key -> (Popen, started)
_streams_lock = threading.Lock()


def ffmpeg_cmd(inp):
    """inp: ffmpeg input arguments (see source_args)."""
    vf = (f'{VDEINT}fps={FPS},scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease:flags={VSCALER},'
          f'{VDENOISE}pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS}')
    if VIDEO:
        video = ['-map', '0:v:0'] + (['-map', '0:a:0?'] if AUDIO else []) + [
            '-vf', vf, '-r', FPS,
            # 15/12/10 fps are "unofficial" MPEG-1 rates (fewer frames to decode)
            '-strict', '-1' if (VCODEC == 'mpeg1video' and FPS not in STD_FPS) else 'normal',
            '-c:v', VCODEC, '-b:v', VBITRATE, '-maxrate', VBITRATE,
            '-bufsize', VBUFSIZE, '-g', GOP, '-bf', '0'] + VENC_EXTRA.split() + (
            ['-vtag', 'DX50'] if (VCODEC == 'mpeg4' and VFORMAT != 'mpegts') else [])
    else:
        video = ['-map', '0:a:0', '-vn']          # audio-only (diagnostics)
    return [
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
        '-fflags', '+genpts+discardcorrupt',
        '-probesize', '2M', '-analyzeduration', '3M',
        '-threads', '2',
    ] + inp + video + (['-af', AFILTER, '-c:a', ACODEC, '-ar', ASRATE, '-ac', ACHANNELS, '-b:a', ABITRATE] if AUDIO else ['-an']) + [
    ] + container_args() + ['pipe:1']


def container_args():
    """Output container.  VFORMAT=mpegts is what Scrivani's VLC demo used
    (MPEG-4 in MPEG-TS): every TS packet carries its own timestamps.  MUXRATE
    then only sets the send pace (no padding in the stream)."""
    if VFORMAT == 'mpegts':
        return ['-f', 'mpegts', '-mpegts_flags', '+resend_headers', '-pat_period', '0.5']
    if VCODEC == 'mpeg4':
        return ['-f', 'avi']
    return ['-f', 'vob' if VCODEC == 'mpeg2video' else 'mpeg',
            '-muxrate', MUXRATE, '-muxdelay', '0.7']


def stop_proc(p):
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(3)
        except subprocess.TimeoutExpired:
            p.kill()


def abort_conn(sock):
    """Reset a client connection, discarding unsent data.

    dcplaya opens every stream twice and never reads the first connection
    again; its unread data then sits in the Dreamcast's tiny lwIP buffers and
    starves the connection that is actually playing.  A RST frees it."""
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        sock.shutdown(socket.SHUT_RDWR)   # wakes the handler blocked in send()
    except OSError:
        pass


def register_stream(key, proc, sock=None):
    """One stream per client (zapping kills the previous one) and a global cap."""
    victims = []
    with _streams_lock:
        if key in _streams:
            victims.append(_streams.pop(key))
        while len(_streams) >= MAX_STREAMS:
            oldest = min(_streams, key=lambda k: _streams[k][1])
            victims.append(_streams.pop(oldest))
        _streams[key] = (proc, time.time(), sock)
    for v in victims:
        stop_proc(v[0])
        abort_conn(v[2])


def unregister_stream(key, proc):
    with _streams_lock:
        if key in _streams and _streams[key][0] is proc:
            del _streams[key]


def check_upstream(url, timeout=8):
    req = urllib.request.Request(url, headers={'User-Agent': UPSTREAM_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ok = 200 <= r.status < 300
            if ok:
                r.read(188)
            return ok, f'{r.status}'
    except Exception as e:  # noqa: BLE001 - report any network error to the client
        return False, type(e).__name__


# ------------------------------------------------------------------ HTTP

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    server_version = 'dcplaya-tv-proxy'

    def log_message(self, fmt, *args):
        if os.environ.get('DEBUG_REQUESTS') == '1':      # troubleshooting only
            log(f'request {self.client_address[0]}: {re.sub(TOKEN or "-", "<TOKEN>", fmt % args)}')

    def client_key(self):
        return (self.headers.get('CF-Connecting-IP')
                or self.headers.get('X-Forwarded-For', '').split(',')[0].strip()
                or self.client_address[0])

    def reply(self, code, body=b'', ctype='text/plain'):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if body and self.command != 'HEAD':
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def admin_auth(self):
        """HTTP basic auth (any user name, password ADMIN_PASSWORD)."""
        if not ADMIN_PASSWORD:
            self.reply(404, b'admin disabled: set ADMIN_PASSWORD\n')
            return False
        hdr = self.headers.get('Authorization', '')
        if hdr.startswith('Basic '):
            try:
                pw = base64.b64decode(hdr[6:]).decode('utf-8', 'replace').split(':', 1)[1]
            except (ValueError, IndexError):
                pw = ''
            if hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()):
                return True
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="dc-tv-proxy"')
        self.send_header('Content-Length', '0')
        self.end_headers()
        return False

    def reply_json(self, obj, code=200):
        self.reply(code, json.dumps(obj).encode('utf-8'), 'application/json; charset=utf-8')

    def admin_get(self, path):
        if not self.admin_auth():
            return
        if path in ('/admin', '/admin/'):
            try:
                body = open(ADMIN_HTML, 'rb').read()
            except OSError:
                return self.reply(500, b'admin.html missing\n')
            return self.reply(200, body, 'text/html; charset=utf-8')
        if path == '/admin/api/state':
            return self.reply_json(admin_state())
        return self.reply(404, b'not found\n')

    def do_POST(self):
        path = self.path.split('?', 1)[0]
        if not path.startswith('/admin/api/') or not self.admin_auth():
            if not path.startswith('/admin/api/'):
                self.reply(404, b'not found\n')
            return
        try:
            n = int(self.headers.get('Content-Length') or 0)
            data = json.loads(self.rfile.read(min(n, 2_000_000)) or b'{}')
        except ValueError:
            return self.reply_json({'ok': False, 'error': 'bad json'}, 400)
        try:
            if path in ('/admin/api/tv', '/admin/api/yt'):
                target = CHANNELS if path.endswith('/tv') else YT_CHANNELS
                header, _ = m3u_read_full(target)
                items = admin_clean(data.get('items', []))
                m3u_write_full(target, header, items)
                log(f'admin: saved {len(items)} entries to {target}')
                if path.endswith('/yt'):
                    threading.Thread(target=yt_refresh_all_now, daemon=True).start()
                return self.reply_json({'ok': True, 'count': len(items)})
            if path == '/admin/api/yt/refresh':
                threading.Thread(target=yt_refresh_all_now, daemon=True).start()
                return self.reply_json({'ok': True})
            if path == '/admin/api/check':
                ok, msg = admin_check(str(data.get('url', '')).strip())
                return self.reply_json({'ok': ok, 'msg': msg})
        except (ValueError, OSError) as e:
            return self.reply_json({'ok': False, 'error': str(e)}, 400)
        return self.reply(404, b'not found\n')

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path == '/health':
            return self.reply(200, b'ok\n')
        if path == '/admin' or path.startswith('/admin/'):
            return self.admin_get(path)
        m = re.fullmatch(r'/_yt/([A-Za-z0-9_-]{6,20})', path)
        if m and self.client_address[0] == '127.0.0.1':
            return self.relay_youtube(m.group(1))
        parts = path.strip('/').split('/')
        if not TOKEN or len(parts) < 2 or parts[0] != TOKEN:
            return self.reply(404, b'not found\n')

        if parts[1] == 'tv.m3u':
            q = self.path.split('?', 1)[1] if '?' in self.path else ''
            m = re.search(r'(?:^|&)host=([A-Za-z0-9.:-]+)', q)
            host = m.group(1) if m else (self.headers.get('Host') or f'localhost:{PORT}')
            body = dc_playlist(host, TOKEN, channels()).encode('ascii')
            return self.reply(200, body, 'audio/x-mpegurl')

        if parts[1] == 'yt':
            return self.youtube_route(parts[2:])
        if parts[1] == 'dcstats' and len(parts) == 3:
            dcstats_log(self.client_key(), parts[2])
            return self.reply(200, b'')

        m = re.fullmatch(r'(\d+)\.mpg', parts[2]) if len(parts) == 3 and parts[1] == 'ch' else None
        if not m:
            return self.reply(404, b'not found\n')
        chans = channels()
        n = int(m.group(1))
        if not 1 <= n <= len(chans):
            return self.reply(404, b'no such channel\n')
        chan = chans[n - 1]
        ua = self.headers.get('User-Agent', '')

        if ua == 'dcplaya' or self.command == 'HEAD':
            if yt_kind(chan['url']):
                try:   # resolving also warms the cache for the real request
                    source_args(chan)
                    ok, why = True, ''
                except SourceError as e:
                    ok, why = False, str(e)
            else:
                ok, why = check_upstream(chan['url'])
            log(f'probe ch{n} {chan["name"]!r}: {"ok" if ok else "FAIL " + why}')
            return self.reply(200 if ok else 502, b'')

        self.stream(n, chan)

    def youtube_route(self, rest):
        """/TOKEN/yt/index.txt, /TOKEN/yt/<n>.m3u, /TOKEN/yt/v/<id>.mpg"""
        if rest == ['index.txt']:
            body = ''.join(f'{i}\t{c["name"]}\n' for i, c in enumerate(yt_channels(), 1))
            return self.reply(200, body.encode('ascii', 'replace'))
        m = re.fullmatch(r'(\d+|all)\.m3u', rest[0]) if len(rest) == 1 else None
        if m:
            q = self.path.split('?', 1)[1] if '?' in self.path else ''
            hm = re.search(r'(?:^|&)host=([A-Za-z0-9.:-]+)', q)
            host = hm.group(1) if hm else (self.headers.get('Host') or f'localhost:{PORT}')
            body = yt_playlist(host, TOKEN, 0 if m.group(1) == 'all' else int(m.group(1)))
            if body is None:
                return self.reply(404, b'no such channel\n')
            log(f'youtube playlist {m.group(1)} for {self.client_key()}')
            return self.reply(200, body.encode('ascii'), 'audio/x-mpegurl')
        m = re.fullmatch(r'([A-Za-z0-9_-]{11})\.mpg', rest[1]) if len(rest) == 2 and rest[0] == 'v' else None
        if not m:
            return self.reply(404, b'not found\n')
        vid = m.group(1)
        if self.headers.get('User-Agent', '') == 'dcplaya' or self.command == 'HEAD':
            return self.reply(200, b'')            # resolved when it plays
        self.stream(f'yt:{vid}', {'name': vid, 'url': f'https://www.youtube.com/watch?v={vid}',
                                  'yt_id': vid})

    def relay_youtube(self, vid):
        """Local-only: relay yt-dlp's download of one video to ffmpeg.

        YouTube often answers 403 to the bare media URL (yt-dlp -g), but
        yt-dlp's own downloader is accepted, so it fetches and we relay."""
        # dcplaya opens every stream twice in a row and YouTube sometimes
        # answers 403 to the second download: retry a few times.
        for attempt in range(1, 4):
            proc = subprocess.Popen(
                ['yt-dlp', '--no-warnings', '--no-playlist', '-q', '--no-part',
                 '--extractor-retries', '3', '-f', YT_FORMAT, '-o', '-',
                 f'https://www.youtube.com/watch?v={vid}'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            threading.Thread(target=self._drain_err, args=(proc, f'yt {vid}'), daemon=True).start()
            data = proc.stdout.read(CHUNK)
            if data:
                break
            stop_proc(proc)
            log(f'youtube {vid}: no data (attempt {attempt})')
            time.sleep(attempt)
        else:
            return self.reply(502, b'')
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
            self.end_headers()
            while data:
                self.wfile.write(data)
                data = proc.stdout.read(CHUNK)
        except OSError:
            pass                         # ffmpeg went away (channel change)
        finally:
            stop_proc(proc)

    def stream(self, n, chan):
        key = self.client_key()
        log(f'start ch{n} {chan["name"]!r} for {key}')
        try:
            inp = source_args(chan)
        except SourceError as e:
            log(f'ch{n}: {e}')
            return self.reply(502, b'source failed\n')
        proc = subprocess.Popen(ffmpeg_cmd(inp), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0)
        register_stream(key, proc, self.connection)
        threading.Thread(target=self._drain_err, args=(proc, n), daemon=True).start()

        first = {}

        def reader():
            first['data'] = proc.stdout.read(CHUNK)

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t.join(FIRST_BYTE_TIMEOUT)
        data = first.get('data')
        if not data:
            stop_proc(proc)
            unregister_stream(key, proc)
            log(f'ch{n}: no data from upstream')
            return self.reply(502, b'upstream failed\n')

        sent = 0
        t0 = time.time()
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'video/mpeg')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            # Pace the output one TCP segment at a time: bursts overflow the
            # W5500/lwIP receive side on a real Dreamcast, and the 2004 lwIP
            # then collapses into retransmission timeouts (~0.5 KiB/s).
            bps = rate_bps(MUXRATE) / 8 * PACE if PACE > 0 else 0
            if SNDBUF > 0:
                try:
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SNDBUF)
                except OSError:
                    pass
            # A frozen console never ACKs again: give up after STALL_TIMEOUT
            # (long enough for a pause with Start).
            self.connection.settimeout(STALL_TIMEOUT)
            last_log, last_sent = time.time(), 0
            while data:
                now = time.time()
                if now - last_log >= 60:
                    log(f'ch{n} for {key}: {(now - t0) / 60:.0f} min, {sent // 1024} KiB, '
                        f'{(sent - last_sent) / 1024 / (now - last_log):.0f} KiB/s last min')
                    last_log, last_sent = now, sent
                for i in range(0, len(data), SEG):
                    piece = data[i:i + SEG]
                    self.wfile.write(piece)
                    sent += len(piece)
                    if bps and sent > PACE_BURST:
                        ahead = (sent - PACE_BURST) / bps - (time.time() - t0)
                        if ahead > 0:
                            time.sleep(ahead)
                data = proc.stdout.read(CHUNK)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            stop_proc(proc)
            unregister_stream(key, proc)
            dt = max(time.time() - t0, 0.001)
            log(f'stop ch{n} for {key}: {sent // 1024} KiB in {dt:.0f}s '
                f'({sent / 1024 / dt:.0f} KiB/s)')

    @staticmethod
    def _drain_err(proc, n):
        for line in proc.stderr:
            txt = line.decode('utf-8', 'replace').rstrip()
            if txt:
                log(f'ch{n} ffmpeg: {txt[:200]}')


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == '--playlist':
        src = sys.argv[4] if len(sys.argv) > 4 else CHANNELS
        sys.stdout.write(dc_playlist(sys.argv[2], sys.argv[3], parse_m3u(src)))
        return
    if not TOKEN:
        sys.exit('TOKEN is not set')
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    channels()
    threading.Thread(target=yt_refresher, daemon=True).start()
    log(f'listening on :{PORT} ({WIDTH}x{HEIGHT}@{FPS} {VCODEC} {VBITRATE} + {ACODEC} {ABITRATE}{"" if VIDEO else ", audio only"})')
    Server(('0.0.0.0', PORT), Handler).serve_forever()


if __name__ == '__main__':
    main()
