"""Веб-сервис: загрузка записей, просмотр конспектов, вебхук для телефонии."""
from __future__ import annotations

import hmac
import importlib.util
import logging
import re
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from . import db, pipeline
from .config import settings

ALLOWED_EXT = {".mp3", ".wav", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".amr", ".flac", ".webm", ".mp4", ".3gp"}
STATIC_DIR = Path(__file__).parent / "static"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app(start_workers: bool = True) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
    db.init()
    if start_workers:
        pipeline.resume_unfinished()

    @app.before_request
    def require_password():
        # Пароль защищает интерфейс и API: там персональные данные клиентов.
        if not settings.app_password or request.path in ("/health",) or request.path.startswith("/webhooks/"):
            return None
        auth = request.authorization
        if auth and hmac.compare_digest(auth.password or "", settings.app_password):
            return None
        return Response("Нужен пароль", 401, {"WWW-Authenticate": 'Basic realm="CallNotes"'})

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/api/status")
    def status():
        return {
            "stt": settings.stt_provider,
            "stt_label": settings.stt_label,
            "stt_available": available_stt(),
            "llm": settings.llm_provider,
            "bitrix": settings.bitrix_enabled,
            "demo": settings.demo_mode,
        }

    @app.post("/api/calls")
    def upload_call():
        file = request.files.get("file")
        if not file or not file.filename:
            return _error("Прикрепите файл записи", 400)
        ext = Path(file.filename).suffix.lower()
        if ext not in ALLOWED_EXT:
            return _error(f"Формат {ext or 'без расширения'} не поддерживается", 400)
        phone = (request.form.get("phone") or "").strip() or None
        deal_id = (request.form.get("deal_id") or "").strip() or None
        if deal_id and not deal_id.isdigit():
            return _error("ID сделки — это число из адреса сделки в Битрикс24", 400)

        call_id = db.create_call("upload", filename=file.filename, phone=phone, deal_id=deal_id)
        path = settings.data_dir / "audio" / f"{call_id}{ext}"
        file.save(path)
        db.update(call_id, audio_path=str(path))
        pipeline.submit(call_id)
        return jsonify({"id": call_id}), 201

    @app.get("/api/calls")
    def list_calls():
        return jsonify([_public(c) for c in db.list_calls()])

    @app.get("/api/calls/<call_id>")
    def get_call(call_id: str):
        call = db.get(call_id)
        return jsonify(_public(call)) if call else _error("Звонок не найден", 404)

    @app.post("/api/calls/<call_id>/retry")
    def retry(call_id: str):
        call = db.get(call_id)
        if not call:
            return _error("Звонок не найден", 404)
        if call["status"] not in db.FINAL:
            return _error("Звонок ещё обрабатывается", 409)
        fields = {"status": "queued", "error": None, "wait_started_at": None}
        if request.args.get("fresh"):
            # Распознать заново текущей моделью — для сравнения моделей на одной записи
            if not call.get("audio_path") and not call.get("recording_url") and call["source"] != "bitrix":
                return _error("Файл записи не сохранился, распознать заново нельзя", 409)
            fields["transcript"] = None
            stt = request.args.get("stt")
            if stt:
                if stt not in available_stt():
                    return _error(f"Модель распознавания «{stt}» не подключена", 400)
                fields["stt_provider"] = stt
        db.update(call_id, **fields)
        pipeline.submit(call_id)
        return {"id": call_id}

    @app.post("/webhooks/call")
    def telephony_webhook():
        """Универсальный вход для телефонии: {"recording_url", "phone", "deal_id"?, "call_id"?}.

        Под конкретную АТС (Mango, Zadarma и т.д.) пишется маленький адаптер,
        который переводит её уведомление в этот формат.
        """
        if not settings.webhook_secret:
            return _error("Вебхук выключен: задайте WEBHOOK_SECRET", 403)
        token = request.headers.get("X-Webhook-Secret") or request.args.get("secret") or ""
        if not hmac.compare_digest(token, settings.webhook_secret):
            return _error("Неверный секрет", 403)
        data = request.get_json(silent=True) or request.form.to_dict()
        url = (data.get("recording_url") or "").strip()
        if not url.startswith(("https://", "http://")):
            return _error("Нужна ссылка на запись в поле recording_url", 400)
        external_id = (str(data.get("call_id")) if data.get("call_id") else None)
        if external_id and (existing := db.find_by_external_id(external_id)):
            return {"id": existing, "duplicate": True}
        call_id = db.create_call(
            "webhook",
            recording_url=url,
            external_id=external_id,
            phone=(data.get("phone") or None),
            deal_id=(str(data["deal_id"]) if data.get("deal_id") else None),
            filename=url.rsplit("/", 1)[-1][:120],
        )
        pipeline.submit(call_id)
        return jsonify({"id": call_id}), 201

    @app.post("/webhooks/bitrix")
    def bitrix_webhook():
        """Исходящий вебхук Битрикс24 на событие ONVOXIMPLANTCALLEND.

        Срабатывает для любой телефонии, подключённой к порталу: встроенной и внешних АТС
        через приложения (Mango, Zadarma, МТС, Билайн и др.). Запись забираем отдельно,
        когда Битрикс её прикрепит.
        """
        if not settings.bitrix_app_token:
            return _error("Приём звонков выключен: задайте BITRIX_APP_TOKEN", 403)
        payload = _bitrix_payload()
        token = (payload.get("auth") or {}).get("application_token", "")
        if not hmac.compare_digest(token, settings.bitrix_app_token):
            return _error("Неверный токен исходящего вебхука", 403)
        if str(payload.get("event", "")).upper() != "ONVOXIMPLANTCALLEND":
            return {"ignored": "event"}

        data = payload.get("data") or {}
        call_ref = str(data.get("CALL_ID") or "").strip()
        if not call_ref:
            return _error("Нет CALL_ID", 400)
        if str(data.get("CALL_FAILED_CODE", "200")) != "200":
            return {"ignored": "not_answered"}  # пропущенный, занято, отклонён
        duration = _num(data.get("CALL_DURATION"))
        if duration < settings.min_call_seconds:
            return {"ignored": "too_short"}  # автоответчик, «перезвоните позже»
        if existing := db.find_by_external_id(call_ref):
            return {"id": existing, "duplicate": True}

        direction = CALL_TYPES.get(str(data.get("CALL_TYPE")), "Звонок")
        call_id = db.create_call(
            "bitrix",
            external_id=call_ref,
            phone=(data.get("PHONE_NUMBER") or None),
            manager_id=(str(data.get("PORTAL_USER_ID")) if data.get("PORTAL_USER_ID") else None),
            call_started_at=data.get("CALL_START_DATE"),
            duration_s=duration,
            filename=direction,
        )
        pipeline.submit(call_id)
        return jsonify({"id": call_id}), 201

    @app.errorhandler(413)
    def too_large(_):
        return _error("Файл больше 200 МБ", 413)

    return app


STT_LABELS = {"yandex": "Yandex SpeechKit", "gigaam": "GigaAM-v3"}


def available_stt() -> dict[str, str]:
    """Модели распознавания, которыми можно распознать звонок прямо сейчас."""
    out = {}
    if settings.yandex_api_key and settings.yandex_folder_id:
        out["yandex"] = STT_LABELS["yandex"]
    if importlib.util.find_spec("gigaam") is not None:
        out["gigaam"] = STT_LABELS["gigaam"]
    return out


CALL_TYPES = {"1": "Исходящий звонок", "2": "Входящий звонок", "3": "Входящий звонок", "4": "Обратный звонок"}


def _bitrix_payload() -> dict:
    """Битрикс шлёт form-urlencoded с ключами вида data[CALL_ID], auth[application_token]."""
    if request.is_json:
        return request.get_json(silent=True) or {}
    out: dict = {}
    for key, value in request.form.items():
        m = re.fullmatch(r"(\w+)\[(\w+)\]", key)
        if m:
            out.setdefault(m.group(1), {})[m.group(2)] = value
        else:
            out[key] = value
    return out


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _public(call: dict) -> dict:
    call = dict(call)
    call.pop("audio_path", None)
    call.pop("recording_url", None)
    return call


def _error(message: str, code: int):
    return jsonify({"error": message}), code


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
