"""Конспект звонка с помощью LLM. Провайдер выбирается в настройках (LLM_PROVIDER)."""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Protocol

from ..config import Settings
from ..models import Summary, Transcript


class LLMProvider(Protocol):
    def summarize(self, transcript: Transcript, today: date) -> Summary: ...


def get_llm(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "yandex":
        from .yandexgpt import YandexGPT

        return YandexGPT(settings)
    if settings.llm_provider == "mock":
        from .mock import MockLLM

        return MockLLM()
    raise ValueError(f"Неизвестный LLM_PROVIDER: {settings.llm_provider}")


def extract_json(text: str) -> dict[str, Any]:
    """Достаёт JSON из ответа модели, даже если он обёрнут в ```json ... ``` или окружён текстом."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"Модель вернула не JSON: {text[:200]}")
        data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("Модель вернула JSON, но не объект")
    return data
