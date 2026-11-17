# dcplaya W5500 + IPTV + YouTube for the Sega Dreamcast

*[Português](README.pt-BR.md)*

Patches for **dcplaya 2.0 beta 2** (the 2004 Dreamcast media player), plus a
small server that lets the Dreamcast watch your **IPTV** channels and the latest
videos of chosen **YouTube** channels.

- **W5500 network adapter** (the SCI/serial-port mod) works as if it were the
  Broadband Adapter. A real BBA keeps working: the driver falls back to it.
- **DHCP**: the network dialog defaults to `dhcp`.
- **Playlists don't freeze** on dead links: every `/http/` entry is checked in
  the background and skipped if it doesn't answer.
- **Start** pauses and resumes. **B** stops, in the file list or the playlist.
- **Playlists that update themselves.** `tv_proxy.m3u` and `youtube.m3u` on the
  disc are downloaded from the proxy each time they are opened. Changing
  channels never needs a new disc.
- **TV proxy** (Docker): converts modern HD/FHD H.264/H.265 IPTV streams and
  YouTube videos live into MPEG-1 + MP3, the format dcplaya 2004 can play. It
  refreshes the YouTube video lists every day.
- **Web admin page** (`/admin`) to edit the TV and YouTube channels.

```
Dreamcast ──HTTP──> dc-tv-proxy (Docker: ffmpeg 7, yt-dlp) ──> your IPTV provider / YouTube
```

The original CD image is **not** included. You patch your own copy.

## What you need

| | |
|---|---|
| Original image | `DCPlaya Scrivani VideoCast_192_168_1_1.cdi` (dcplaya 2.0b2, DiscJuggler, MD5 `eb75eeb78184c54b9f6d2bde7657b15a`). Other images are not supported: the patcher checks the layout. |
| Build PC | Python 3.8+ (no extra packages) |
| Server | Any x86-64 or ARM64 Linux box with Docker (Raspberry Pi 4/5, LXC, VM…), reachable from the Dreamcast |
| IPTV | Your own `.m3u` list from **your** provider. Use only content you are entitled to watch. |
| Dreamcast | W5500 mod or Broadband Adapter, and a way to boot a CDI (CD-R or ODE) |

## 1. Install the proxy

```bash
git clone <this repo> && cd <repo>/proxy
cp .env.example .env            # set TOKEN and ADMIN_PASSWORD
cp /path/to/your/iptv.m3u data/channels.m3u
docker compose up -d --build
curl http://localhost:8765/health   # -> ok
```

- **TOKEN** (in `.env`): it's part of every URL the Dreamcast uses. Without it
  the proxy answers 404, so nobody else can use your IPTV account through it.
  To generate one: `python3 -c "import secrets; print(secrets.token_hex(6))"`.
- **`data/channels.m3u`** is your IPTV list. It holds your IPTV login, stays on
  the server, and is ignored by git.
- **`data/youtube.m3u`** (optional): YouTube channels, for example:
  ```
  #EXTM3U
  #EXTINF:-1,Some channel
  https://www.youtube.com/@somechannel
  ```
- **`http://<server>:8765/admin`** (any user name, password `ADMIN_PASSWORD`):
  - edit, test, reorder and add TV and YouTube channels;
  - import m3u snippets;
  - refresh the YouTube lists now.

  Every save keeps the previous file in `data/backups/`. The page uses plain
  HTTP, so use it on your LAN.
- Logs: `docker logs -f dc-tv-proxy`.

### Reaching the proxy from outside your home (optional)

dcplaya only speaks **plain HTTP** (no TLS). You need a port-forward to `8765`,
or a tunnel such as Cloudflare Tunnel pointing `tv.example.com` to
`http://<server>:8765`.

For that hostname, turn **off** "Always Use HTTPS" and any HTTP→HTTPS redirect,
and bypass the cache. Check from a PC: `curl -I http://tv.example.com/health`
must answer `200`, not `301`.

Put a strong `ADMIN_PASSWORD` before exposing it, or block `/admin` at the tunnel.

## 2. Build your CD image

```bash
cd cdi
python build_cdi.py "DCPlaya Scrivani VideoCast_192_168_1_1.cdi" dcplaya_tv.cdi \
       --proxy 192.168.1.20:8765 --proxy tv.example.com --token YOUR_TOKEN
```

- `--proxy` is the address **the Dreamcast** uses to reach the proxy, as
  `HOST:PORT`, or `HOST` for port 80, without `http://`. Repeat it to give
  fallbacks, tried in order (e.g. LAN first, then internet).
- The proxy doesn't need to be running when you build: the disc only stores
  the addresses and the token.
- The output has the same size as the original. Burn it like the original
  (DiscJuggler/ImgBurn, CDI format), or copy it to your ODE.
- You only need to rebuild if the proxy address or the token changes.

The token ends up inside the image, so don't share your built CDI.

## 3. On the Dreamcast

1. Boot the disc. In **Network settings** leave **Ip** as `dhcp`, or type a
   fixed IP, mask, gateway and DNS. Then connect.
2. In the file browser open `cd` → `extra`:
   - `tv_proxy.m3u`: your TV channels;
   - `youtube.m3u`: the latest videos of every YouTube channel, each title
     prefixed with the channel name;
   - `radio.m3u`: the disc's original radio list.

   Each entry is checked first, and dead ones are skipped without freezing.
3. Controls:

   | Button | Action |
   |---|---|
   | A | play the selected channel or playlist |
   | Start | pause / resume (when nothing plays: virtual keyboard, as before) |
   | B | stop (file list or playlist). In the playlist, when nothing plays, B removes the entry as before. |
   | X | context menu |
   | Y | application switcher |

## Picture and sound

The profile in `proxy/tune.env` is the one that played best on a **real**
console (W5500):

- MPEG-1 192x144 at 15 fps and 150 kbit/s;
- MP3 44.1 kHz 128 kbit/s stereo, normalized to about -14 LUFS (`loudnorm`).

The limit is the Dreamcast CPU decoding video with dcplaya's 2004 ffmpeg, not
the network. The proxy shrinks HD/FHD sources with `area` scaling plus a light
blur (`VSCALER`, `VDENOISE`). On the same clip at the same size, that cut the
frames with visible blocks from 107 to 4 out of 451, compared to `lanczos`.

Change one value at a time, run `docker compose up -d`, switch channel on the
console and judge there. Flycast is not a good reference: its emulated BBA is
much slower than real hardware.

Things that were tried on real hardware and were **worse**:

| Change | Result |
|---|---|
| MP2 audio | loud, garbled, unintelligible (even without video) |
| MP3 mono, or 22.05 kHz | A/V sync breaks, worse video and freezes |
| 224x168, 256x192, MPEG-2, MPEG-4 | too slow, audio stutters |
| 128x96 | the console froze |
| full frame every 10–20 (`GOP`) | slower, audio stutters |
| RD encoder options, deinterlace | measure better on a PC, look worse on the DC |

## YouTube

- One YouTube channel per entry in `data/youtube.m3u` (or the admin page).
- The proxy keeps the newest `YT_LIST_MAX` (25) videos of each one and
  refreshes the lists every `YT_REFRESH_HOURS` (24 h) and at start.
- Uses `yt-dlp` with `deno` (installed in the image). `yt-dlp` is updated each
  time the container starts, because YouTube changes often.
- YouTube's terms of service don't allow playing its videos outside its own
  site and apps. This feature is for your personal use. Leave
  `data/youtube.m3u` empty if you don't want it.

## Known issues

- Some YouTube videos show a 4:3 game inside a 16:9 frame with decorations, so
  the game ends up small (about 140x105 pixels on the Dreamcast).
- The occasional smear at scene changes is dcplaya skipping a frame when the
  CPU runs short. It clears at the next full frame (≤ 2 s).
- Pausing live TV for long: the IPTV provider may drop the connection and the
  proxy reconnects, so playback resumes "live", not where it stopped.
- In Flycast, network video sometimes stops the emulator ("SH4 exception when
  blocked"), even with the untouched original image.

## Under the hood

| File on the CD | Change |
|---|---|
| `DCPLAYA/PLUGINS/EXE/NET/BBA/BBA.LEZ` | new W5500 driver + DHCP client (`driver/w5500_bba.c`, `driver/dhcp.c`) |
| `.../BBA/RTL8139.LEZ` (new) | the original BBA driver, loaded when no W5500 is found |
| `.../NET/NETPROBE.LEZ` (new) | background HTTP check used by playlists (`driver/netprobe.c`) |
| `.../NET/NET.LEZ` | lwIP connect gives up after 2 SYN retries (~9 s instead of ~45 s) |
| `DCPLAYA/LUA/NET.LUA` | DHCP default, loads netprobe |
| `DCPLAYA/LUA/SONG_BROWSER.LUA` | async link check, B stops in the playlist |
| `DCPLAYA/LUA/KEYBOARD_EMU.LUA` | Start pauses/resumes |
| `DCPLAYA/LUA/PLAYLIST.LUA` | `#DCPLAYA-REMOTE:` lines: the playlist is downloaded from that address |
| `EXTRA/TV_PROXY.M3U`, `EXTRA/YOUTUBE.M3U` (new) | remote playlists pointing at your proxy |

The executable, IP.BIN, audio track and DiscJuggler header are untouched. New
data goes into the free padding at the end of the ISO volume, and EDC/ECC is
recomputed for the changed sectors.

**W5500 wiring.** The driver probes, in this order:
1. **SCI**: hardware SPI at 12.5 MHz, CS on **PA7**.
2. **SCIF / serial port**: bit-banged SPI with CLK=CTS, MOSI=TX, MISO=RX, CS=RTS.
3. Otherwise it loads the original BBA driver.

The W5500 runs socket 0 in MACRAW mode. A receive thread polls it, because the
mod has no interrupt line.

dcplaya's lwIP input hook was written for the BBA's interrupt handler. Called
from a thread, its `thd_schedule_next` mixed up two threads' contexts, which
crashed the console after a few minutes. So the driver:
- points that call to a wrapper that only acts inside an interrupt;
- delivers frames with interrupts disabled;
- never prints or looks up symbols from the receive thread.

**Rebuilding the plugins** (optional; prebuilt copies are in `cdi/prebuilt/`).
You need a KallistiOS `sh-elf` toolchain. The plugins link at run time against
the 2004 KOS inside dcplaya's executable, so they use the declarations in
`driver/oldkos.h`, and the LEF loader only accepts `R_SH_DIR32` relocations.

```bash
cd driver && ./build.sh                 # -> out/BBA.LEZ, out/NETPROBE.LEZ
cp out/BBA.LEZ out/NETPROBE.LEZ ../cdi/prebuilt/
```

## Credits

- dcplaya by Benjamin Gerard and Vincent Penne (sashipa), ffmpeg port with SH4
  optimisations by Bero.
- The VideoCast build of dcplaya 2.0b2 by Scrivani.
- This package: W5500 driver, DHCP, playlist/pause/stop/remote-playlist
  patches, CD patcher, TV/YouTube proxy and admin page.

## License

The code in this repository is released under the **GNU General Public License
v2.0** (see [LICENSE](LICENSE)). dcplaya itself, the original CD image and your
IPTV/YouTube content are not part of this repository and keep their own terms.
