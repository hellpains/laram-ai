import io
import json
import subprocess
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from app import db, pipeline
from app.config import settings
from app.crm.bitrix import Bitrix, normalize_phones
from app.llm import extract_json
from app.llm.yandexgpt import YandexGPT, parse_completion
from app.models import Segment, Summary, Transcript
from app.stt.yandex import YandexSpeechKit, parse_recognition


# ---------- фейковый HTTP для внешних API ----------
class FakeResp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status
        self.text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)

    def json(self):
        return json.loads(self.text)


class FakeSession:
    """Отвечает по правилам {подстрока URL: ответ или функция(body)}; пишет журнал вызовов."""

    def __init__(self, routes):
        self.routes, self.calls, self.headers = routes, [], {}

    def _handle(self, method, url, body=None, params=None):
        self.calls.append((method, url, body, params))
        for key, resp in self.routes.items():
            if key in url:
                resp = resp(body) if callable(resp) else resp
                return resp if isinstance(resp, FakeResp) else FakeResp(resp)
        raise AssertionError(f"unexpected {method} {url}")

    def post(self, url, json=None, timeout=None):
        return self._handle("POST", url, json)

    def get(self, url, params=None, timeout=None):
        return self._handle("GET", url, params=params)


# ---------- SpeechKit ----------
STT_STREAM = "\n".join(json.dumps(x, ensure_ascii=False) for x in [
    {"result": {"channelTag": "0", "final": {"alternatives": [{"text": "добрый день", "startTimeMs": "0"}]}}},
    {"result": {"channelTag": "0", "finalRefinement": {"normalizedText": {"alternatives": [{"text": "Добрый день!", "startTimeMs": "0"}]}}}},
    {"result": {"channelTag": "1", "finalRefinement": {"normalizedText": {"alternatives": [{"text": "Здравствуйте.", "startTimeMs": "2100"}]}}}},
    {"result": {"channelTag": "0", "eouUpdate": {"timeMs": "2000"}}},
    {"result": {"channelTag": "0", "finalRefinement": {"normalizedText": {"alternatives": [{"text": "Цена 140 000 ₽.", "startTimeMs": "4000"}]}}}},
])


def test_parse_recognition_prefers_normalized_text_and_orders_by_time():
    segs = parse_recognition(STT_STREAM)
    assert [(s.speaker, s.start_ms, s.text) for s in segs] == [
        ("A", 0, "Добрый день!"), ("B", 2100, "Здравствуйте."), ("A", 4000, "Цена 140 000 ₽."),
    ]


def test_speechkit_full_flow(tmp_path, monkeypatch):
    import app.stt.yandex as mod

    monkeypatch.setattr(mod.time, "sleep", lambda s: None)  # не ждём в тестах
    ogg = tmp_path / "a.ogg"
    ogg.write_bytes(b"OggS-fake")
    polls = iter([{"done": False}, {"done": True}])
    http = FakeSession({
        "recognizeFileAsync": {"id": "op1"},
        "operations/op1": lambda _: next(polls),
        "getRecognition": FakeResp(STT_STREAM),
    })
    stt = YandexSpeechKit(replace(settings, yandex_api_key="k", yandex_folder_id="f"), session=http)
    t = stt.transcribe(ogg, channels=2, duration_s=12.0)
    assert len(t.segments) == 3
    body = http.calls[0][2]
    assert body["recognitionModel"]["audioFormat"]["containerAudio"]["containerAudioType"] == "OGG_OPUS"
    assert "speakerLabeling" not in body  # стерео — говорящие разделены каналами
    assert http.headers["Authorization"] == "Api-Key k"


# ---------- YandexGPT и разбор ответа ----------
def test_extract_json_handles_fences_and_noise():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Вот ответ: {"a": 2} спасибо') == {"a": 2}
    with pytest.raises(ValueError):
        extract_json("нет json")


def test_summary_normalizes_bad_values():
    s = Summary.from_llm({
        "summary": "ок", "agreements": "одна договорённость", "next_step_date": "в четверг",
        "outcome": "maybe", "objections": None, "quality_flags": ["", "Не назначил шаг"], "manager_speaker": "A",
    })
    assert s.agreements == ["одна договорённость"]
    assert s.next_step_date is None
    assert s.outcome == "in_progress"
    assert s.objections == [] and s.quality_flags == ["Не назначил шаг"]


def test_yandexgpt_request_and_parse():
    answer = {"summary": "Клиент заинтересован", "result": "Отправим КП", "agreements": ["КП сегодня"],
              "next_step": "Созвон", "next_step_date": "2026-11-05", "outcome": "in_progress",
              "objections": [], "quality_flags": [], "manager_speaker": "A"}
    http = FakeSession({"completion": {"result": {"alternatives": [
        {"message": {"role": "assistant", "text": json.dumps(answer, ensure_ascii=False)}, "status": "ALTERNATIVE_STATUS_FINAL"}]}}})
    llm = YandexGPT(replace(settings, yandex_api_key="k", yandex_folder_id="fld"), session=http)
    t = Transcript([Segment("A", 0, "Добрый день"), Segment("B", 1000, "Здравствуйте")], 30)
    s = llm.summarize(t, date(2026, 11, 2))
    assert s.next_step_date == "2026-11-05" and s.agreements == ["КП сегодня"]
    body = http.calls[0][2]
    assert body["modelUri"] == "gpt://fld/yandexgpt/latest" and body["jsonObject"] is True
    assert "02.11.2026, понедельник" in body["messages"][0]["text"]
    assert "Собеседник B: Здравствуйте" in body["messages"][1]["text"]


def test_parse_completion_without_result_wrapper():
    assert parse_completion({"alternatives": [{"message": {"text": "{}"}}]}) == "{}"


# ---------- Битрикс24 ----------
def test_normalize_phones():
    assert normalize_phones("8 (928) 123-45-67") == ["+79281234567", "89281234567", "79281234567"]


def test_bitrix_push_by_phone_creates_fields_comment_and_task():
    http = FakeSession({
        "crm.duplicate.findbycomm": {"result": {"CONTACT": [55]}},
        "crm.deal.list": {"result": [{"ID": "901", "TITLE": "Газоблок Аргун", "ASSIGNED_BY_ID": "7"}]},
        "crm.deal.update": {"result": True},
        "crm.timeline.comment.add": {"result": 1},
        "tasks.task.add": {"result": {"task": {"id": "3001"}}},
    })
    cfg = replace(settings, bitrix_webhook_url="https://x.bitrix24.ru/rest/1/abc")
    summary = Summary(summary="Суть", result="Итог", agreements=["КП сегодня"], next_step="Позвонить",
                      next_step_date="2026-11-05", quality_flags=["Не выявил ЛПР"])
    res = Bitrix(cfg, session=http).push_summary(summary, "+7 928 123-45-67", None, "звонок 1")

    assert res.status == "ok" and "#901" in res.message and "задача #3001" in res.message
    calls = {url.rsplit("/", 1)[-1]: body for _, url, body, _ in http.calls}
    assert calls["crm.deal.list.json"]["filter"] == {"CONTACT_ID": 55, "CLOSED": "N"}
    upd = calls["crm.deal.update.json"]
    assert upd["id"] == 901 and upd["fields"]["UF_CRM_CALL_NEXT_DATE"] == "2026-11-05"
    assert upd["fields"]["UF_CRM_CALL_AGREEMENTS"] == "• КП сегодня"
    assert "На разбор РОПу" in calls["crm.timeline.comment.add.json"]["fields"]["COMMENT"]
    task = calls["tasks.task.add.json"]["fields"]
    assert task["RESPONSIBLE_ID"] == 7 and task["UF_CRM_TASK"] == ["D_901"] and task["DEADLINE"] == "2026-11-05T12:00:00"


def test_bitrix_not_found_and_error():
    cfg = replace(settings, bitrix_webhook_url="https://x.bitrix24.ru/rest/1/abc")
    http = FakeSession({"crm.duplicate.findbycomm": {"result": []}})
    assert Bitrix(cfg, session=http).push_summary(Summary(), "+79280000000", None, "").status == "not_found"
    from app.crm.bitrix import BitrixError
    http = FakeSession({"crm.deal.get": {"error": "NOT_FOUND", "error_description": "Not found"}})
    with pytest.raises(BitrixError):
        Bitrix(cfg, session=http).find_target(None, "42")


# ---------- сквозной сценарий через веб ----------
@pytest.fixture
def client(monkeypatch):
    from app.main import create_app

    monkeypatch.setattr(pipeline, "submit", lambda cid: pipeline.process_call(cid))  # без фоновых потоков
    app = create_app(start_workers=False)
    return app.test_client()


def _wav(tmp_path: Path) -> bytes:
    path = tmp_path / "call.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    "-ac", "2", str(path)], check=True)
    return path.read_bytes()


def test_upload_end_to_end_demo_mode(client, tmp_path):
    r = client.post("/api/calls", data={"file": (io.BytesIO(_wav(tmp_path)), "call.wav"), "phone": "+79281234567"},
                    content_type="multipart/form-data")
    assert r.status_code == 201
    call = client.get(f"/api/calls/{r.json['id']}").json
    assert call["status"] == "done", call.get("error")
    assert call["duration_s"] == pytest.approx(3.0, abs=0.2)
    assert call["summary"]["next_step"] and call["transcript"]["segments"]
    assert call["crm_status"] == "skipped"
    assert "audio_path" not in call
    assert any(c["id"] == call["id"] for c in client.get("/api/calls").json)


def test_upload_validation(client):
    r = client.post("/api/calls", data={"file": (io.BytesIO(b"x"), "notes.txt")}, content_type="multipart/form-data")
    assert r.status_code == 400
    r = client.post("/api/calls", data={"file": (io.BytesIO(b"x"), "a.mp3"), "deal_id": "abc"},
                    content_type="multipart/form-data")
    assert r.status_code == 400


def test_broken_audio_becomes_error_and_can_retry(client):
    r = client.post("/api/calls", data={"file": (io.BytesIO(b"not audio"), "a.mp3")}, content_type="multipart/form-data")
    call = client.get(f"/api/calls/{r.json['id']}").json
    assert call["status"] == "error" and "аудио" in call["error"].lower()
    assert client.post(f"/api/calls/{call['id']}/retry").status_code == 200


def test_webhook_requires_secret_and_dedupes(client, monkeypatch):
    assert client.post("/webhooks/call", json={"recording_url": "https://x/r.mp3"}).status_code == 403
    monkeypatch.setattr(pipeline, "submit", lambda cid: None)
    body = {"recording_url": "https://x/r.mp3", "phone": "+79280000000", "call_id": "ext-1"}
    r1 = client.post("/webhooks/call?secret=test-secret", json=body)
    r2 = client.post("/webhooks/call", json=body, headers={"X-Webhook-Secret": "test-secret"})
    assert r1.status_code == 201 and r2.json == {"id": r1.json["id"], "duplicate": True}


# ---------- приём звонков из Битрикс24 ----------
def _bitrix_form(token="tok", code="200", duration="95", call_id="externalCall.abc.1"):
    return {
        "event": "ONVOXIMPLANTCALLEND",
        "data[CALL_ID]": call_id, "data[CALL_TYPE]": "1", "data[PHONE_NUMBER]": "+79281234567",
        "data[PORTAL_USER_ID]": "15", "data[CALL_DURATION]": duration, "data[CALL_FAILED_CODE]": code,
        "data[CALL_START_DATE]": "2026-11-02T10:00:00+03:00", "auth[application_token]": token,
    }


@pytest.fixture
def bitrix_on(monkeypatch):
    monkeypatch.setattr(settings, "bitrix_webhook_url", "https://x.bitrix24.ru/rest/1/abc")
    monkeypatch.setattr(settings, "bitrix_app_token", "tok")
    monkeypatch.setattr(settings, "record_wait_minutes", 15)


def test_bitrix_webhook_filters(client, bitrix_on, monkeypatch):
    monkeypatch.setattr(pipeline, "submit", lambda cid: None)
    assert client.post("/webhooks/bitrix", data=_bitrix_form(token="bad")).status_code == 403
    assert client.post("/webhooks/bitrix", data=_bitrix_form(code="304")).json == {"ignored": "not_answered"}
    assert client.post("/webhooks/bitrix", data=_bitrix_form(duration="7")).json == {"ignored": "too_short"}
    assert client.post("/webhooks/bitrix", data={**_bitrix_form(), "event": "ONCRMDEALUPDATE"}).json == {"ignored": "event"}
    r1 = client.post("/webhooks/bitrix", data=_bitrix_form(call_id="c-dup"))
    r2 = client.post("/webhooks/bitrix", data=_bitrix_form(call_id="c-dup"))
    assert r1.status_code == 201 and r2.json["duplicate"] is True
    call = db.get(r1.json["id"])
    assert call["manager_id"] == "15" and call["filename"] == "Исходящий звонок"


def test_bitrix_call_end_to_end(client, bitrix_on, monkeypatch, tmp_path):
    wav = _wav(tmp_path)
    polls = iter([[{"CALL_ID": "c-1"}],  # первая проверка: записи ещё нет
                  [{"CALL_ID": "c-1", "RECORD_FILE_ID": "777", "CRM_ENTITY_TYPE": "CONTACT", "CRM_ENTITY_ID": "55"}]])
    http = FakeSession({
        "voximplant.statistic.get": lambda _: {"result": next(polls)},
        "disk.file.get": {"result": {"DOWNLOAD_URL": "https://x.bitrix24.ru/disk/download/777"}},
        "crm.deal.list": {"result": [{"ID": "901", "TITLE": "Газоблок", "ASSIGNED_BY_ID": "7"}]},
        "crm.deal.update": {"result": True},
        "crm.timeline.comment.add": {"result": 1},
        "tasks.task.add": {"result": {"task": {"id": "3002"}}},
    })
    import app.crm.bitrix as bxmod
    monkeypatch.setattr(bxmod.requests, "Session", lambda: http)
    downloaded = []

    def fake_download(url, dst):
        downloaded.append(url)
        dst.write_bytes(wav)
        return dst

    monkeypatch.setattr(pipeline, "_download", fake_download)
    monkeypatch.setattr(pipeline, "submit_later", lambda cid, delay: pipeline.process_call(cid))

    r = client.post("/webhooks/bitrix", data=_bitrix_form(call_id="c-1"))
    call = client.get(f"/api/calls/{r.json['id']}").json
    assert call["status"] == "done", call.get("error")
    assert downloaded == ["https://x.bitrix24.ru/disk/download/777"]
    assert call["crm_status"] == "ok" and "#901" in call["crm_message"]
    calls = {url.rsplit("/", 1)[-1]: body for _, url, body, _ in http.calls}
    assert calls["crm.deal.list.json"]["filter"]["CONTACT_ID"] == 55  # сделка найдена по привязке звонка
    assert calls["tasks.task.add.json"]["fields"]["RESPONSIBLE_ID"] == 15  # задача менеджеру, который звонил


def test_bitrix_record_never_appears(client, bitrix_on, monkeypatch):
    monkeypatch.setattr(settings, "record_wait_minutes", 0)
    http = FakeSession({"voximplant.statistic.get": {"result": [{"CALL_ID": "c-2"}]}})
    import app.crm.bitrix as bxmod
    monkeypatch.setattr(bxmod.requests, "Session", lambda: http)
    r = client.post("/webhooks/bitrix", data=_bitrix_form(call_id="c-2"))
    call = client.get(f"/api/calls/{r.json['id']}").json
    assert call["status"] == "error" and "запись разговоров" in call["error"]


def test_db_migration_adds_new_columns(tmp_path, monkeypatch):
    import sqlite3
    old = tmp_path / "old.db"
    sqlite3.connect(old).executescript("CREATE TABLE calls (id TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT,"
                                       " status TEXT, source TEXT, external_id TEXT);")
    monkeypatch.setattr(type(settings), "db_path", property(lambda self: old))
    db.init()
    cols = {r[1] for r in sqlite3.connect(old).execute("PRAGMA table_info(calls)")}
    assert {"manager_id", "crm_entity_id", "wait_started_at"} <= cols


# ---------- GigaAM: нарезка и распознавание по каналам ----------
from app.stt.gigaam import CHUNK_MAX_S, GigaAMSTT, detect_silences, plan_chunks


def test_plan_chunks_respects_limit_and_skips_silence():
    # речь 0–5, пауза 5–6, речь 6–20, пауза 20–21, речь 21–30, длинная фраза 31–80 без пауз, тишина до конца
    silences = [(5, 6), (20, 21), (30, 31), (80, 100)]
    chunks = plan_chunks(silences, total=100)
    assert all(e - s <= CHUNK_MAX_S + 0.31 for s, e in chunks)
    assert chunks[0][0] == 0 and chunks[0][1] <= 20.2  # 0–20 помещается в один кусок
    assert not any(s >= 80 for s, _ in chunks)  # тишину в конце не распознаём
    covered = sum(e - s for s, e in chunks)
    assert covered >= 5 + 14 + 9 + 49  # вся речь покрыта


def test_plan_chunks_without_silences_uses_fixed_windows():
    chunks = plan_chunks([], total=50)
    assert len(chunks) == 3 and chunks[-1][1] == 50


class FakeGigaModel:
    def __init__(self):
        self.calls = []

    def transcribe(self, path):
        self.calls.append(path)
        return f"фраза {len(self.calls)}"


def _two_channel_call(tmp_path):
    """Стерео: в левом канале говорит «менеджер» 0–3 с, в правом «клиент» 4–7 с."""
    out = tmp_path / "call.ogg"
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", "sine=frequency=400:duration=3",
        "-f", "lavfi", "-i", "sine=frequency=700:duration=3",
        "-filter_complex",
        "[0]apad=whole_dur=8[l];[1]adelay=4000,apad=whole_dur=8[r];[l][r]join=inputs=2:channel_layout=stereo",
        "-c:a", "libopus", str(out)], check=True)
    return out


def test_detect_silences_on_real_audio(tmp_path):
    wav = tmp_path / "x.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=500:duration=2",
                    "-af", "apad=pad_dur=3", str(wav)], check=True)
    sil = detect_silences(wav)
    assert sil and sil[0][0] == pytest.approx(2.0, abs=0.2)


def test_gigaam_stereo_transcript_by_channels(tmp_path):
    model = FakeGigaModel()
    stt = GigaAMSTT(replace(settings, gigaam_model="v3_e2e_rnnt"), model=model)
    t = stt.transcribe(_two_channel_call(tmp_path), channels=2, duration_s=8)
    assert [(s.speaker, round(s.start_ms / 1000)) for s in t.segments] == [("A", 0), ("B", 4)]
    assert len(model.calls) == 2 and "GigaAM" in t.language


def test_retry_fresh_redoes_transcription(client, tmp_path, monkeypatch):
    r = client.post("/api/calls", data={"file": (io.BytesIO(_wav(tmp_path)), "call.wav")},
                    content_type="multipart/form-data")
    cid = r.json["id"]
    db.update(cid, transcript={"segments": [{"speaker": "A", "start_ms": 0, "text": "старая"}], "duration_s": 3})
    client.post(f"/api/calls/{cid}/retry")
    assert db.get(cid)["transcript"]["segments"][0]["text"] == "старая"  # конспект пересобран по старой расшифровке
    client.post(f"/api/calls/{cid}/retry?fresh=1")
    assert db.get(cid)["transcript"]["segments"][0]["text"] != "старая"  # речь распознана заново


def test_status_lists_models_and_retry_rejects_unavailable(client, tmp_path, monkeypatch):
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "available_stt", lambda: {"gigaam": "GigaAM-v3"})
    assert client.get("/api/status").json["stt_available"] == {"gigaam": "GigaAM-v3"}
    r = client.post("/api/calls", data={"file": (io.BytesIO(_wav(tmp_path)), "call.wav")},
                    content_type="multipart/form-data")
    cid = r.json["id"]
    assert client.post(f"/api/calls/{cid}/retry?fresh=1&stt=yandex").status_code == 400
    # выбранная модель запоминается для звонка и используется при распознавании
    import app.stt.gigaam as g
    monkeypatch.setattr(g, "_load_model", lambda name: FakeGigaModel())
    assert client.post(f"/api/calls/{cid}/retry?fresh=1&stt=gigaam").status_code == 200
    call = db.get(cid)
    assert call["stt_provider"] == "gigaam" and "GigaAM" in call["transcript"]["language"]
