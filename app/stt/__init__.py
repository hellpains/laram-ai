"""Распознавание речи. Провайдер выбирается в настройках (STT_PROVIDER)."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..config import Settings
from ..models import Transcript


class STTProvider(Protocol):
    def transcribe(self, ogg_path: Path, channels: int, duration_s: float) -> Transcript: ...


def get_stt(settings: Settings) -> STTProvider:
    if settings.stt_provider == "yandex":
        from .yandex import YandexSpeechKit

        return YandexSpeechKit(settings)
    if settings.stt_provider == "gigaam":
        from .gigaam import GigaAMSTT

        return GigaAMSTT(settings)
    if settings.stt_provider == "mock":
        from .mock import MockSTT

        return MockSTT()
    # Сюда позже добавится провайдер для чеченского (дообученная модель)
    raise ValueError(f"Неизвестный STT_PROVIDER: {settings.stt_provider}")
