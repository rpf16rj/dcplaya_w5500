"""dcplaya TV proxy: modern ffmpeg on the server, MPEG-1 for the Dreamcast.

The Dreamcast cannot decode today's HD/FHD H.264/H.265 IPTV streams, so this
server pulls each channel and transcodes it live into what dcplaya 2.0b2's
(2004) ffmpeg plugin plays on a real console: MPEG-1 video 192x144 @ 15 fps +
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
  WIDTH/HEIGHT   output size                     (default 192x144)
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
import http.server
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
WIDTH = int(os.environ.get('WIDTH', '192'))
HEIGHT = int(os.environ.get('HEIGHT', '144'))
FPS = os.environ.get('FPS', '15')
VBITRATE = os.environ.get('VBITRATE', '150k')
ABITRATE = os.environ.get('ABITRATE', '128k')
ACHANNELS = os.environ.get('ACHANNELS', '2')
MUXRATE = os.environ.get('MUXRATE', '600k')
GOP = os.environ.get('GOP', '30')                # frames between I-frames (longer = cheaper to decode)
AFILTER = os.environ.get('AFILTER', 'volume=0.5,alimiter=limit=0.7:level=false')  # tame loud TV audio (clipping = pops on the DC)
ACODEC = os.environ.get('ACODEC', 'libmp3lame')        # mp2 or libmp3lame
ASRATE = os.environ.get('ASRATE', '44100')       # keep 44100: other rates break dcplaya's A/V sync
VBUFSIZE = os.environ.get('VBUFSIZE', '1000k')     # rate-control buffer (smaller = steadier quality)
# Encoder tweaks: only change these after testing on a real console.
VENC_EXTRA = os.environ.get('VENC_EXTRA', '-qmin 2')  # RD options measured better but looked worse on the DC
VDENOISE = os.environ.get('VDENOISE', '')  # e.g. hqdn3d=2:1.5:3:2.5, (looked worse on the DC)
VDEINT = os.environ.get('VDEINT', '')  # e.g. bwdif=mode=send_frame:deint=interlaced, (looked worse on the DC)
VCODEC = os.environ.get('VCODEC', 'mpeg1video')  # mpeg1video (PS), mpeg2video (VOB), mpeg4 (DivX AVI)
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


# ------------------------------------------------------------------ streams

_streams = {}               # key -> (Popen, started)
_streams_lock = threading.Lock()


def ffmpeg_cmd(url):
    vf = (f'{VDEINT}fps={FPS},scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease:flags=lanczos,'
          f'{VDENOISE}pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS}')
    if VIDEO:
        video = ['-map', '0:v:0'] + (['-map', '0:a:0?'] if AUDIO else []) + [
            '-vf', vf, '-r', FPS,
            # 15/12/10 fps are "unofficial" MPEG-1 rates (fewer frames to decode)
            '-strict', '-1' if (VCODEC == 'mpeg1video' and FPS not in STD_FPS) else 'normal',
            '-c:v', VCODEC, '-b:v', VBITRATE, '-maxrate', VBITRATE,
            '-bufsize', VBUFSIZE, '-g', GOP, '-bf', '0'] + VENC_EXTRA.split() + (
            ['-vtag', 'DX50'] if VCODEC == 'mpeg4' else [])
    else:
        video = ['-map', '0:a:0', '-vn']          # audio-only (diagnostics)
    return [
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
        '-user_agent', UPSTREAM_UA,
        '-reconnect', '1', '-reconnect_streamed', '1',
        '-reconnect_on_network_error', '1', '-reconnect_delay_max', '4',
        '-fflags', '+genpts+discardcorrupt',
        '-probesize', '2M', '-analyzeduration', '3M',
        '-threads', '2',
        '-i', url,
    ] + video + (['-af', AFILTER, '-c:a', ACODEC, '-ar', ASRATE, '-ac', ACHANNELS, '-b:a', ABITRATE] if AUDIO else ['-an']) + [
    ] + (['-f', 'avi'] if VCODEC == 'mpeg4' else
         ['-f', 'vob' if VCODEC == 'mpeg2video' else 'mpeg',
          '-muxrate', MUXRATE, '-muxdelay', '0.7']) + ['pipe:1']


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
        pass

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

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path == '/health':
            return self.reply(200, b'ok\n')
        parts = path.strip('/').split('/')
        if not TOKEN or len(parts) < 2 or parts[0] != TOKEN:
            return self.reply(404, b'not found\n')

        if parts[1] == 'tv.m3u':
            q = self.path.split('?', 1)[1] if '?' in self.path else ''
            m = re.search(r'(?:^|&)host=([A-Za-z0-9.:-]+)', q)
            host = m.group(1) if m else (self.headers.get('Host') or f'localhost:{PORT}')
            body = dc_playlist(host, TOKEN, channels()).encode('ascii')
            return self.reply(200, body, 'audio/x-mpegurl')

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
            ok, why = check_upstream(chan['url'])
            log(f'probe ch{n} {chan["name"]!r}: {"ok" if ok else "FAIL " + why}')
            return self.reply(200 if ok else 502, b'')

        self.stream(n, chan)

    def stream(self, n, chan):
        key = self.client_key()
        log(f'start ch{n} {chan["name"]!r} for {key}')
        proc = subprocess.Popen(ffmpeg_cmd(chan['url']), stdout=subprocess.PIPE,
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
    log(f'listening on :{PORT} ({WIDTH}x{HEIGHT}@{FPS} {VCODEC} {VBITRATE} + {ACODEC} {ABITRATE}{"" if VIDEO else ", audio only"})')
    Server(('0.0.0.0', PORT), Handler).serve_forever()


if __name__ == '__main__':
    main()
