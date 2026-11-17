FROM debian:trixie-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server.py /app/server.py
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=4)"
CMD ["python3", "-u", "/app/server.py"]
