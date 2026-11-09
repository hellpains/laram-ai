"""Подготовка аудио: любой формат → OGG Opus для SpeechKit."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


class AudioError(RuntimeError):
    pass


@dataclass
class AudioInfo:
    duration_s: float
    channels: int


def probe(path: Path) -> AudioInfo:
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=channels:format=duration", "-of", "json", str(path),
            ],
            capture_output=True, text=True, check=True, timeout=60,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise AudioError(f"Не удалось прочитать аудиофайл: {exc}") from exc
    data = json.loads(out or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise AudioError("В файле нет звуковой дорожки")
    duration = float((data.get("format") or {}).get("duration") or 0)
    return AudioInfo(duration_s=duration, channels=int(streams[0].get("channels") or 1))


def to_ogg_opus(src: Path, dst: Path, channels: int) -> Path:
    """Перекодирует в OGG Opus 48 кГц. Стерео сохраняется: у многих АТС менеджер и клиент в разных каналах."""
    ch = "2" if channels >= 2 else "1"
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-i", str(src),
        "-vn", "-ac", ch, "-ar", "48000", "-c:a", "libopus", "-b:a", "48k" if ch == "2" else "32k",
        str(dst),
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=600)
    except subprocess.CalledProcessError as exc:
        raise AudioError(f"Ошибка перекодирования: {exc.stderr.strip()[:300]}") from exc
    except FileNotFoundError as exc:
        raise AudioError("ffmpeg не установлен на сервере") from exc
    return dst
