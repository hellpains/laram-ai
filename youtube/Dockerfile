# Скачивание звука с YouTube: yt-dlp + ffmpeg + Deno (yt-dlp требует JS-движок для YouTube с конца 2025 года)
FROM mirror.gcr.io/library/python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -U "yt-dlp[default]" deno \
    && yt-dlp --version && deno --version

COPY run.sh /run.sh
RUN chmod +x /run.sh
WORKDIR /out
ENTRYPOINT ["/run.sh"]
