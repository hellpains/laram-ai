"""GigaAM-v3 (Сбер, лицензия MIT) — локальное распознавание русской речи, без отправки записей в облако.

Модель понимает фрагменты до 25 секунд, поэтому запись режется по паузам на куски до CHUNK_MAX_S.
Стерео-запись (менеджер и клиент в разных каналах) распознаётся по каждому каналу отдельно.
Исходники модели: https://github.com/salute-developers/GigaAM
"""
from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

from ..config import Settings
from ..models import Segment, Transcript

log = logging.getLogger("callnotes")

CHUNK_MAX_S = 22.0  # запас до предела модели в 25 с
MIN_CHUNK_S = 0.4
SILENCE_DB = "-35dB"
SILENCE_MIN_S = 0.35
SPEAKERS = "ABCDEFGH"

_model: Any = None
_model_lock = threading.Lock()  # модель одна на процесс; распознавание идёт по очереди


def _load_model(name: str) -> Any:
    global _model
    if _model is None:
        import gigaam  # импорт здесь: пакет ставится только в сборке с GigaAM

        log.info("Загружаю модель GigaAM %s…", name)
        try:
            _model = gigaam.load_model(name, device="cpu")
        except TypeError:  # старые версии пакета без параметра device
            _model = gigaam.load_model(name)
    return _model


class GigaAMSTT:
    def __init__(self, settings: Settings, model: Any = None):
        self.model_name = settings.gigaam_model
        self._model = model  # для тестов можно передать готовую модель

    def transcribe(self, ogg_path: Path, channels: int, duration_s: float) -> Transcript:
        segments: list[Segment] = []
        with tempfile.TemporaryDirectory(prefix="gigaam-") as tmp:
            tmpdir = Path(tmp)
            for ch in range(2 if channels >= 2 else 1):
                wav = tmpdir / f"ch{ch}.wav"
                _extract_channel(ogg_path, wav, ch if channels >= 2 else None)
                total = _duration(wav) or duration_s
                for start, end in plan_chunks(detect_silences(wav), total):
                    piece = tmpdir / f"ch{ch}_{int(start * 1000)}.wav"
                    _cut(wav, piece, start, end)
                    text = self._recognize(piece)
                    if text:
                        segments.append(Segment(SPEAKERS[ch], int(start * 1000), text))
                    piece.unlink(missing_ok=True)
        segments.sort(key=lambda s: s.start_ms)
        return Transcript(segments=segments, duration_s=duration_s, language=f"ru (GigaAM {self.model_name})")

    def _recognize(self, wav: Path) -> str:
        with _model_lock:
            model = self._model or _load_model(self.model_name)
            result = model.transcribe(str(wav))
        text = result if isinstance(result, str) else getattr(result, "text", "") or ""
        return re.sub(r"\s+", " ", str(text)).strip()


# ---------- работа со звуком ----------

def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=600)


def _extract_channel(src: Path, dst: Path, channel: int | None) -> None:
    """Один канал (или моно-микс) в WAV 16 кГц — формат, на котором обучена модель."""
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src)]
    if channel is not None:
        cmd += ["-af", f"pan=mono|c0=c{channel}"]
    cmd += ["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)]
    _run(cmd)


def _duration(wav: Path) -> float:
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(wav)]).stdout
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


def _cut(src: Path, dst: Path, start: float, end: float) -> None:
    _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
          "-i", str(src), "-c:a", "pcm_s16le", str(dst)])


def detect_silences(wav: Path) -> list[tuple[float, float]]:
    """Паузы через фильтр ffmpeg silencedetect: список (начало, конец) в секундах."""
    proc = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", str(wav), "-af", f"silencedetect=noise={SILENCE_DB}:d={SILENCE_MIN_S}",
         "-f", "null", "-"],
        capture_output=True, text=True, timeout=600,
    )
    silences, start = [], None
    for line in proc.stderr.splitlines():
        if m := re.search(r"silence_start: (-?[\d.]+)", line):
            start = max(0.0, float(m.group(1)))
        elif (m := re.search(r"silence_end: ([\d.]+)", line)) and start is not None:
            silences.append((start, float(m.group(1))))
            start = None
    if start is not None:  # пауза до конца файла
        silences.append((start, float("inf")))
    return silences


def plan_chunks(silences: list[tuple[float, float]], total: float, max_len: float = CHUNK_MAX_S) -> list[tuple[float, float]]:
    """Режет запись на куски не длиннее max_len, по возможности посередине пауз. Чистые паузы пропускает."""
    if total <= 0:
        return []
    # Участки речи — всё, что между паузами
    speech, pos = [], 0.0
    for s, e in sorted(silences):
        if s > pos:
            speech.append((pos, min(s, total)))
        pos = max(pos, min(e, total))
    if pos < total:
        speech.append((pos, total))
    speech = [(s, e) for s, e in speech if e - s >= 0.05]

    chunks: list[tuple[float, float]] = []
    cur_start: float | None = None
    cur_end = 0.0
    for s, e in speech:
        # Слишком длинная фраза без пауз — режем на равные части
        while e - s > max_len:
            if cur_start is not None:
                chunks.append((cur_start, cur_end))
                cur_start = None
            chunks.append((s, s + max_len))
            s += max_len
        if cur_start is None:
            cur_start, cur_end = s, e
        elif e - cur_start <= max_len:
            cur_end = e
        else:
            chunks.append((cur_start, cur_end))
            cur_start, cur_end = s, e
    if cur_start is not None:
        chunks.append((cur_start, cur_end))

    # Небольшой запас по краям, чтобы не обрезать начало и конец слов
    padded = []
    for s, e in chunks:
        s2, e2 = max(0.0, s - 0.15), min(total, e + 0.15)
        if e2 - s2 > max_len + 0.3:
            s2, e2 = s, e
        if e2 - s2 >= MIN_CHUNK_S:
            padded.append((round(s2, 3), round(e2, 3)))
    return padded
