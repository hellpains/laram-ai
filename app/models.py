"""Структуры данных: расшифровка и конспект звонка."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

OUTCOMES = {
    "success": "Цель звонка достигнута",
    "in_progress": "Есть движение, нужен следующий контакт",
    "refused": "Клиент отказался",
    "no_contact": "Разговор не состоялся",
}


@dataclass
class Segment:
    speaker: str  # метка канала или говорящего: "A", "B"
    start_ms: int
    text: str


@dataclass
class Transcript:
    segments: list[Segment]
    duration_s: float = 0.0
    language: str = "ru-RU"

    def as_dialog(self) -> str:
        """Текст для LLM: [мм:сс] Собеседник A: реплика."""
        lines = []
        for s in self.segments:
            m, sec = divmod(s.start_ms // 1000, 60)
            lines.append(f"[{m:02d}:{sec:02d}] Собеседник {s.speaker}: {s.text}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Transcript":
        return cls(
            segments=[Segment(**s) for s in data.get("segments", [])],
            duration_s=data.get("duration_s", 0.0),
            language=data.get("language", "ru-RU"),
        )


@dataclass
class Summary:
    summary: str = ""
    result: str = ""
    agreements: list[str] = field(default_factory=list)
    next_step: str = ""
    next_step_date: str | None = None  # YYYY-MM-DD
    outcome: str = "in_progress"
    objections: list[str] = field(default_factory=list)
    quality_flags: list[str] = field(default_factory=list)
    manager_speaker: str | None = None

    @classmethod
    def from_llm(cls, data: dict[str, Any]) -> "Summary":
        """Нормализует ответ модели: приводит типы, отбрасывает мусор."""

        def text(v: Any) -> str:
            return str(v).strip() if v not in (None, "null") else ""

        def items(v: Any) -> list[str]:
            if isinstance(v, str):
                v = [v] if v.strip() else []
            if not isinstance(v, list):
                return []
            return [text(x) for x in v if text(x)]

        date = text(data.get("next_step_date"))
        if not _is_iso_date(date):
            date = None
        outcome = text(data.get("outcome")) or "in_progress"
        if outcome not in OUTCOMES:
            outcome = "in_progress"
        manager = text(data.get("manager_speaker")) or None
        return cls(
            summary=text(data.get("summary")),
            result=text(data.get("result")),
            agreements=items(data.get("agreements")),
            next_step=text(data.get("next_step")),
            next_step_date=date,
            outcome=outcome,
            objections=items(data.get("objections")),
            quality_flags=items(data.get("quality_flags")),
            manager_speaker=manager,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_iso_date(value: str) -> bool:
    from datetime import date

    try:
        date.fromisoformat(value)
        return len(value) == 10
    except ValueError:
        return False
