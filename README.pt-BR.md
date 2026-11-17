# dcplaya W5500 + IPTV + YouTube para Sega Dreamcast

*[English](README.md)*

Patches para o **dcplaya 2.0 beta 2**, o player de mídia de 2004 para
Dreamcast, e um pequeno servidor que deixa o Dreamcast assistir aos seus
canais de **IPTV** e aos vídeos mais recentes de canais do **YouTube** que você
escolher.

- **Adaptador de rede W5500** (o mod no SCI/porta serial) funciona como se
  fosse a Broadband Adapter. Uma BBA original continua funcionando: o driver
  usa a BBA quando não encontra o W5500.
- **DHCP**: a tela de rede já vem com `dhcp`.
- **Playlists não travam** com link fora do ar: cada item `/http/` é testado
  em segundo plano e, se não responder, é pulado.
- **Start** pausa e retoma. **B** para, na lista de arquivos ou na playlist.
- **Playlists que se atualizam sozinhas:** `tv_proxy.m3u` e `youtube.m3u` são
  baixadas do proxy cada vez que você abre. Mudar canais nunca exige gravar
  outro CD.
- **Proxy** (Docker): converte ao vivo os streams de IPTV HD/FHD em
  H.264/H.265 e os vídeos do YouTube para MPEG-1 + MP3, o formato que o dcplaya
  de 2004 toca. Também atualiza as listas de vídeos do YouTube todo dia.
- **Página de administração** (`/admin`) para editar os canais de TV e do YouTube.

```
Dreamcast ──HTTP──> dc-tv-proxy (Docker: ffmpeg 7, yt-dlp) ──> seu provedor de IPTV / YouTube
```

A imagem original do CD **não** vem no pacote. Você aplica os patches na sua cópia.

## O que você precisa

| | |
|---|---|
| Imagem original | `DCPlaya Scrivani VideoCast_192_168_1_1.cdi` (dcplaya 2.0b2, DiscJuggler, MD5 `eb75eeb78184c54b9f6d2bde7657b15a`). Outras imagens não são suportadas, porque o patcher confere o layout. |
| PC para gerar a imagem | Python 3.8+ (sem pacotes extras) |
| Servidor | Qualquer Linux x86-64 ou ARM64 com Docker (Raspberry Pi 4/5, LXC, VM…), acessível pelo Dreamcast |
| IPTV | Sua própria lista `.m3u`, do **seu** provedor. Use só conteúdo que você tem direito de assistir. |
| Dreamcast | Mod W5500 ou Broadband Adapter, e um jeito de rodar CDI (CD-R ou ODE) |

## 1. Instalar o proxy

```bash
git clone <este repo> && cd <repo>/proxy
cp .env.example .env            # preencha TOKEN e ADMIN_PASSWORD
cp /caminho/da/sua/lista_iptv.m3u data/channels.m3u
docker compose up -d --build
curl http://localhost:8765/health   # -> ok
```

- **TOKEN** (no `.env`): faz parte de toda URL que o Dreamcast usa. Sem ele o
  proxy responde 404, então ninguém usa a sua conta de IPTV por ele. Para gerar
  um: `python3 -c "import secrets; print(secrets.token_hex(6))"`.
- **`data/channels.m3u`** é a sua lista de IPTV. Ela contém o seu login, fica
  só no servidor, e o git ignora o arquivo.
- **`data/youtube.m3u`** (opcional): canais do YouTube, por exemplo:
  ```
  #EXTM3U
  #EXTINF:-1,Algum canal
  https://www.youtube.com/@algumcanal
  ```
- **`http://<servidor>:8765/admin`** (qualquer usuário, senha `ADMIN_PASSWORD`):
  - editar, testar, reordenar e adicionar canais de TV e do YouTube;
  - importar trechos de m3u;
  - atualizar as listas do YouTube na hora.

  A cada vez que você salva, a versão anterior fica em `data/backups/`. A
  página usa HTTP simples: use na rede local.
- Logs: `docker logs -f dc-tv-proxy`.

### Acessar o proxy fora de casa (opcional)

O dcplaya só fala **HTTP simples**, sem TLS. Use um redirecionamento de porta
para `8765` ou um túnel, como o Cloudflare Tunnel apontando `tv.exemplo.com`
para `http://<servidor>:8765`.

Nesse hostname, **desligue** o "Always Use HTTPS" e qualquer redirecionamento
HTTP→HTTPS, e deixe o cache em bypass. Teste no PC:
`curl -I http://tv.exemplo.com/health` tem que responder `200`, não `301`.

Antes de expor o proxy na internet, troque o `ADMIN_PASSWORD` por uma senha
forte ou bloqueie o caminho `/admin` no túnel.

## 2. Gerar a sua imagem

```bash
cd cdi
python build_cdi.py "DCPlaya Scrivani VideoCast_192_168_1_1.cdi" dcplaya_tv.cdi \
       --proxy 192.168.1.20:8765 --proxy tv.exemplo.com --token SEU_TOKEN
```

- `--proxy` é o endereço que **o Dreamcast** usa para chegar ao proxy, no
  formato `HOST:PORTA` ou só `HOST` para a porta 80, sem `http://`. Pode
  repetir para dar alternativas, tentadas em ordem (primeiro a rede local,
  depois a internet).
- O proxy não precisa estar ligado na hora de gerar, porque o disco só guarda
  os endereços e o token.
- A imagem tem o mesmo tamanho da original. Grave como a original
  (DiscJuggler/ImgBurn, formato CDI) ou copie para o seu ODE.
- Só é preciso gerar de novo se mudar o endereço do proxy ou o token.

O token fica gravado dentro da imagem, então não compartilhe a sua CDI.

## 3. No Dreamcast

1. Dê boot no disco. Em **Network settings**, deixe **Ip** como `dhcp` ou
   digite IP, máscara, gateway e DNS fixos. Depois conecte.
2. No navegador de arquivos abra `cd` → `extra`:
   - `tv_proxy.m3u`: os seus canais de TV;
   - `youtube.m3u`: os vídeos mais recentes de todos os canais do YouTube, com
     o nome do canal antes do título;
   - `radio.m3u`: a lista de rádios original do disco.

   Cada item é testado antes, e os que estão fora do ar são pulados sem travar.
3. Controles:

   | Botão | Ação |
   |---|---|
   | A | toca o canal ou a playlist selecionada |
   | Start | pausa / retoma (sem nada tocando: teclado virtual, como antes) |
   | B | para (lista de arquivos ou playlist). Na playlist, com nada tocando, remove o item como antes. |
   | X | menu de contexto |
   | Y | alternador de aplicativos |

## Imagem e som

O perfil em `proxy/tune.env` é o que tocou melhor num console **real** (W5500):

- MPEG-1 192x144 a 15 fps e 150 kbit/s;
- MP3 44,1 kHz 128 kbit/s estéreo, normalizado em ~-14 LUFS (`loudnorm`).

O limite é a CPU do Dreamcast decodificando vídeo com o ffmpeg de 2004 do
dcplaya, e não a rede.

O proxy reduz fontes HD/FHD com escala `area` e um desfoque leve (`VSCALER`,
`VDENOISE`). No mesmo trecho e com o mesmo tamanho de stream, os quadros com
blocos visíveis caíram de 107 para 4 em 451, em comparação com o `lanczos`.

Mude um valor por vez, rode `docker compose up -d`, troque de canal no console
e avalie lá. O Flycast não serve de referência: a BBA emulada dele é bem mais
lenta que o hardware real.

O que foi testado em console real e ficou **pior**:

| Mudança | Resultado |
|---|---|
| Áudio MP2 | alto, distorcido, incompreensível (mesmo sem vídeo) |
| MP3 mono, ou 22,05 kHz | o sincronismo áudio/vídeo se perde: vídeo pior e travamentos |
| 224x168, 256x192, MPEG-2, MPEG-4 | lento demais, o som picota |
| 128x96 | o console travou |
| quadro completo a cada 10–20 (`GOP`) | mais lento, o som picota |
| opções RD do encoder, desentrelaçamento | medem melhor no PC, ficam piores no DC |

## YouTube

- Um canal do YouTube por entrada em `data/youtube.m3u` (ou pela página de
  administração).
- O proxy guarda os `YT_LIST_MAX` (25) vídeos mais recentes de cada canal e
  atualiza as listas a cada `YT_REFRESH_HOURS` (24 h) e ao iniciar.
- Usa `yt-dlp` com `deno`, que já vêm na imagem. O `yt-dlp` é atualizado a cada
  início do container, porque o YouTube muda com frequência.
- Os termos de uso do YouTube não permitem reproduzir vídeos fora do site e
  dos apps dele. Este recurso é para uso pessoal. Deixe `data/youtube.m3u`
  vazio se não quiser usá-lo.

## Problemas conhecidos

- Alguns vídeos do YouTube mostram o jogo em 4:3 dentro de um quadro 16:9 com
  molduras, e o jogo fica pequeno (~140x105 pixels no Dreamcast).
- O borrão ocasional nas trocas de cena é o dcplaya pulando um quadro quando
  falta CPU. Ele some no próximo quadro completo (≤ 2 s).
- Pausar a TV ao vivo por muito tempo: o provedor de IPTV pode derrubar a
  conexão e o proxy reconecta. Por isso a reprodução volta "ao vivo", e não de
  onde parou.
- No Flycast, vídeo pela rede às vezes para o emulador ("SH4 exception when
  blocked"), mesmo com a imagem original sem modificações.

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
| `DCPLAYA/LUA/PLAYLIST.LUA` | linhas `#DCPLAYA-REMOTE:`: a playlist é baixada desse endereço |
| `EXTRA/TV_PROXY.M3U`, `EXTRA/YOUTUBE.M3U` (novos) | playlists remotas apontando para o seu proxy |

O executável, o IP.BIN, a trilha de áudio e o cabeçalho DiscJuggler não mudam.
Os dados novos vão para o espaço livre no fim do volume ISO, e o EDC/ECC é
recalculado nos setores alterados.

**Ligação do W5500.** O driver procura, nesta ordem:
1. **SCI**: SPI por hardware a 12,5 MHz, CS no **PA7**.
2. **SCIF / porta serial**: SPI por bit-bang com CLK=CTS, MOSI=TX, MISO=RX, CS=RTS.
3. Se não achar nenhum, carrega o driver original da BBA.

O W5500 usa o socket 0 em modo MACRAW. Uma thread de recepção consulta o chip,
porque o mod não tem linha de interrupção.

A função do lwIP do dcplaya que recebe cada pacote foi feita para a rotina de
interrupção da BBA. Chamada de uma thread, o `thd_schedule_next` dela misturava
o contexto de duas threads, e o console travava depois de alguns minutos. Por
isso o driver:
- desvia essa chamada para uma versão que só age dentro de interrupção;
- entrega cada pacote com as interrupções desligadas;
- nunca imprime nada nem busca símbolos a partir da thread de recepção.

**Recompilar os plugins** (opcional; as cópias prontas estão em `cdi/prebuilt/`).
É preciso um toolchain `sh-elf` do KallistiOS. Os plugins são ligados em tempo
de execução ao KOS de 2004 que está dentro do executável do dcplaya. Por isso
usam as declarações de `driver/oldkos.h`, e o carregador LEF só aceita
relocações `R_SH_DIR32`.

```bash
cd driver && ./build.sh                 # -> out/BBA.LEZ, out/NETPROBE.LEZ
cp out/BBA.LEZ out/NETPROBE.LEZ ../cdi/prebuilt/
```

## Créditos

- dcplaya de Benjamin Gerard e Vincent Penne (sashipa), port do ffmpeg com
  otimizações para SH4 por Bero.
- A versão VideoCast do dcplaya 2.0b2, por Scrivani.
- Este pacote: driver W5500, DHCP, patches de playlist/pausa/stop/playlists
  remotas, patcher do CD, proxy de TV/YouTube e página de administração.

## Licença

O código deste repositório é distribuído sob a **GNU General Public License
v2.0** (veja [LICENSE](LICENSE)). O dcplaya em si, a imagem original do CD e o
seu conteúdo de IPTV/YouTube não fazem parte deste repositório e seguem os
próprios termos.
