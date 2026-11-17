# dcplaya W5500 + IPTV para Sega Dreamcast

*[English](README.md)*

Patches para o **dcplaya 2.0 beta 2**, o player de mídia de 2004 para Dreamcast,
e um pequeno servidor que deixa o Dreamcast assistir aos seus canais de **IPTV**.

- **Adaptador de rede W5500** (o mod no SCI/porta serial) funciona como se
  fosse a Broadband Adapter. Uma BBA original continua funcionando: o driver
  volta para ela.
- **DHCP**: a tela de rede já vem com `dhcp`.
- **Playlists não travam** com link fora do ar: cada item `/http/` é testado em
  segundo plano e, se não responder, é pulado. **B** para a qualquer momento.
- **Start** pausa e retoma. **B** para, tanto na lista de arquivos quanto na playlist.
- **Proxy de TV** (Docker): converte ao vivo os streams de IPTV HD/FHD
  H.264/H.265 em MPEG-1 + MP3, o formato que o dcplaya de 2004 toca.

```
Dreamcast ──HTTP──> dc-tv-proxy (Docker, ffmpeg 7) ──> seu provedor de IPTV
          /http/<host>/<TOKEN>/ch/N.mpg
```

A imagem original do CD **não** vem no pacote. Você aplica os patches na sua cópia.

## O que você precisa

| | |
|---|---|
| Imagem original | `DCPlaya Scrivani VideoCast_192_168_1_1.cdi` (dcplaya 2.0b2, DiscJuggler, MD5 `eb75eeb78184c54b9f6d2bde7657b15a`). Outras imagens não são suportadas, porque o patcher confere o layout. |
| PC para gerar a imagem | Python 3.8+ (sem pacotes extras) |
| Servidor | Qualquer Linux com Docker (Raspberry Pi 4/5 ou melhor, LXC ou VM), acessível pelo Dreamcast |
| IPTV | Sua própria lista `.m3u`, do **seu** provedor. Use só conteúdo que você tem direito de assistir. |
| Dreamcast | Mod W5500 ou Broadband Adapter, e um jeito de rodar CDI (CD-R ou ODE) |

## 1. Instalar o proxy

```bash
git clone <este repo> && cd <repo>/proxy
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(6))"   # coloque o resultado no .env como TOKEN=...
cp /caminho/da/sua/lista_iptv.m3u data/channels.m3u
docker compose up -d --build
curl http://localhost:8765/health                          # -> ok
```

- `data/channels.m3u` é a sua lista de IPTV (`#EXTINF:...,Nome` e, na linha
  seguinte, a URL). Ela contém o seu login do IPTV, fica só no servidor e o git
  ignora o arquivo.
- O **TOKEN** faz parte de toda URL que o Dreamcast usa. Sem ele o proxy
  responde 404, então ninguém usa a sua conta de IPTV por ele.
- O número de cada canal segue a ordem do `channels.m3u`. Se adicionar, remover
  ou reordenar canais, gere a imagem de novo (passo 2). O proxy recarrega a
  lista sozinho.
- Logs: `docker logs -f dc-tv-proxy` mostra cada stream e, a cada minuto, a taxa.

### Acessar o proxy fora de casa (opcional)

O dcplaya só fala **HTTP simples**, sem TLS. Use um redirecionamento de porta
para `8765` ou um túnel, como o Cloudflare Tunnel apontando `tv.exemplo.com`
para `http://<servidor>:8765`. Nesse hostname, **desligue** o "Always Use
HTTPS" e qualquer redirecionamento HTTP→HTTPS, e deixe o cache em bypass. Teste
no PC: `curl -I http://tv.exemplo.com/health` tem que responder `200`, não `301`.

## 2. Gerar a sua imagem

```bash
cd cdi
python build_cdi.py "DCPlaya Scrivani VideoCast_192_168_1_1.cdi" dcplaya_tv.cdi \
       --proxy 192.168.1.20:8765 --token SEU_TOKEN
```

- `--proxy` é o endereço que **o Dreamcast** usa para chegar ao proxy. Use
  `HOST:PORTA`, ou só `HOST` para a porta 80. Não coloque `http://` na frente.
- Por padrão a lista de canais é baixada do proxy em execução, então o PC
  precisa alcançá-lo. Sem acesso ao proxy, use `--channels sua_lista.m3u`, que
  precisa ser o mesmo arquivo, na mesma ordem, que está no proxy.
- Pode haver mais de um endereço, por exemplo uma playlist para casa e outra para fora:
  ```bash
  python build_cdi.py original.cdi dcplaya_tv.cdi --token SEU_TOKEN \
         --proxy 192.168.1.20:8765=TV_LAN.M3U --proxy tv.exemplo.com=TV.M3U
  ```
  Os nomes seguem o formato 8.3, em maiúsculas, e precisam vir depois de
  `RADIO.M3U` em ordem alfabética (`TV*.M3U` serve).
- A imagem gerada tem o mesmo tamanho da original. Grave como a original
  (DiscJuggler/ImgBurn, formato CDI) ou copie para o seu ODE.

O token fica gravado dentro da imagem, então não compartilhe a sua CDI.

## 3. No Dreamcast

1. Dê boot no disco. Em **Network settings**, deixe **Ip** como `dhcp` ou
   digite IP, máscara, gateway e DNS fixos. Depois conecte.
2. No navegador de arquivos abra `cd` → `extra` e toque `tv.m3u` (ou os nomes
   que você escolheu). Cada canal é testado antes, e os que estão fora do ar
   são pulados sem travar.
3. Controles:

   | Botão | Ação |
   |---|---|
   | A | toca o canal ou a playlist selecionada |
   | Start | pausa / retoma (sem nada tocando: teclado virtual, como antes) |
   | B | para (lista de arquivos ou playlist). Na playlist, com nada tocando, remove o item como antes. |
   | X | menu de contexto |
   | Y | alternador de aplicativos |

## Ajuste de imagem e som

O perfil em `proxy/tune.env` é o que tocou melhor num console **real**
(W5500): MPEG-1 192x144 a 15 fps e 150 kbit/s, com MP3 44,1 kHz 128 kbit/s
estéreo. O limite é a CPU do Dreamcast decodificando vídeo com o ffmpeg de 2004
do dcplaya, e não a rede. Mude um valor por vez, rode `docker compose up -d`,
troque de canal no console e avalie lá. O Flycast não serve de referência: a
BBA emulada dele é bem mais lenta.

O que foi testado em console real e ficou **pior**:

| Mudança | Resultado |
|---|---|
| Áudio MP2 | alto, distorcido, incompreensível (mesmo sem vídeo) |
| MP3 mono, ou 22,05 kHz | o sincronismo áudio/vídeo se perde: vídeo pior e travamentos |
| 224x168, 256x192, MPEG-2, MPEG-4 | lento demais, o som picota |
| 128x96 | o console travou |
| quadro completo a cada 10–20 (`GOP`) | mais lento, o som picota |
| opções RD do encoder, redução de ruído, desentrelaçamento | medem melhor no PC, ficam piores no DC |

## Problemas conhecidos

- Depois de 5–10 minutos de vídeo o console pode travar. O mesmo travamento
  acontece no Flycast com a imagem original **sem modificações**, então parece
  ser um bug do dcplaya. O proxy derruba o cliente travado depois de
  `STALL_TIMEOUT` (5 min).
- O borrão ocasional nas trocas de cena é o dcplaya pulando um quadro quando
  falta CPU. Ele some no próximo quadro completo (≤ 2 s).
- Pausar a TV ao vivo por muito tempo: o provedor de IPTV pode derrubar a
  conexão e o proxy reconecta. Por isso a reprodução volta "ao vivo", e não de
  onde parou.

## Por dentro

| Arquivo no CD | Mudança |
|---|---|
| `DCPLAYA/PLUGINS/EXE/NET/BBA/BBA.LEZ` | novo driver W5500 + cliente DHCP (`driver/w5500_bba.c`, `driver/dhcp.c`) |
| `.../BBA/RTL8139.LEZ` (novo) | o driver original da BBA, carregado quando não há W5500 |
| `.../NET/NETPROBE.LEZ` (novo) | teste HTTP em segundo plano usado pelas playlists (`driver/netprobe.c`) |
| `.../NET/NET.LEZ` | o lwIP desiste do connect após 2 SYN (~9 s em vez de ~45 s) |
| `DCPLAYA/LUA/NET.LUA` | DHCP por padrão, carrega o netprobe |
| `DCPLAYA/LUA/SONG_BROWSER.LUA` | teste assíncrono dos links, B para na playlist |
| `DCPLAYA/LUA/KEYBOARD_EMU.LUA` | Start pausa/retoma |
| `EXTRA/TV*.M3U` (novo) | as suas playlists de canais |

O executável, o IP.BIN, a trilha de áudio e o cabeçalho DiscJuggler não mudam.
Os dados novos vão para o espaço livre no fim do volume ISO, e o EDC/ECC é
recalculado nos setores alterados.

**Ligação do W5500.** O driver procura, nesta ordem:
1. **SCI**: SPI por hardware a 12,5 MHz, CS no **PA7**.
2. **SCIF / porta serial**: SPI por bit-bang com CLK=CTS, MOSI=TX, MISO=RX, CS=RTS.
3. Se não achar nenhum, carrega o driver original da BBA.

O W5500 usa o socket 0 em modo MACRAW, e o MAC é derivado do ID do console.

**Recompilar os plugins** (opcional; as cópias prontas estão em `cdi/prebuilt/`).
É preciso um toolchain `sh-elf` do KallistiOS. Os plugins são ligados em
tempo de execução ao KOS de 2004 que está dentro do executável do dcplaya. Por
isso usam as declarações de `driver/oldkos.h`, e o loader LEF só aceita
relocações `R_SH_DIR32`.

```bash
cd driver && ./build.sh                 # -> out/BBA.LEZ, out/NETPROBE.LEZ
cp out/BBA.LEZ out/NETPROBE.LEZ ../cdi/prebuilt/
```

## Créditos

- dcplaya de Benjamin Gerard e Vincent Penne (sashipa), port do ffmpeg com
  otimizações para SH4 por Bero.
- A versão VideoCast do dcplaya 2.0b2, por Scrivani.
- Este pacote: driver W5500, DHCP, patches de playlist/pausa/stop, patcher do CD e proxy de TV.

## Licença

O código deste repositório é distribuído sob a **GNU General Public License
v2.0** (veja [LICENSE](LICENSE)). O dcplaya em si, a imagem original do CD e o
seu conteúdo de IPTV não fazem parte deste repositório e seguem os próprios termos.
