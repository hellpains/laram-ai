"""YandexGPT через Foundation Models API (Yandex AI Studio).

Документация: https://aistudio.yandex.ru/docs/ru/ai-studio/text-generation/api-ref/TextGeneration/completion
"""
from __future__ import annotations

import time
from datetime import date

import requests

from ..config import Settings
from ..models import Summary, Transcript
from . import extract_json
from .prompt import build_system_prompt, build_user_prompt


class LLMError(RuntimeError):
    pass


class YandexGPT:
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self.s = settings
        self.http = session or requests.Session()
        self.http.headers.update(
            {"Authorization": f"Api-Key {settings.yandex_api_key}", "x-folder-id": settings.yandex_folder_id}
        )

    def summarize(self, transcript: Transcript, today: date) -> Summary:
        if not transcript.segments:
            return Summary(summary="В записи не распознано речи.", outcome="no_contact")
        body = {
            "modelUri": f"gpt://{self.s.yandex_folder_id}/{self.s.yandex_gpt_model}",
            "completionOptions": {"stream": False, "temperature": 0.1, "maxTokens": "2000"},
            "messages": [
                {"role": "system", "text": build_system_prompt(today)},
                {"role": "user", "text": build_user_prompt(transcript.as_dialog(), transcript.duration_s)},
            ],
            "jsonObject": True,
        }
        last_error = ""
        for attempt in range(3):
            resp = self.http.post(self.s.yandex_gpt_url, json=body, timeout=120)
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code != 200:
                raise LLMError(f"YandexGPT: HTTP {resp.status_code} {resp.text[:300]}")
            text = parse_completion(resp.json())
            try:
                return Summary.from_llm(extract_json(text))
            except ValueError as exc:
                last_error = str(exc)  # модель ответила не по формату — пробуем ещё раз
        raise LLMError(f"YandexGPT не дал корректный ответ: {last_error}")


def parse_completion(data: dict) -> str:
    # REST-ответ бывает как {"result": {"alternatives": [...]}} так и {"alternatives": [...]}
    payload = data.get("result", data)
    alternatives = payload.get("alternatives") or []
    if not alternatives:
        raise LLMError(f"YandexGPT вернул пустой ответ: {str(data)[:300]}")
    alt = alternatives[0]
    if alt.get("status") == "ALTERNATIVE_STATUS_CONTENT_FILTER":
        raise LLMError("YandexGPT отказался обрабатывать текст (фильтр содержимого)")
    return (alt.get("message") or {}).get("text", "")
