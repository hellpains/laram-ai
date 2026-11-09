"""Настройки сервиса. Читаются из переменных окружения и файла .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Минимальный загрузчик .env: KEY=VALUE, комментарии через #."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


_load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    value = _env(name)
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "on", "да"}


@dataclass
class Settings:
    # Общее
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", str(BASE_DIR / "data"))))
    app_password: str = field(default_factory=lambda: _env("APP_PASSWORD"))
    webhook_secret: str = field(default_factory=lambda: _env("WEBHOOK_SECRET"))
    timezone: str = field(default_factory=lambda: _env("TIMEZONE", "Europe/Moscow"))
    workers: int = field(default_factory=lambda: int(_env("WORKERS", "2")))

    # Yandex Cloud (AI Studio)
    yandex_api_key: str = field(default_factory=lambda: _env("YANDEX_API_KEY"))
    yandex_folder_id: str = field(default_factory=lambda: _env("YANDEX_FOLDER_ID"))
    yandex_stt_model: str = field(default_factory=lambda: _env("YANDEX_STT_MODEL", "general"))
    yandex_stt_languages: list[str] = field(
        default_factory=lambda: [x.strip() for x in _env("YANDEX_STT_LANGUAGES", "ru-RU").split(",") if x.strip()]
    )
    yandex_gpt_model: str = field(default_factory=lambda: _env("YANDEX_GPT_MODEL", "yandexgpt/latest"))
    yandex_gpt_url: str = field(
        default_factory=lambda: _env("YANDEX_GPT_URL", "https://ai.api.cloud.yandex.net/foundationModels/v1/completion")
    )

    # GigaAM: локальная модель распознавания (STT_PROVIDER=gigaam)
    gigaam_model: str = field(default_factory=lambda: _env("GIGAAM_MODEL", "v3_e2e_rnnt"))

    # Провайдеры: yandex | gigaam (только распознавание) | mock. Пусто = yandex при наличии ключей, иначе mock (демо-режим).
    stt_provider: str = field(default_factory=lambda: _env("STT_PROVIDER"))
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER"))

    # Битрикс24
    bitrix_webhook_url: str = field(default_factory=lambda: _env("BITRIX_WEBHOOK_URL").rstrip("/"))
    bitrix_field_result: str = field(default_factory=lambda: _env("BITRIX_FIELD_RESULT", "UF_CRM_CALL_RESULT"))
    bitrix_field_agreements: str = field(default_factory=lambda: _env("BITRIX_FIELD_AGREEMENTS", "UF_CRM_CALL_AGREEMENTS"))
    bitrix_field_next_step: str = field(default_factory=lambda: _env("BITRIX_FIELD_NEXT_STEP", "UF_CRM_CALL_NEXT_STEP"))
    bitrix_field_next_date: str = field(default_factory=lambda: _env("BITRIX_FIELD_NEXT_DATE", "UF_CRM_CALL_NEXT_DATE"))
    bitrix_create_task: bool = field(default_factory=lambda: _bool("BITRIX_CREATE_TASK", True))
    bitrix_task_hour: int = field(default_factory=lambda: int(_env("BITRIX_TASK_HOUR", "12")))

    # Приём звонков из Битрикс24 (исходящий вебхук на событие ONVOXIMPLANTCALLEND)
    bitrix_app_token: str = field(default_factory=lambda: _env("BITRIX_APP_TOKEN"))
    min_call_seconds: int = field(default_factory=lambda: int(_env("MIN_CALL_SECONDS", "20")))
    record_wait_minutes: int = field(default_factory=lambda: int(_env("RECORD_WAIT_MINUTES", "15")))
    record_poll_seconds: int = field(default_factory=lambda: int(_env("RECORD_POLL_SECONDS", "30")))

    def __post_init__(self) -> None:
        has_yandex = bool(self.yandex_api_key and self.yandex_folder_id)
        if not self.stt_provider:
            self.stt_provider = "yandex" if has_yandex else "mock"
        if not self.llm_provider:
            self.llm_provider = "yandex" if has_yandex else "mock"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "audio").mkdir(exist_ok=True)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "calls.db"

    @property
    def bitrix_enabled(self) -> bool:
        return bool(self.bitrix_webhook_url)

    @property
    def stt_label(self) -> str:
        return {"yandex": "Yandex SpeechKit", "gigaam": "GigaAM-v3 (локально)", "mock": "демо"}.get(
            self.stt_provider, self.stt_provider
        )

    @property
    def demo_mode(self) -> bool:
        return self.stt_provider == "mock" or self.llm_provider == "mock"


settings = Settings()
