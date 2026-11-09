# Базовый образ можно взять с зеркала, если Docker Hub недоступен: --build-arg BASE_IMAGE=mirror.gcr.io/library/python:3.12-slim
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

# WITH_GIGAAM=1 — добавить локальную модель распознавания GigaAM-v3 (образ станет больше примерно на 1,5 ГБ)
ARG WITH_GIGAAM=0
ARG GIGAAM_MODEL=v3_e2e_rnnt

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg $( [ "$WITH_GIGAAM" = "1" ] && echo git ) \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# PyTorch для процессора (без видеокарты) и GigaAM из официального репозитория Сбера
RUN if [ "$WITH_GIGAAM" = "1" ]; then \
      pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu \
      && git clone --depth 1 https://github.com/salute-developers/GigaAM.git /opt/GigaAM \
      && pip install --no-cache-dir -e /opt/GigaAM ; \
    fi

# Сервис работает не от root: записи звонков — персональные данные
RUN useradd --system --uid 10001 --create-home --home-dir /home/callnotes callnotes \
    && mkdir -p /data && chown callnotes:callnotes /data
USER callnotes

COPY --chown=callnotes:callnotes scripts ./scripts
# Веса модели скачиваются при сборке, а не при первом звонке
RUN if [ "$WITH_GIGAAM" = "1" ]; then python scripts/download_gigaam.py "$GIGAAM_MODEL"; fi

COPY --chown=callnotes:callnotes app ./app

ENV DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# Один процесс: очередь обработки живёт внутри него. Потоки обслуживают веб-запросы.
CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:8000", "--timeout", "300", \
     "--access-logfile", "-", "app.main:app"]
