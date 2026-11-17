# dcplaya W5500 + IPTV for the Sega Dreamcast

*[Português](README.pt-BR.md)*

Patches for **dcplaya 2.0 beta 2** (the 2004 Dreamcast media player), plus a
small server that lets the Dreamcast watch your **IPTV** channels.

- **W5500 network adapter** (the SCI/serial-port mod) works as if it were the
  Broadband Adapter. A real BBA keeps working: the driver falls back to it.
- **DHCP**: the network dialog now defaults to `dhcp`.
- **Playlists don't freeze** on dead links: every `/http/` entry is checked in
  the background and skipped if it doesn't answer. **B** stops at any time.
- **Start** pauses and resumes. **B** stops, in the file list or the playlist.
- **TV proxy** (Docker): converts modern HD/FHD H.264/H.265 IPTV streams live
  into MPEG-1 + MP3, the format dcplaya 2004 can play.

```
Dreamcast ──HTTP──> dc-tv-proxy (Docker, ffmpeg 7) ──> your IPTV provider
          /http/<host>/<TOKEN>/ch/N.mpg
```

The original CD image is **not** included. You patch your own copy.

## What you need

| | |
|---|---|
| Original image | `DCPlaya Scrivani VideoCast_192_168_1_1.cdi` (dcplaya 2.0b2, DiscJuggler, MD5 `eb75eeb78184c54b9f6d2bde7657b15a`). Other images are not supported: the patcher checks the layout. |
| Build PC | Python 3.8+ (no extra packages) |
| Server | Any Linux box with Docker (a Raspberry Pi 4/5 or better, or an LXC/VM). It must be reachable from the Dreamcast. |
| IPTV | Your own `.m3u` list from **your** provider. Use only content you are entitled to watch. |
| Dreamcast | W5500 mod or Broadband Adapter, and a way to boot a CDI (CD-R or ODE) |

## 1. Install the proxy

```bash
git clone <this repo> && cd <repo>/proxy
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(6))"   # put the result in .env as TOKEN=...
cp /path/to/your/iptv.m3u data/channels.m3u
docker compose up -d --build
curl http://localhost:8765/health                          # -> ok
```

- `data/channels.m3u` is your IPTV list (`#EXTINF:...,Name` followed by the URL).
  It holds your IPTV login. It stays on the server and is ignored by git.
- The **TOKEN** is part of every URL the Dreamcast uses. Without it the proxy
  answers 404, so nobody else can use your IPTV account through it.
- Channel numbers follow the order of `channels.m3u`. If you add, remove or
  reorder channels, rebuild the CD image (step 2). The proxy reloads the list
  by itself.
- Logs: `docker logs -f dc-tv-proxy` shows each stream and its rate every minute.

### Reaching the proxy from outside your home (optional)

dcplaya only speaks **plain HTTP**, without TLS. Use a port-forward to `8765`,
or a tunnel such as Cloudflare Tunnel pointing `tv.example.com` to
`http://<server>:8765`. For that hostname, turn **off** "Always Use HTTPS" and
any HTTP→HTTPS redirect, and bypass the cache. Check from a PC:
`curl -I http://tv.example.com/health` must answer `200`, not `301`.

## 2. Build your CD image

```bash
cd cdi
python build_cdi.py "DCPlaya Scrivani VideoCast_192_168_1_1.cdi" dcplaya_tv.cdi \
       --proxy 192.168.1.20:8765 --token YOUR_TOKEN
```

- `--proxy` is the address **the Dreamcast** uses to reach the proxy. Use
  `HOST:PORT`, or just `HOST` for port 80. Don't put `http://` in front.
- By default the channel list is downloaded from the running proxy, so the
  build PC must reach it. Without network access to the proxy, use
  `--channels your_iptv.m3u`. It must be the same file and order as on the
  proxy.
- Several addresses are allowed: one playlist for home, one for outside.
  ```bash
  python build_cdi.py original.cdi dcplaya_tv.cdi --token YOUR_TOKEN \
         --proxy 192.168.1.20:8765=TV_LAN.M3U --proxy tv.example.com=TV.M3U
  ```
  Names are 8.3, upper case, and must sort after `RADIO.M3U` (`TV*.M3U` is fine).
- The output has the same size as the original. Burn it like the original
  (DiscJuggler/ImgBurn, CDI format), or copy it to your ODE.

The token ends up inside the image, so don't share your built CDI.

## 3. On the Dreamcast

1. Boot the disc. In **Network settings** leave **Ip** as `dhcp`, or type a
   fixed IP, mask, gateway and DNS. Then connect.
2. In the file browser open `cd` → `extra` and start `tv.m3u` (or your names).
   Each channel is checked first, and dead ones are skipped without freezing.
3. Controls:

   | Button | Action |
   |---|---|
   | A | play the selected channel or playlist |
   | Start | pause / resume (when nothing plays: virtual keyboard, as before) |
   | B | stop (file list or playlist). In the playlist, when nothing plays, B removes the entry as before. |
   | X | context menu |
   | Y | application switcher |

## Picture and sound tuning

The profile in `proxy/tune.env` is the one that played best on a **real**
console (W5500): MPEG-1 192x144 at 15 fps and 150 kbit/s, with MP3 44.1 kHz
128 kbit/s stereo. The limit is the Dreamcast CPU decoding video with dcplaya's
2004 ffmpeg, not the network. Change one value at a time, run
`docker compose up -d`, switch channel on the console and judge there. Flycast
is not representative: its emulated BBA is much slower.

Things that were tried on real hardware and were **worse**:

| Change | Result |
|---|---|
| MP2 audio | loud, garbled, unintelligible (even without video) |
| MP3 mono, or 22.05 kHz | A/V sync breaks, worse video and freezes |
| 224x168, 256x192, MPEG-2, MPEG-4 | too slow, audio stutters |
| 128x96 | the console froze |
| full frame every 10–20 (`GOP`) | slower, audio stutters |
| RD encoder options, denoise, deinterlace | measure better on a PC, look worse on the DC |

## Known issues

- After 5–10 minutes of video the console may freeze. The same crash happens in
  Flycast with the **untouched** original image, so it looks like a dcplaya
  bug. The proxy drops a frozen client after `STALL_TIMEOUT` (5 min).
- The occasional smear at scene changes is dcplaya skipping a frame when the
  CPU runs short. It clears at the next full frame (≤ 2 s).
- Pausing live TV for long: the IPTV provider may drop the upstream and the
  proxy reconnects, so playback resumes "live", not where it stopped.

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
| `EXTRA/TV*.M3U` (new) | your channel playlists |

The executable, IP.BIN, audio track and DiscJuggler header are untouched. New
data goes into the free padding at the end of the ISO volume, and EDC/ECC is
recomputed for the changed sectors.

**W5500 wiring.** The driver probes, in this order:
1. **SCI**: hardware SPI at 12.5 MHz, CS on **PA7**.
2. **SCIF / serial port**: bit-banged SPI with CLK=CTS, MOSI=TX, MISO=RX, CS=RTS.
3. Otherwise it loads the original BBA driver.

The W5500 runs socket 0 in MACRAW mode. The MAC address is derived from the
console ID.

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
- This package: W5500 driver, DHCP, playlist/pause/stop patches, CD patcher and TV proxy.

## License

The code in this repository is released under the **GNU General Public License
v2.0** (see [LICENSE](LICENSE)). dcplaya itself, the original CD image and your
IPTV content are not part of this repository and keep their own terms.
