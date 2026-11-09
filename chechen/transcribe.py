"""Распознаёт все записи из папки /samples и пишет результат в /samples/results.

Режимы:
  • одна модель — исходная Omnilingual (MODEL) или дообученная чеченская (LOCAL_MODEL);
  • две модели (DUAL=1, по умолчанию для дообученной): каждый фрагмент слушают чеченская модель и русская GigaAM,
    а нейросеть AI Studio (DeepSeek / Alice AI / YandexGPT) собирает один текст.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

SAMPLES = Path(os.environ.get("SAMPLES_DIR", "/samples"))
MODEL = os.environ.get("MODEL", "omniASR_LLM_Unlimited_1B_v2")
LANG = os.environ.get("LANG_CODE", "che_Cyrl")
LOCAL_MODEL = os.environ.get("LOCAL_MODEL", "")  # путь к весам дообученной модели внутри контейнера
LOCAL_CARD = "chechen_ctc_local"
DUAL = os.environ.get("DUAL", "1" if LOCAL_MODEL else "0") == "1"
GIGAAM_MODEL = os.environ.get("GIGAAM_MODEL", "v3_e2e_rnnt")
CHUNK_MAX_S = 20.0  # фрагменты для CTC-моделей и GigaAM: обе принимают только короткие записи
AUDIO_EXT = {".mp3", ".wav", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".amr", ".flac", ".webm", ".mp4", ".3gp", ".wma"}


# ---------- звук ----------

def find_audio(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXT)


def to_wav(src: Path, dst: Path) -> float:
    """Любой формат → WAV 16 кГц моно. Возвращает длительность в секундах."""
    proc = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                           "-c:a", "pcm_s16le", str(dst)], capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError("файл повреждён или это не аудио")
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(dst)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip() or 0)


def detect_silences(wav: Path) -> list[tuple[float, float]]:
    proc = subprocess.run(["ffmpeg", "-v", "info", "-i", str(wav), "-af", "silencedetect=noise=-35dB:d=0.35",
                           "-f", "null", "-"], capture_output=True, text=True, timeout=600)
    out, start = [], None
    for line in proc.stderr.splitlines():
        if m := re.search(r"silence_start: (-?[\d.]+)", line):
            start = max(0.0, float(m.group(1)))
        elif (m := re.search(r"silence_end: ([\d.]+)", line)) and start is not None:
            out.append((start, float(m.group(1))))
            start = None
    if start is not None:
        out.append((start, float("inf")))
    return out


def plan_chunks(silences: list[tuple[float, float]], total: float, max_len: float = CHUNK_MAX_S) -> list[tuple[float, float]]:
    """Фрагменты не длиннее max_len, разрезы — по паузам. Чистые паузы пропускаются."""
    speech, pos = [], 0.0
    for s, e in sorted(silences):
        if s > pos:
            speech.append((pos, min(s, total)))
        pos = max(pos, min(e, total))
    if pos < total:
        speech.append((pos, total))
    chunks, cur = [], None
    for s, e in speech:
        while e - s > max_len:
            if cur:
                chunks.append(cur)
                cur = None
            chunks.append((s, s + max_len))
            s += max_len
        if cur is None:
            cur = (s, e)
        elif e - cur[0] <= max_len:
            cur = (cur[0], e)
        else:
            chunks.append(cur)
            cur = (s, e)
    if cur:
        chunks.append(cur)
    out = []
    for s, e in chunks:
        s2, e2 = max(0.0, s - 0.15), min(total, e + 0.15)
        if e2 - s2 >= 0.4:
            out.append((round(s2, 3), round(e2, 3)))
    return out


def cut(src: Path, dst: Path, start: float, end: float) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(src),
                    "-c:a", "pcm_s16le", str(dst)], check=True, timeout=120)


def mmss(sec: float) -> str:
    return f"{int(sec // 60):02d}:{int(sec % 60):02d}"


# ---------- модели ----------

def as_text(result) -> str:
    if isinstance(result, str):
        return result.strip()
    for attr in ("text", "transcription"):
        if hasattr(result, attr):
            return str(getattr(result, attr)).strip()
    if isinstance(result, dict):
        return str(result.get("text") or result.get("transcription") or "").strip()
    return str(result).strip()


def register_local_model(weights: str) -> str:
    """Карточка модели для дообученных весов (архитектура CTC 300M v2, тот же токенизатор)."""
    oasr = Path(os.environ.get("OASR_DIR", "/opt/oasr"))
    card = oasr / "src" / "omnilingual_asr" / "cards" / "models" / f"{LOCAL_CARD}.yaml"
    card.write_text(
        f"name: {LOCAL_CARD}\nmodel_family: wav2vec2_asr\nmodel_arch: 300m_v2\n"
        f"checkpoint: {weights}\ntokenizer_ref: omniASR_tokenizer_written_v2\n",
        encoding="utf-8",
    )
    return LOCAL_CARD


def find_broken_cache(exc: BaseException) -> Path | None:
    """Ищет в цепочке ошибок путь к папке модели в кэше, которую не удалось прочитать."""
    while exc is not None:
        m = re.search(r"(/root/\.cache/fairseq2/assets/[0-9a-f]+)", str(exc))
        if m and "known checkpoints" in str(exc):
            path = Path(m.group(1))
            return path if path.exists() else None
        exc = exc.__cause__ or exc.__context__
    return None


def load_pipeline(model: str):
    from omnilingual_asr.models.inference.pipeline import ASRInferencePipeline

    try:
        return ASRInferencePipeline(model_card=model)
    except Exception as exc:  # noqa: BLE001
        broken = find_broken_cache(exc)
        if not broken:
            raise
        print(f"Модель скачана не полностью — удаляю недокачанные файлы и скачиваю заново ({broken.name})...")
        shutil.rmtree(broken, ignore_errors=True)
        return ASRInferencePipeline(model_card=model)


def load_gigaam():
    import gigaam
    import torch

    device = "cuda" if torch.cuda.is_available() else "cpu"
    return gigaam.load_model(GIGAAM_MODEL, device=device)


# ---------- склейка нейросетью AI Studio ----------

MERGE_PROMPT = """Ты получаешь расшифровку одного телефонного разговора, разрезанного на фрагменты. \
Люди говорят по-чеченски и постоянно вставляют русские слова и фразы. Для каждого фрагмента есть две \
автоматические версии:
- che — модель для чеченского языка. Чеченские слова пишет хорошо. Русские слова тоже слышит, но записывает их \
на слух чеченскими буквами, с искажениями.
- ru — модель для русского языка. Русские слова пишет правильно, а чеченскую речь превращает в бессмысленные \
русские слова.

Задача: собрать один текст фрагмента в порядке звучания.
1. Чеченские слова бери из che как есть: не переводи и не исправляй чеченское написание.
2. Если слово в che звучит похоже на русское слово (даже искажённо) — пиши его по-русски, правильно. \
Сверяйся с ru: там это слово обычно есть в правильном виде. Русских вставок в разговоре много — не оставляй их \
в чеченском написании.
3. Числа, цены, суммы, площади, даты и телефоны бери ТОЛЬКО из ru и пиши цифрами — ru слышит числа надёжнее. \
Если в ru числа нет, а в che оно есть — переведи его в цифры по смыслу.
4. Не добавляй ничего, чего нет в che или ru, и не пересказывай.

Примеры (che → правильно):
- «квартийра херири враони» → «квартира в районе»
- «на стопин десят квадратов» → «на 150 квадратов»
- «пятанацц миллионов рублей» (ru: «15 млн ₽») → «15 млн ₽»
- «кинутин побарацки» → «скинуть по-братски»
- «суна цхьа квартира йезар-кх» → «суна цхьа квартира йезар-кх» (это чеченская фраза — оставляем)

Верни только JSON вида {"lines": [{"i": номер фрагмента, "text": "собранный текст"}]}."""


def yandex_ready() -> bool:
    return bool(os.environ.get("YANDEX_API_KEY") and os.environ.get("YANDEX_FOLDER_ID"))


MERGE_MODELS = [m.strip() for m in os.environ.get(
    "MERGE_MODELS", "qwen3.6-35b-a3b,deepseek-v4.1-flash,aliceai-llm,yandexgpt/latest").split(",") if m.strip()]
RAW_ANSWERS: list[str] = []  # сырые ответы моделей — сохраняются для разбора проблем
OPENAI_URL = os.environ.get("YANDEX_OPENAI_URL", "https://ai.api.cloud.yandex.net/v1/chat/completions")  # OpenAI-совместимый API
NATIVE_URL = os.environ.get("YANDEX_GPT_URL", "https://ai.api.cloud.yandex.net/foundationModels/v1/completion")


def _post(url: str, body: dict, headers: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={**headers, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        hint = " — у ключа нет доступа к этой модели" if exc.code in (401, 403) else ""
        raise RuntimeError(f"HTTP {exc.code}: {detail}{hint}") from None
    try:
        return json.loads(raw)
    except ValueError:
        raise RuntimeError(f"ответ не JSON: {raw[:300]!r}") from None


def _ask(model: str, user_text: str) -> str:
    """Один запрос к модели AI Studio. Возвращает текст ответа или бросает ошибку с причиной."""
    key, folder = os.environ["YANDEX_API_KEY"], os.environ["YANDEX_FOLDER_ID"]
    uri = f"gpt://{folder}/{model}"
    messages = [{"role": "system", "content": MERGE_PROMPT}, {"role": "user", "content": user_text}]
    if model.startswith("yandexgpt"):
        data = _post(NATIVE_URL, {
            "modelUri": uri, "completionOptions": {"stream": False, "temperature": 0.1, "maxTokens": "4000"},
            "messages": [{"role": m["role"], "text": m["content"]} for m in messages], "jsonObject": True,
        }, {"Authorization": f"Api-Key {key}", "x-folder-id": folder})
        alt = (data.get("result", data).get("alternatives") or [{}])[0]
        if alt.get("status") == "ALTERNATIVE_STATUS_CONTENT_FILTER":
            raise RuntimeError("отказ: сработал фильтр содержимого")
        return (alt.get("message") or {}).get("text", "")
    data = _post(OPENAI_URL, {"model": uri, "messages": messages, "temperature": 0.1, "max_tokens": 12000},
                 {"Authorization": f"Api-Key {key}", "OpenAI-Project": folder})
    RAW_ANSWERS.append(f"=== {model} ===\n{json.dumps(data, ensure_ascii=False, indent=2)[:6000]}")
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    if choice.get("finish_reason") == "content_filter":
        raise RuntimeError("отказ: сработал фильтр содержимого")
    content = msg.get("content") or ""
    if not content.strip() and (msg.get("reasoning_content") or msg.get("reasoning")):
        if choice.get("finish_reason") == "length":
            raise RuntimeError("модель потратила весь лимит на рассуждения и не успела ответить")
        content = msg.get("reasoning_content") or msg.get("reasoning") or ""  # иногда ответ лежит в рассуждениях
    return content


def _parse_lines(text: str) -> dict[int, str]:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)  # у моделей с рассуждениями
    clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    if "{" not in clean:
        raise RuntimeError(f"ответ не в формате JSON: {text[:200]!r}")
    out = {}
    for line in json.loads(clean[clean.find("{"): clean.rfind("}") + 1]).get("lines", []):
        try:
            out[int(line["i"])] = str(line.get("text", "")).strip()
        except (KeyError, ValueError, TypeError):
            continue
    if not out:
        raise RuntimeError("модель вернула пустой список фрагментов")
    return out


def merge_with_llm(pieces: list[dict]) -> tuple[dict[int, str], str]:
    """Склеивает фрагменты порциями. Пробует модели по очереди, пока одна не справится.
    Возвращает ({номер: текст}, какая модель сработала)."""
    merged: dict[int, str] = {}
    used = ""
    log_path = SAMPLES / "results" / "склейка-журнал.txt"
    journal = []
    for start in range(0, len(pieces), 30):
        batch = [{"i": p["i"], "che": p["che"], "ru": p["ru"]} for p in pieces[start:start + 30]]
        user_text = json.dumps(batch, ensure_ascii=False)
        errors = []
        for model in MERGE_MODELS:
            try:
                answer = _ask(model, user_text)
                journal.append(f"--- {model} ---\n{answer}\n")
                merged.update(_parse_lines(answer))
                used = model
                break
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{model}: {exc}")
                journal.append(f"--- {model}: ОШИБКА ---\n{exc}\n")
        else:
            log_path.write_text("\n".join(journal), encoding="utf-8")
            (SAMPLES / "results" / "склейка-сырые-ответы.txt").write_text("\n\n".join(RAW_ANSWERS), encoding="utf-8")
            raise RuntimeError("ни одна модель не справилась — " + " | ".join(errors))
    log_path.write_text("\n".join(journal), encoding="utf-8")
    (SAMPLES / "results" / "склейка-сырые-ответы.txt").write_text("\n\n".join(RAW_ANSWERS), encoding="utf-8")
    return merged, used


# ---------- основной цикл ----------

def main() -> int:
    if not SAMPLES.exists():
        print(f"Нет папки {SAMPLES}")
        return 1
    files = find_audio(SAMPLES)
    if not files:
        print("В папке samples нет аудиофайлов. Положите туда записи (mp3, wav, ogg, m4a, opus...) и запустите снова.")
        return 1
    model = MODEL
    if LOCAL_MODEL:
        if not Path(LOCAL_MODEL).exists():
            print(f"Не найдены веса обученной модели: {LOCAL_MODEL}")
            return 1
        model = register_local_model(LOCAL_MODEL)
    is_ctc = "CTC" in model or model == LOCAL_CARD
    model_label = f"дообученная ({Path(LOCAL_MODEL).parent.name})" if LOCAL_MODEL else model
    print(f"Найдено записей: {len(files)}")
    print(f"Чеченская модель: {model_label}")
    print("Загружаю модели. Первый раз они скачиваются, это может занять несколько минут...")

    t0 = time.time()
    pipeline = load_pipeline(model)
    giga = None
    if DUAL:
        try:
            giga = load_gigaam()
            print("Русская модель: GigaAM-v3")
        except Exception as exc:  # noqa: BLE001
            print(f"Русскую модель GigaAM загрузить не удалось ({exc}) — работаю только с чеченской")
    can_merge = giga is not None and yandex_ready()
    if giga is not None and not can_merge:
        print("Ключи Yandex не найдены — склейки не будет, покажу обе версии рядом")
    print(f"Модели загружены за {time.time() - t0:.0f} с\n")

    out_dir = SAMPLES / "results"
    out_dir.mkdir(exist_ok=True)
    report = [f"Распознавание — {datetime.now():%d.%m.%Y %H:%M}", f"Чеченская модель: {model_label}"]
    if giga is not None:
        report.append(f"Русская модель: GigaAM-v3; склейка: {'нейросеть AI Studio' if can_merge else 'нет (нет ключей Yandex)'}")
    report.append("")

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for n, src in enumerate(files, 1):
            print(f"[{n}/{len(files)}] {src.name} ...", end=" ", flush=True)
            try:
                wav = tmpdir / f"{n}.wav"
                duration = to_wav(src, wav)
                t1 = time.time()
                if is_ctc or giga is not None:
                    spans = plan_chunks(detect_silences(wav), duration)
                else:
                    spans = [(0.0, duration)]
                paths = []
                for k, (s, e) in enumerate(spans):
                    p = tmpdir / f"{n}_{k:04d}.wav"
                    cut(wav, p, s, e) if len(spans) > 1 or is_ctc else shutil.copy(wav, p)
                    paths.append(p)
                che = [as_text(r) for r in pipeline.transcribe([str(p) for p in paths], lang=[LANG] * len(paths),
                                                                batch_size=4)] if paths else []
                ru = [as_text(giga.transcribe(str(p))) for p in paths] if giga is not None else [""] * len(paths)
                pieces = [{"i": k, "start": s, "che": c, "ru": r} for k, ((s, _), c, r) in enumerate(zip(spans, che, ru))]
                merged, merge_error, merge_model = {}, "", ""
                if can_merge and pieces:
                    try:
                        merged, merge_model = merge_with_llm(pieces)
                    except Exception as exc:  # noqa: BLE001
                        merge_error = str(exc)
                        print(f"(склейка не удалась: {exc})", end=" ")
                took = time.time() - t1
                print(f"готово за {took:.0f} с")

                block = [f"=== {src.name} ({duration:.0f} с, распознано за {took:.0f} с) ==="]
                if giga is None:
                    text = " ".join(p["che"] for p in pieces if p["che"])
                    block += [text or "(пусто)", ""]
                else:
                    final = " ".join((merged.get(p["i"]) or p["che"]) for p in pieces).strip()
                    text = final
                    if merge_error:
                        block += [f"СКЛЕЙКА НЕ УДАЛАСЬ: {merge_error}", ""]
                    block += [f"ИТОГ (склеила модель {merge_model}):" if merged else "ЧЕЧЕНСКАЯ МОДЕЛЬ:", final or "(пусто)", "",
                              "По фрагментам:"]
                    for p in pieces:
                        block.append(f"[{mmss(p['start'])}] ЧЕ: {p['che']}")
                        block.append(f"        РУ: {p['ru']}")
                        if merged:
                            block.append(f"        ИТОГ: {merged.get(p['i'], '(нет)')}")
                    block.append("")
                (out_dir / f"{src.stem}.txt").write_text("\n".join(block[1:]) + "\n", encoding="utf-8-sig")
            except Exception as exc:  # noqa: BLE001 — одна битая запись не должна останавливать остальные
                print(f"ошибка: {exc}")
                block = [f"=== {src.name} ===", f"ОШИБКА: {exc}", ""]
            report += block

    summary = out_dir / "ВСЕ_РЕЗУЛЬТАТЫ.txt"
    summary.write_text("\n".join(report), encoding="utf-8-sig")
    # Отдельный файл на каждый запуск: Блокнот с вкладками иначе показывает старую открытую копию
    dated = out_dir / f"результат-{datetime.now():%Y-%m-%d_%H-%M-%S}.txt"
    dated.write_text("\n".join(report), encoding="utf-8-sig")
    print(f"\nГотово. Результаты: samples\\results\\{dated.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
