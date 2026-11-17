FROM debian:trixie-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg python3 python3-pip ca-certificates curl unzip \
 && rm -rf /var/lib/apt/lists/*
# YouTube channels: yt-dlp plus deno (YouTube needs a JavaScript runtime)
RUN arch="$(dpkg --print-architecture)" \
 && case "$arch" in amd64) d=x86_64 ;; arm64) d=aarch64 ;; *) d="" ;; esac \
 && if [ -n "$d" ]; then \
      curl -fsSL "https://github.com/denoland/deno/releases/latest/download/deno-$d-unknown-linux-gnu.zip" -o /tmp/deno.zip \
      && unzip -q /tmp/deno.zip -d /usr/local/bin && rm /tmp/deno.zip; \
    fi \
 && pip3 install --no-cache-dir --break-system-packages "yt-dlp[default]"
WORKDIR /app
COPY server.py admin.html /app/
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s --start-period=120s CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=4)"
# YouTube changes often: refresh yt-dlp on every start (set YTDLP_UPDATE=0 to skip)
CMD ["sh", "-c", "if [ \"${YTDLP_UPDATE:-1}\" = 1 ]; then timeout 90 pip3 install -q --no-cache-dir --break-system-packages -U 'yt-dlp[default]' || true; fi; exec python3 -u /app/server.py"]
