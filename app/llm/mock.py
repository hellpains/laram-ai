"""Демо-конспект без обращения к LLM. Даты считаются от сегодняшнего дня, как у настоящей модели."""
from __future__ import annotations

from datetime import date, timedelta

from ..models import Summary, Transcript


def _next_weekday(today: date, weekday: int) -> date:
    days = (weekday - today.weekday()) % 7 or 7
    return today + timedelta(days=days)


class MockLLM:
    def summarize(self, transcript: Transcript, today: date) -> Summary:
        return Summary(
            manager_speaker="A",
            summary=(
                "Клиент оставлял заявку на газоблок для дома около 120 м² с доставкой в Аргун. "
                "Менеджер назвал ориентировочную цену и отработал возражение о цене скидкой 3%."
            ),
            result="Клиент получит расчёт и обсудит покупку с семьёй.",
            agreements=[
                "Менеджер отправит расчёт в WhatsApp сегодня до 18:00",
                "Скидка 3% при оплате до конца недели",
                "Созвон в четверг",
            ],
            next_step="Позвонить клиенту и обсудить расчёт",
            next_step_date=_next_weekday(today, 3).isoformat(),
            outcome="in_progress",
            objections=["У конкурентов дешевле"],
            quality_flags=["Не уточнил, кто принимает решение, до разговора о цене"],
        )
