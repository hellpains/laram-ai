"""Конвейер обработки звонка: аудио → расшифровка → конспект → Битрикс24."""
from __future__ import annotations

import logging
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from . import db
from .audio import probe, to_ogg_opus
from .config import Settings, settings
from .crm.bitrix import Bitrix
from .llm import get_llm
from .models import Summary, Transcript
from .stt import get_stt

log = logging.getLogger("callnotes")

MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024

_executor: ThreadPoolExecutor | None = None


class RecordNotReady(Exception):
    """Битрикс24 ещё не прикрепил запись к звонку — проверим позже."""


def submit(call_id: str) -> None:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=settings.workers, thread_name_prefix="call")
    _executor.submit(process_call, call_id)


def submit_later(call_id: str, delay_s: float) -> None:
    """Повторная попытка без блокировки рабочих потоков: пока ждём запись, другие звонки обрабатываются."""
    timer = threading.Timer(delay_s, submit, args=(call_id,))
    timer.daemon = True
    timer.start()


def resume_unfinished() -> None:
    """После перезапуска сервера дообрабатываем звонки, которые не успели закончиться."""
    for call_id in db.unfinished():
        db.update(call_id, status="queued")
        submit(call_id)


def process_call(call_id: str, cfg: Settings | None = None) -> None:
    cfg = cfg or settings
    call = db.get(call_id)
    if not call:
        return
    try:
        transcript = _transcribe(call_id, call, cfg)
        db.update(call_id, status="summarizing")
        today = datetime.now(ZoneInfo(cfg.timezone)).date()
        summary = get_llm(cfg).summarize(transcript, today)
        db.update(call_id, summary=summary.to_dict())
        _push_to_crm(call_id, db.get(call_id) or call, summary, cfg)
        db.update(call_id, status="done", error=None)
    except RecordNotReady:
        submit_later(call_id, cfg.record_poll_seconds)
    except Exception as exc:  # noqa: BLE001 — любую ошибку показываем в интерфейсе
        log.error("call %s failed: %s\n%s", call_id, exc, traceback.format_exc())
        db.update(call_id, status="error", error=str(exc)[:1000])


def _transcribe(call_id: str, call: dict, cfg: Settings) -> Transcript:
    if call.get("transcript"):  # повторная обработка: расшифровку не оплачиваем второй раз
        return Transcript.from_dict(call["transcript"])
    if call["source"] == "bitrix" and not call.get("recording_url") and not call.get("audio_path"):
        call = _resolve_bitrix_record(call_id, call, cfg)
    db.update(call_id, status="transcribing")
    src = Path(call["audio_path"]) if call.get("audio_path") else None
    if not src or not src.exists():
        if not call.get("recording_url"):
            raise RuntimeError("Нет ни файла записи, ни ссылки на запись")
        src = _download(call["recording_url"], cfg.data_dir / "audio" / f"{call_id}.src")
        db.update(call_id, audio_path=str(src))
    info = probe(src)
    ogg = to_ogg_opus(src, cfg.data_dir / "audio" / f"{call_id}.ogg", info.channels)
    stt_cfg = replace(cfg, stt_provider=call.get("stt_provider") or cfg.stt_provider)
    transcript = get_stt(stt_cfg).transcribe(ogg, info.channels, info.duration_s)
    db.update(call_id, transcript=transcript.to_dict(), duration_s=round(info.duration_s, 1))
    return transcript


def _resolve_bitrix_record(call_id: str, call: dict, cfg: Settings) -> dict:
    """Ждёт, пока Битрикс24 прикрепит запись к звонку. АТС часто присылают файл через минуту-другую."""
    db.update(call_id, status="waiting_record")
    bx = Bitrix(cfg)
    stat = bx.get_call_stat(call["external_id"])
    url = bx.record_url(stat) if stat else None
    if not url:
        started = call.get("wait_started_at")
        if not started:
            started = datetime.now(timezone.utc).isoformat(timespec="seconds")
            db.update(call_id, wait_started_at=started)
        begin = datetime.fromisoformat(started)
        waited_min = (datetime.now(begin.tzinfo) - begin).total_seconds() / 60
        if waited_min < cfg.record_wait_minutes:
            raise RecordNotReady()
        raise RuntimeError(
            f"Запись звонка не появилась в Битрикс24 за {cfg.record_wait_minutes} мин. "
            "Проверьте, что в телефонии включена запись разговоров и сохранение записей в Битрикс24"
        )
    fields = {"recording_url": url}
    if stat.get("CRM_ENTITY_TYPE") and stat.get("CRM_ENTITY_ID"):
        fields.update(crm_entity_type=str(stat["CRM_ENTITY_TYPE"]), crm_entity_id=str(stat["CRM_ENTITY_ID"]))
    db.update(call_id, **fields)
    return {**call, **fields}


def _push_to_crm(call_id: str, call: dict, summary: Summary, cfg: Settings) -> None:
    if not cfg.bitrix_enabled:
        db.update(call_id, crm_status="skipped", crm_message="Битрикс24 не подключён (BITRIX_WEBHOOK_URL)")
        return
    if not (call.get("phone") or call.get("deal_id") or call.get("crm_entity_id")):
        db.update(call_id, crm_status="skipped", crm_message="Не указан телефон или ID сделки")
        return
    db.update(call_id, status="crm")
    result = Bitrix(cfg).push_summary(
        summary, call.get("phone"), call.get("deal_id"), f"звонок {call_id}",
        entity_type=call.get("crm_entity_type"), entity_id=call.get("crm_entity_id"),
        manager_id=call.get("manager_id"),
    )
    db.update(call_id, crm_status=result.status, crm_message=result.message,
              deal_id=str(result.target.id) if result.target and result.target.entity == "deal" else call.get("deal_id"))


def _download(url: str, dst: Path) -> Path:
    with requests.get(url, stream=True, timeout=(10, 120)) as resp:
        resp.raise_for_status()
        size = 0
        with dst.open("wb") as f:
            for chunk in resp.iter_content(1 << 16):
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError("Запись слишком большая (больше 200 МБ)")
                f.write(chunk)
    return dst
