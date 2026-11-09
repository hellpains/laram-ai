"""Нарезка аудиокниги на короткие фразы с точным текстом — датасет для дообучения модели распознавания.

Для каждой пары в папке /books «имя.wav|mp3|… + имя.txt»:
  1. текст чистится и делится на фразы;
  2. модель выравнивания MMS_FA (Meta, работает для любого языка через латинскую транслитерацию)
     находит в записи начало и конец каждой фразы — окнами по ~40 с, чтобы хватало памяти;
  3. фразы с плохим совпадением отбрасываются, остальные режутся в WAV 16 кГц;
  4. пишутся manifest.tsv (для обучения) и проверка.html (послушать и сверить глазами).

Запуск: python align.py  (в образе callnotes-chechen, папка /books подключена снаружи)
"""
from __future__ import annotations

import csv
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

BOOKS = Path(os.environ.get("BOOKS_DIR", "/books"))
SR = 16000
FPS = 50  # кадров модели в секунду (шаг 320 отсчётов)
MIN_SCORE = float(os.environ.get("MIN_SCORE", "0.2"))  # уверенность выравнивания; для чеченского нормальные значения 0.2-0.4
MIN_RATE, MAX_RATE = 7.0, 24.0  # букв в секунду: вне этих границ фраза, скорее всего, выровнена неверно
MIN_SEC, MAX_SEC = 1.0, 25.0
MAX_UNIT_WORDS = 30
WINDOW_TARGET_SEC = 40
AUDIO_EXT = {".wav", ".mp3", ".ogg", ".opus", ".m4a", ".flac", ".aac", ".webm"}

# ---------- текст ----------

# Латинские буквы, которые в чеченских текстах часто стоят вместо кириллических
LATIN_TO_CYR = str.maketrans({
    "A": "А", "a": "а", "B": "В", "C": "С", "c": "с", "E": "Е", "e": "е", "H": "Н", "K": "К", "k": "к",
    "M": "М", "m": "м", "O": "О", "o": "о", "P": "Р", "p": "р", "T": "Т", "X": "Х", "x": "х", "y": "у", "Y": "У",
})
PALOCHKA = "ӏ"  # U+04CF — чеченская «палочка»; в текстах её пишут как I, l, 1, Ӏ или |
CYR = "а-яёА-ЯЁӀӏ"


def normalize_chechen(text: str) -> str:
    """Единый вид текста: палочка одной буквой, кириллица вместо похожей латиницы."""
    t = text.replace("­", "").replace("﻿", "")
    # Палочка: I, l, 1, | рядом с кириллицей (гIа, кIант, Iаьржа) → ӏ
    t = re.sub(rf"(?<=[{CYR}])[Il1|Ӏ]|[Il1|Ӏ](?=[{CYR}])", PALOCHKA, t)
    t = t.replace("Ӏ", PALOCHKA)
    # Латиница в словах → кириллица (Max → Мах)
    t = re.sub(r"[A-Za-z]+", lambda m: m.group(0).translate(LATIN_TO_CYR), t)
    return t


def training_text(text: str) -> str:
    """Текст для обучения: строчные буквы, без пунктуации — как выдаёт модель."""
    t = normalize_chechen(text).lower().replace("ё", "е")
    t = re.sub(rf"[^{CYR}0-9\s-]", " ", t)
    t = re.sub(r"\s-\s|^-|-$", " ", t)
    return re.sub(r"\s+", " ", t).strip()


ROMAN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "x", "ц": "c", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "'", "ы": "y", "ь": "'",
    "э": "e", "ю": "yu", "я": "ya", PALOCHKA: "'",
}


def romanize(text: str) -> list[str]:
    """Слова латиницей для модели выравнивания (её алфавит: a-z и апостроф). Цифры и знаки пропускаются."""
    words = []
    for w in training_text(text).split():
        r = "".join(ROMAN.get(ch, "") for ch in w)
        r = re.sub(r"'+", "'", r).strip("'") or r
        if r and r != "'":
            words.append(r)
    return words


def split_units(text: str) -> list[str]:
    """Фразы для нарезки: предложения; слишком длинные — по запятым."""
    text = normalize_chechen(text)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    units: list[str] = []
    for p in paragraphs:
        for sent in re.split(r"(?<=[.!?…»])\s+(?=[«\"(—–-]?[" + CYR + r"0-9])", p):
            sent = sent.strip()
            if not sent:
                continue
            if len(sent.split()) <= MAX_UNIT_WORDS:
                units.append(sent)
                continue
            parts, cur = re.split(r"(?<=[,;:])\s+", sent), ""
            for part in parts:
                if cur and len((cur + " " + part).split()) > MAX_UNIT_WORDS:
                    units.append(cur)
                    cur = part
                else:
                    cur = f"{cur} {part}".strip()
            if cur:
                units.append(cur)
    # Живая речь часто записана без знаков препинания: режем слишком длинные куски по словам на равные части
    out: list[str] = []
    for u in units:
        words = u.split()
        if len(words) <= MAX_UNIT_WORDS:
            out.append(u)
            continue
        n = -(-len(words) // MAX_UNIT_WORDS)
        size = -(-len(words) // n)
        out += [" ".join(words[i:i + size]) for i in range(0, len(words), size)]
    return out


# ---------- выравнивание окнами ----------

@dataclass
class Unit:
    idx: int
    text: str
    roman: list[str]
    start: float = -1.0
    end: float = -1.0
    score: float = 0.0
    status: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def n_chars(self) -> int:
        return sum(len(w) for w in self.roman)


# align_fn(начальный кадр, конечный кадр, список фраз) -> [(старт_кадр, конец_кадр, уверенность) для каждой фразы]
AlignFn = Callable[[int, int, list[list[str]]], list[tuple[int, int, float]]]


def align_all(units: list[Unit], total_frames: int, align_fn: AlignFn, log=print) -> None:
    """Идём по записи окнами: выравниваем несколько фраз, фиксируем все, кроме последней, сдвигаемся."""
    speakable = [u for u in units if u.roman]
    for u in units:
        if not u.roman:
            u.status = "нет текста для выравнивания"
    if not speakable:
        return
    chars_total = sum(u.n_chars for u in speakable)
    frames_per_char = total_frames / max(chars_total, 1)
    pos, i = 0, 0
    t0 = time.time()
    while i < len(speakable):
        # Набираем фразы примерно на WINDOW_TARGET_SEC, минимум две
        j, expected = i, 0.0
        while j < len(speakable) and (j - i < 2 or expected < WINDOW_TARGET_SEC * FPS):
            expected += speakable[j].n_chars * frames_per_char
            j += 1
        batch = speakable[i:j]
        is_last = j >= len(speakable)
        end = total_frames if is_last else min(total_frames, int(pos + expected * 1.6 + 20 * FPS))
        # окно должно вмещать хотя бы по кадру на символ
        need = sum(u.n_chars + len(u.roman) for u in batch) + 10
        end = max(end, min(total_frames, pos + need))
        try:
            spans = align_fn(pos, end, [u.roman for u in batch])
        except Exception as exc:  # noqa: BLE001 — обычно: текст длиннее записи, оставшимся фразам не хватает звука
            log(f"  выравнивание остановлено на фразе {batch[0].idx}: {exc}")
            break
        commit = batch if is_last else batch[:-1]
        for u, (s, e, sc) in zip(commit, spans):
            u.start, u.end, u.score = s / FPS, e / FPS, sc
        pos = max(pos + 1, spans[len(commit) - 1][1]) if commit else end
        i += len(commit)
        done = i / len(speakable)
        el = time.time() - t0
        log(f"  выровнено фраз: {i}/{len(speakable)} ({done:.0%}), позиция {pos / FPS / 60:.1f} мин, прошло {el:.0f} с")


def judge(u: Unit) -> None:
    if u.status:
        return
    dur = u.end - u.start
    if u.start < 0:
        u.status = "не выровнено"
    elif re.search(r"\d", u.text):
        u.status = "есть цифры (произносятся словами — текст не совпадёт)"
    elif dur < MIN_SEC:
        u.status = "слишком коротко"
    elif dur > MAX_SEC:
        u.status = "слишком длинно"
    elif not MIN_RATE <= len(re.sub(r"\W", "", u.text)) / dur <= MAX_RATE:
        u.status = "неестественная скорость речи — вероятно, неверное выравнивание"
    elif u.score < MIN_SCORE:
        u.status = "плохое совпадение звука и текста"
    else:
        u.status = "ok"


# ---------- звук и модель ----------

def to_wav16k(src: Path, dst: Path) -> None:
    proc = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", str(SR),
                           "-c:a", "pcm_s16le", str(dst)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"не удалось прочитать звук: {proc.stderr.strip()[:200]}")


def read_wav(path: Path):
    import numpy as np

    with wave.open(str(path)) as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return data.astype("float32") / 32768.0


def build_aligner(samples):
    """Готовит модель MMS_FA и возвращает (число кадров, функцию выравнивания)."""
    import torch
    import torchaudio
    import torchaudio.functional as F

    torch.set_num_threads(max(1, os.cpu_count() or 1))
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"  устройство: {'видеокарта ' + torch.cuda.get_device_name(0) if device == 'cuda' else 'процессор'}")
    bundle = torchaudio.pipelines.MMS_FA
    model = bundle.get_model(with_star=True).eval().to(device)
    vocab = bundle.get_dict(star="*")
    star = vocab["*"]
    wav = torch.from_numpy(samples)[None]

    # Вероятности букв для всей записи — кусками по 30 с с запасом по краям
    hop, chunk, ctx = 320, 30 * SR, 2 * SR
    n = wav.shape[1]
    parts = []
    print("  считаю звук моделью выравнивания...")
    with torch.inference_mode():
        for st in range(0, n, chunk):
            a, b = max(0, st - ctx), min(n, st + chunk + ctx)
            em, _ = model(wav[:, a:b].to(device))
            em = torch.log_softmax(em.float(), dim=-1)[0].cpu()
            off = (st - a) // hop
            cnt = (min(n, st + chunk) - st + hop - 1) // hop
            parts.append(em[off:off + cnt])
    emission = torch.cat(parts)
    total = emission.shape[0]

    def align_fn(f0: int, f1: int, phrases: list[list[str]]):
        tokens, owner = [star], [-1]
        for k, words in enumerate(phrases):
            for w in words:
                for ch in w:
                    if ch in vocab:
                        tokens.append(vocab[ch])
                        owner.append(k)
        tokens.append(star)
        owner.append(-1)
        em = emission[f0:f1]
        targets = torch.tensor([tokens], dtype=torch.int32)
        ali, scores = F.forced_align(em[None], targets, blank=0)
        spans = F.merge_tokens(ali[0], scores[0].exp())
        res = []
        for k in range(len(phrases)):
            mine = [sp for sp, o in zip(spans, owner) if o == k]
            if not mine:
                res.append((f0, f0, 0.0))
                continue
            sc = sum(sp.score * (sp.end - sp.start) for sp in mine) / max(1, sum(sp.end - sp.start for sp in mine))
            res.append((f0 + mine[0].start, f0 + mine[-1].end, float(sc)))
        return res

    return total, align_fn


# ---------- одна книга ----------

def process_pair(audio: Path, text_path: Path) -> dict:
    name = audio.stem
    out = BOOKS / "dataset" / name
    (out / "clips").mkdir(parents=True, exist_ok=True)
    print(f"\n=== {audio.name} + {text_path.name} ===")

    raw = text_path.read_bytes()
    for enc in ("utf-8-sig", "cp1251", "utf-16"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    units = [Unit(k, t, romanize(t)) for k, t in enumerate(split_units(text))]
    print(f"  фраз в тексте: {len(units)}")

    wav16 = Path(tempfile.mkdtemp(prefix="align-")) / "full16k.wav"  # временный файл — не в папке пользователя
    to_wav16k(audio, wav16)
    samples = read_wav(wav16)
    print(f"  длительность записи: {len(samples) / SR / 60:.1f} мин")

    total, align_fn = build_aligner(samples)
    align_all(units, total, align_fn)
    for u in units:
        judge(u)

    rows = cut_clips(out, name, units, wav16)
    wav16.unlink(missing_ok=True)
    return finish(out, name, audio, units, rows, total)


def cut_clips(out: Path, name: str, units: list[Unit], wav16: Path) -> list[dict]:
    """Режет фразы со статусом ok из WAV 16 кГц в папку clips."""
    clips = out / "clips"
    clips.mkdir(exist_ok=True)
    for old in clips.glob("*.wav"):
        try:
            old.unlink()
        except OSError:
            pass  # файл будет перезаписан
    rows = []
    with wave.open(str(wav16)) as src:
        n = src.getnframes()
        for u in units:
            if u.status != "ok":
                continue
            a, b = max(0, int((u.start - 0.15) * SR)), min(n, int((u.end + 0.15) * SR))
            src.setpos(a)
            clip = clips / f"{name}_{u.idx:05d}.wav"
            with wave.open(str(clip), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SR)
                w.writeframes(src.readframes(b - a))
            rows.append({"audio": f"clips/{clip.name}", "duration": round((b - a) / SR, 2),
                         "text": training_text(u.text), "original": u.text, "score": round(u.score, 3)})
    return rows


def recut(audio: Path) -> dict:
    """Пересобрать датасет по уже готовому all_phrases.tsv (с новыми фильтрами), без повторного выравнивания."""
    name = audio.stem
    out = BOOKS / "dataset" / name
    table = out / "all_phrases.tsv"
    units = []
    with table.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            u = Unit(int(r["№"]), r["текст"], romanize(r["текст"]), float(r["начало, с"]), float(r["конец, с"]),
                     float(r["уверенность"]))
            if not u.roman:
                u.status = "нет текста для выравнивания"
            units.append(u)
    for u in units:
        judge(u)
    wav16 = Path(tempfile.mkdtemp(prefix="align-")) / "full16k.wav"  # временный файл — не в папке пользователя
    to_wav16k(audio, wav16)
    with wave.open(str(wav16)) as w:
        total = int(w.getnframes() / SR * FPS)
    rows = cut_clips(out, name, units, wav16)
    wav16.unlink(missing_ok=True)
    return finish(out, name, audio, units, rows, total)


def finish(out: Path, name: str, audio: Path, units: list[Unit], rows: list[dict], total: int) -> dict:
    good = [u for u in units if u.status == "ok"]
    text_path = audio.with_suffix(".txt")
    with (out / "manifest.tsv").open("w", encoding="utf-8", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=["audio", "duration", "text", "original", "score"], delimiter="\t")
        wr.writeheader()
        wr.writerows(rows)
    with (out / "all_phrases.tsv").open("w", encoding="utf-8-sig", newline="") as f:
        wr = csv.writer(f, delimiter="\t")
        wr.writerow(["№", "начало, с", "конец, с", "уверенность", "статус", "текст"])
        for u in units:
            wr.writerow([u.idx, round(u.start, 2), round(u.end, 2), round(u.score, 3), u.status, u.text])
    write_review(out, name, rows)

    stats = {
        "запись": audio.name, "текст": text_path.name, "фраз_всего": len(units), "фраз_в_датасете": len(good),
        "минут_в_датасете": round(sum(r["duration"] for r in rows) / 60, 1),
        "минут_записи": round(total / FPS / 60, 1),
        "причины_отсева": {s: sum(1 for u in units if u.status == s) for s in sorted({u.status for u in units}) if s != "ok"},
    }
    (out / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  в датасет вошло: {len(good)} фраз, {stats['минут_в_датасете']} мин из {stats['минут_записи']} мин")
    return stats


def write_review(out: Path, name: str, rows: list[dict]) -> None:
    """Страница для проверки: 60 случайных фраз с плеером и текстом."""
    import random

    sample = sorted(random.Random(7).sample(rows, min(60, len(rows))), key=lambda r: r["audio"])
    items = "\n".join(
        f'<div class="r"><audio controls preload="none" src="{html.escape(r["audio"])}"></audio>'
        f'<div><div class="t">{html.escape(r["original"])}</div>'
        f'<div class="m">{r["duration"]} с · уверенность {r["score"]:.2f} · {html.escape(r["audio"])}</div></div></div>'
        for r in sample)
    page = f"""<!doctype html><meta charset="utf-8"><title>Проверка: {html.escape(name)}</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#1b231e;background:#f4f6f5}}
.r{{display:grid;grid-template-columns:300px 1fr;gap:12px;align-items:center;background:#fff;border:1px solid #dde3df;border-radius:8px;padding:10px;margin:8px 0}}
.t{{font-size:16px}}.m{{color:#66746c;font-size:12px}}audio{{width:100%}}
@media(max-width:640px){{.r{{grid-template-columns:1fr}}}}</style>
<h1>Проверка нарезки: {html.escape(name)}</h1>
<p>Прослушайте фразы. Текст должен совпадать со звуком от начала до конца. Если почти все совпадают — датасет годится для обучения.
Показаны {len(sample)} случайных из {len(rows)}.</p>
{items}"""
    (out / "проверка.html").write_text(page, encoding="utf-8")


def main() -> int:
    if not BOOKS.exists():
        print(f"Нет папки {BOOKS}")
        return 1
    pairs = []
    for audio in sorted(p for p in BOOKS.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXT):
        txt = audio.with_suffix(".txt")
        if not txt.exists():
            print(f"Пропускаю {audio.name}: рядом нет файла {txt.name}")
            continue
        if len(txt.read_bytes().strip()) < 200:
            print(f"Пропускаю {audio.name}: файл {txt.name} пустой — вставьте в него текст этой части и сохраните (Ctrl+S)")
            continue
        if "--recut" in sys.argv:
            if (BOOKS / "dataset" / audio.stem / "all_phrases.tsv").exists():
                pairs.append((audio, txt))
            continue
        if (BOOKS / "dataset" / audio.stem / "manifest.tsv").exists() and not os.environ.get("REDO"):
            print(f"Уже нарезано: {audio.name} (чтобы переделать, удалите папку dataset\\{audio.stem})")
            continue
        pairs.append((audio, txt))
    if not pairs:
        print("Нет новых пар «аудио + текст с тем же именем» в папке books.")
        return 1
    summary = []
    for audio, txt in pairs:
        try:
            summary.append(recut(audio) if "--recut" in sys.argv else process_pair(audio, txt))
        except Exception as exc:  # noqa: BLE001
            print(f"  ОШИБКА: {exc}")
            summary.append({"запись": audio.name, "ошибка": str(exc)})
    total = sum(s.get("минут_в_датасете", 0) for s in summary)
    print(f"\nГотово. Всего в датасете: {total:.1f} мин. Проверьте файл проверка.html в папке books\\dataset\\<имя>.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
