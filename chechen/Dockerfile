# Тестовый стенд распознавания чеченской речи: Omnilingual ASR (Meta, Apache 2.0), язык che_Cyrl.
# Полный образ Python (не slim): в нём есть компилятор C++, нужный для сборки библиотеки kenlm.
FROM mirror.gcr.io/library/python:3.11-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libsndfile1 build-essential cmake zlib1g-dev libbz2-dev liblzma-dev libboost-program-options-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip setuptools wheel
# kenlm на PyPI есть только в исходниках и собирается из них — ставим отдельным шагом, чтобы ошибку было видно сразу
RUN pip install --no-cache-dir kenlm
# fairseq2 0.6 собран под torch 2.8.0 и numpy 1.26. Новые torchaudio, pyarrow и pandas с ними несовместимы,
# поэтому фиксируем версии. Тяжёлый torch — отдельным шагом, чтобы при повторной сборке не качать его заново.
RUN pip install --no-cache-dir --default-timeout=300 --retries 10 "torch==2.8.0" "torchaudio==2.8.0"
RUN pip install --no-cache-dir --default-timeout=300 --retries 10 \
        omnilingual-asr "torch==2.8.0" "torchaudio==2.8.0" "numpy<2" "pyarrow>=20,<21" "pandas<3"
# Проверка совместимости прямо при сборке
RUN python -c "import numpy, pyarrow, torch, torchaudio; print('numpy', numpy.__version__, 'pyarrow', pyarrow.__version__, 'torch', torch.__version__, 'torchaudio', torchaudio.__version__); import omnilingual_asr.models.inference.pipeline; print('omnilingual-asr OK')"

# Рецепты обучения есть только в репозитории (в pip-пакет не входят). Фиксируем проверенную версию.
RUN git clone https://github.com/facebookresearch/omnilingual-asr.git /opt/oasr \
    && cd /opt/oasr && git checkout 81f51e2 \
    && pip install --no-cache-dir --no-deps -e /opt/oasr \
    && python -c "import omnilingual_asr, workflows; print('рецепты обучения OK')"
ENV OASR_DIR=/opt/oasr PYTHONPATH=/opt/oasr
# Рецепт обучения пишет графики обучения в TensorBoard — без него не стартует
RUN pip install --no-cache-dir tensorboard "numpy<2" \
    && python -c "import numpy, torch.utils.tensorboard; print('tensorboard OK, numpy', numpy.__version__)"

# Русская модель GigaAM-v3 (Сбер, MIT) — для русских слов в смешанной речи.
# Ставим без её зависимостей (она просит numpy 2, а стенд держит numpy 1.26), нужные ставим сами.
RUN git clone https://github.com/salute-developers/GigaAM.git /opt/GigaAM \
    && cd /opt/GigaAM && git checkout 7447938 \
    && pip install --no-cache-dir --no-deps -e /opt/GigaAM \
    && pip install --no-cache-dir "hydra-core==1.3.*" "omegaconf==2.3.*" sentencepiece soundfile tqdm "numpy<2" \
    && python -c "import gigaam, numpy; print('GigaAM OK, numpy', numpy.__version__)"

WORKDIR /app
COPY transcribe.py .
ENV PYTHONUNBUFFERED=1
# Модели скачиваются при первом запуске в папку кэша (подключается как том, чтобы не качать повторно)
ENTRYPOINT ["python", "transcribe.py"]
