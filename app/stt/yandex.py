"""Yandex SpeechKit, асинхронное распознавание API v3.

Документация: https://aistudio.yandex.ru/docs/ru/speechkit/stt/api/transcribation-api-v3
Аудио передаётся прямо в запросе (поле content, base64), без Object Storage.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any, Iterator

import requests

from ..config import Settings
from ..models import Segment, Transcript

STT_URL = "https://stt.api.cloud.yandex.net:443/stt/v3/recognizeFileAsync"
RESULT_URL = "https://stt.api.cloud.yandex.net:443/stt/v3/getRecognition"
OPERATION_URL = "https://operation.api.cloud.yandex.net/operations/{id}"

SPEAKERS = "ABCDEFGH"


class STTError(RuntimeError):
    pass


class YandexSpeechKit:
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self.s = settings
        self.http = session or requests.Session()
        self.http.headers.update(
            {"Authorization": f"Api-Key {settings.yandex_api_key}", "x-folder-id": settings.yandex_folder_id}
        )

    def transcribe(self, ogg_path: Path, channels: int, duration_s: float) -> Transcript:
        operation_id = self._start(ogg_path, channels)
        self._wait(operation_id, timeout_s=max(300, int(duration_s * 4)))
        resp = self.http.get(RESULT_URL, params={"operation_id": operation_id}, timeout=120)
        if resp.status_code != 200:
            raise STTError(f"SpeechKit getRecognition: HTTP {resp.status_code} {resp.text[:300]}")
        segments = parse_recognition(resp.text)
        return Transcript(segments=segments, duration_s=duration_s, language=",".join(self.s.yandex_stt_languages) + " (Yandex SpeechKit)")

    def _start(self, ogg_path: Path, channels: int) -> str:
        body: dict[str, Any] = {
            "content": base64.b64encode(ogg_path.read_bytes()).decode(),
            "recognitionModel": {
                "model": self.s.yandex_stt_model,
                "audioFormat": {"containerAudio": {"containerAudioType": "OGG_OPUS"}},
                "textNormalization": {
                    "textNormalization": "TEXT_NORMALIZATION_ENABLED",
                    "literatureText": True,
                    "profanityFilter": False,
                },
                "languageRestriction": {
                    "restrictionType": "WHITELIST",
                    "languageCode": self.s.yandex_stt_languages,
                },
            },
        }
        if channels < 2:
            # Моно: просим модель различать говорящих. В стерео говорящие уже разделены каналами.
            body["speakerLabeling"] = {"speakerLabeling": "SPEAKER_LABELING_ENABLED"}
        resp = self.http.post(STT_URL, json=body, timeout=120)
        if resp.status_code != 200:
            raise STTError(f"SpeechKit recognizeFileAsync: HTTP {resp.status_code} {resp.text[:300]}")
        operation_id = resp.json().get("id")
        if not operation_id:
            raise STTError(f"SpeechKit не вернул id операции: {resp.text[:300]}")
        return operation_id

    def _wait(self, operation_id: str, timeout_s: int) -> None:
        deadline = time.monotonic() + timeout_s
        delay = 2.0
        while time.monotonic() < deadline:
            resp = self.http.get(OPERATION_URL.format(id=operation_id), timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("error"):
                    raise STTError(f"SpeechKit: {data['error'].get('message', data['error'])}")
                if data.get("done"):
                    return
            elif resp.status_code >= 500:
                pass  # временная ошибка, ждём дальше
            else:
                raise STTError(f"SpeechKit operations: HTTP {resp.status_code} {resp.text[:300]}")
            time.sleep(delay)
            delay = min(delay * 1.5, 15)
        raise STTError(f"SpeechKit не успел распознать запись за {timeout_s} с")


def iter_json_objects(text: str) -> Iterator[dict[str, Any]]:
    """getRecognition отдаёт поток JSON-объектов подряд (не массив). Разбираем по одному."""
    decoder = json.JSONDecoder()
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in " \r\n\t,":
            i += 1
        if i >= n:
            break
        obj, i = decoder.raw_decode(text, i)
        if isinstance(obj, dict):
            yield obj


def parse_recognition(text: str) -> list[Segment]:
    """Собирает реплики. Берём нормализованный текст (finalRefinement), иначе сырой (final)."""
    refined: list[tuple[int, str, str]] = []
    raw: list[tuple[int, str, str]] = []
    for obj in iter_json_objects(text):
        result = obj.get("result", obj)
        channel = str(result.get("channelTag", "0"))
        if "finalRefinement" in result:
            alts = ((result["finalRefinement"].get("normalizedText") or {}).get("alternatives")) or []
            target = refined
        elif "final" in result:
            alts = result["final"].get("alternatives") or []
            target = raw
        else:
            continue
        if not alts:
            continue
        alt = alts[0]
        phrase = (alt.get("text") or "").strip()
        if not phrase:
            continue
        start = int(alt.get("startTimeMs") or (alt.get("words") or [{}])[0].get("startTimeMs") or 0)
        target.append((start, channel, phrase))

    items = sorted(refined or raw, key=lambda x: x[0])
    channels = sorted({c for _, c, _ in items}, key=lambda c: (len(c), c))
    label = {c: SPEAKERS[i % len(SPEAKERS)] for i, c in enumerate(channels)}
    return [Segment(speaker=label[c], start_ms=start, text=phrase) for start, c, phrase in items]
