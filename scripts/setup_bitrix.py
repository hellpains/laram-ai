"""Проверяет вебхук Битрикс24 и создаёт поля сделки под конспект.

Запуск: python -m scripts.setup_bitrix
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.crm.bitrix import Bitrix, BitrixError  # noqa: E402


def main() -> int:
    if not settings.bitrix_enabled:
        print("Заполните BITRIX_WEBHOOK_URL в файле .env")
        return 1
    bx = Bitrix(settings)
    try:
        scope = bx.call("scope") or []
        missing = {"crm", "task", "telephony", "disk"} - set(scope)
        if missing:
            print(f"У вебхука нет прав: {', '.join(sorted(missing))}. Добавьте их в настройках вебхука.")
            return 1
        created = bx.ensure_deal_fields()
    except BitrixError as exc:
        print(f"Ошибка Битрикс24: {exc}")
        return 1
    if created:
        print("Созданы поля сделки:", ", ".join(created))
    else:
        print("Все поля уже есть.")
    print("Готово. Вынесите новые поля в карточку сделки, чтобы менеджеры их видели.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
