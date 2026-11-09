"""Битрикс24 через входящий вебхук (REST API).

Нужные права входящего вебхука: crm, task, telephony, disk. Документация: https://apidocs.bitrix24.ru/
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import requests

from ..config import Settings
from ..models import OUTCOMES, Summary


class BitrixError(RuntimeError):
    pass


@dataclass
class CrmTarget:
    entity: str  # "deal" или "lead"
    id: int
    responsible_id: int | None
    title: str = ""


@dataclass
class CrmResult:
    status: str  # "ok", "not_found", "skipped"
    message: str
    target: CrmTarget | None = None


class Bitrix:
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self.s = settings
        self.http = session or requests.Session()

    # --- низкоуровневый вызов ---
    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.s.bitrix_webhook_url}/{method}.json"
        for attempt in range(3):
            resp = self.http.post(url, json=params or {}, timeout=30)
            if resp.status_code == 503:  # QUERY_LIMIT_EXCEEDED: Битрикс ограничивает частоту запросов
                time.sleep(1 + attempt)
                continue
            try:
                data = resp.json()
            except ValueError:
                raise BitrixError(f"{method}: HTTP {resp.status_code}, ответ не JSON")
            if "error" in data:
                raise BitrixError(f"{method}: {data.get('error')} {data.get('error_description', '')}".strip())
            return data.get("result")
        raise BitrixError(f"{method}: превышен лимит запросов Битрикс24")

    # --- звонки телефонии ---
    def get_call_stat(self, call_id: str) -> dict[str, Any] | None:
        """Запись о звонке из истории телефонии Битрикс24 (любая АТС, подключённая к порталу)."""
        rows = self.call("voximplant.statistic.get", {"FILTER": {"CALL_ID": call_id}}) or []
        return rows[0] if rows else None

    def record_url(self, stat: dict[str, Any]) -> str | None:
        """Ссылка на файл записи. Записи встроенной телефонии лежат на Диске Битрикс24 (RECORD_FILE_ID),
        внешние АТС обычно отдают прямую ссылку (CALL_RECORD_URL)."""
        file_id = _int(stat.get("RECORD_FILE_ID"))
        if file_id:
            info = self.call("disk.file.get", {"id": file_id}) or {}
            if info.get("DOWNLOAD_URL"):
                return info["DOWNLOAD_URL"]
        url = (stat.get("CALL_RECORD_URL") or "").strip()
        return url or None

    # --- поиск сделки ---
    def find_target(
        self,
        phone: str | None,
        deal_id: str | None,
        entity_type: str | None = None,
        entity_id: str | None = None,
    ) -> CrmTarget | None:
        if deal_id:
            deal = self.call("crm.deal.get", {"id": int(deal_id)})
            if not deal:
                raise BitrixError(f"Сделка {deal_id} не найдена")
            return CrmTarget("deal", int(deal["ID"]), _int(deal.get("ASSIGNED_BY_ID")), deal.get("TITLE", ""))

        # Битрикс сам привязал звонок к сущности CRM — используем это в первую очередь
        kind, owner = (entity_type or "").upper(), _int(entity_id)
        if owner:
            if kind == "DEAL":
                return self.find_target(None, str(owner))
            if kind in ("CONTACT", "COMPANY"):
                deal = self._latest_open_deal(f"{kind}_ID", owner)
                if deal:
                    return CrmTarget("deal", int(deal["ID"]), _int(deal.get("ASSIGNED_BY_ID")), deal.get("TITLE", ""))
            if kind == "LEAD":
                lead = self.call("crm.lead.get", {"id": owner})
                if lead:
                    return CrmTarget("lead", int(lead["ID"]), _int(lead.get("ASSIGNED_BY_ID")), lead.get("TITLE", ""))
        if not phone:
            return None

        found = self.call("crm.duplicate.findbycomm", {"type": "PHONE", "values": normalize_phones(phone)}) or {}
        if isinstance(found, list):  # пустой результат приходит списком
            found = {}
        for entity, key in (("CONTACT", "CONTACT_ID"), ("COMPANY", "COMPANY_ID")):
            for owner_id in found.get(entity, []):
                deal = self._latest_open_deal(key, owner_id)
                if deal:
                    return CrmTarget("deal", int(deal["ID"]), _int(deal.get("ASSIGNED_BY_ID")), deal.get("TITLE", ""))
        for lead_id in found.get("LEAD", []):
            lead = self.call("crm.lead.get", {"id": int(lead_id)})
            if lead:
                return CrmTarget("lead", int(lead["ID"]), _int(lead.get("ASSIGNED_BY_ID")), lead.get("TITLE", ""))
        return None

    def _latest_open_deal(self, key: str, owner_id: Any) -> dict[str, Any] | None:
        deals = self.call(
            "crm.deal.list",
            {
                "filter": {key: owner_id, "CLOSED": "N"},
                "order": {"DATE_MODIFY": "DESC"},
                "select": ["ID", "TITLE", "ASSIGNED_BY_ID"],
            },
        ) or []
        return deals[0] if deals else None

    # --- запись результата ---
    def push_summary(
        self,
        summary: Summary,
        phone: str | None,
        deal_id: str | None,
        call_ref: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        manager_id: str | None = None,
    ) -> CrmResult:
        target = self.find_target(phone, deal_id, entity_type, entity_id)
        if not target:
            return CrmResult("not_found", f"В CRM не найдено сделки или лида по номеру {phone or '—'}")
        # Задачу на следующий шаг получает тот, кто разговаривал, а не ответственный по сделке
        if _int(manager_id):
            target.responsible_id = _int(manager_id)

        if target.entity == "deal":
            fields = {
                self.s.bitrix_field_result: summary.result,
                self.s.bitrix_field_agreements: "\n".join(f"• {a}" for a in summary.agreements),
                self.s.bitrix_field_next_step: summary.next_step,
                self.s.bitrix_field_next_date: summary.next_step_date or "",
            }
            self.call("crm.deal.update", {"id": target.id, "fields": {k: v for k, v in fields.items() if k}})

        self.call(
            "crm.timeline.comment.add",
            {"fields": {"ENTITY_ID": target.id, "ENTITY_TYPE": target.entity, "COMMENT": format_comment(summary, call_ref)}},
        )

        task_note = ""
        if self.s.bitrix_create_task and summary.next_step and target.responsible_id:
            task_id = self._create_task(target, summary)
            task_note = f", задача #{task_id}"
        name = "сделку" if target.entity == "deal" else "лид"
        return CrmResult("ok", f"Записано в {name} #{target.id} «{target.title}»{task_note}", target)

    def _create_task(self, target: CrmTarget, summary: Summary) -> Any:
        fields: dict[str, Any] = {
            "TITLE": f"Следующий шаг: {summary.next_step}"[:250],
            "DESCRIPTION": format_comment(summary, ""),
            "RESPONSIBLE_ID": target.responsible_id,
            "UF_CRM_TASK": [("D_" if target.entity == "deal" else "L_") + str(target.id)],
        }
        if summary.next_step_date:
            fields["DEADLINE"] = f"{summary.next_step_date}T{self.s.bitrix_task_hour:02d}:00:00"
        result = self.call("tasks.task.add", {"fields": fields}) or {}
        return (result.get("task") or {}).get("id", "?")

    # --- настройка портала ---
    def ensure_deal_fields(self) -> list[str]:
        """Создаёт пользовательские поля сделки, если их ещё нет. Возвращает список созданных."""
        wanted = [
            (self.s.bitrix_field_result, "string", "Итог звонка", {"ROWS": 3}),
            (self.s.bitrix_field_agreements, "string", "Договорённости", {"ROWS": 5}),
            (self.s.bitrix_field_next_step, "string", "Следующий шаг", {"ROWS": 2}),
            (self.s.bitrix_field_next_date, "date", "Дата следующего шага", {}),
        ]
        existing = {f["FIELD_NAME"] for f in (self.call("crm.deal.userfield.list", {}) or [])}
        created = []
        for code, kind, label, extra in wanted:
            if not code or code in existing:
                continue
            short = re.sub(r"^UF_CRM_", "", code)
            self.call(
                "crm.deal.userfield.add",
                {
                    "fields": {
                        "FIELD_NAME": short,
                        "USER_TYPE_ID": kind,
                        "EDIT_FORM_LABEL": {"ru": label},
                        "LIST_COLUMN_LABEL": {"ru": label},
                        "LIST_FILTER_LABEL": {"ru": label},
                        "SHOW_FILTER": "Y",
                        "SETTINGS": extra,
                    }
                },
            )
            created.append(code)
        return created


def format_comment(summary: Summary, call_ref: str) -> str:
    lines = ["[B]Конспект звонка[/B]" + (f" ({call_ref})" if call_ref else ""), ""]
    lines.append(f"[B]Итог:[/B] {OUTCOMES.get(summary.outcome, summary.outcome)}")
    if summary.summary:
        lines.append(summary.summary)
    if summary.result:
        lines.append(f"[B]Чего добились:[/B] {summary.result}")
    if summary.agreements:
        lines.append("[B]Договорённости:[/B]")
        lines += [f"• {a}" for a in summary.agreements]
    step = summary.next_step or "не назначен"
    if summary.next_step_date:
        step += f" — до {summary.next_step_date}"
    lines.append(f"[B]Следующий шаг:[/B] {step}")
    if summary.objections:
        lines.append("[B]Возражения:[/B] " + "; ".join(summary.objections))
    if summary.quality_flags:
        lines.append("[B]На разбор РОПу:[/B] " + "; ".join(summary.quality_flags))
    return "\n".join(lines)


def normalize_phones(phone: str) -> list[str]:
    """Битрикс ищет номер в том виде, как он сохранён. Пробуем частые варианты: +7…, 8…, 7…"""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    if len(digits) == 10:
        return [f"+7{digits}", f"8{digits}", f"7{digits}"]
    return [phone.strip()]


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
