"""Дообучение Omnilingual ASR (CTC 300M) на нарезанных книгах из /books/dataset.

Шаги:
  1. Собирает все books/dataset/*/manifest.tsv, делит фразы на обучение (train) и проверку (dev).
  2. Проверяет, какие буквы знает токенизатор модели (в первую очередь — как писать «палочку»).
  3. Пишет данные в формате рецепта (train.tsv/.wrd, dev.tsv/.wrd), карточку датасета и конфиг.
  4. Замер «до обучения»: исходная модель на dev.
  5. Обучение официальным рецептом Meta. Если не хватает видеопамяти — уменьшает батч и пробует снова.

Результаты: /books/train_runs/<дата-время>/ (журналы, конфиги, чекпоинты).
"""
from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

BOOKS = Path(os.environ.get("BOOKS_DIR", "/books"))
OASR = Path(os.environ.get("OASR_DIR", "/opt/oasr"))
MODEL = os.environ.get("TRAIN_MODEL", "omniASR_CTC_300M_v2")
TOKENIZER = "omniASR_tokenizer_written_v2"
DATASET_NAME = "chechen_local"
SR = 16000
EPOCHS = float(os.environ.get("EPOCHS", "30"))
LR = os.environ.get("LR", "3e-05")
MODELS = Path(os.environ.get("MODELS_DIR", "/models"))  # куда выносить обученные модели
TEST_PREFIX = "test-"  # записи с таким именем идут только в проверку (никогда в обучение) и меряются отдельно
MODE = os.environ.get("TRAIN_MODE", "continue")  # continue — дообучить последнюю модель на новом; full — заново на всём
REPLAY_RATIO = 4.0  # на минуту новых данных — столько минут старых, чтобы модель не забыла выученное
REPLAY_MIN_SEC = 30 * 60
DEV_EVERY = 12  # каждая 12-я фраза уходит в проверку и никогда не участвует в обучении
PALOCHKA_VARIANTS = ["ӏ", "Ӏ", "i", "I", "1", "l"]


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------- 1. данные ----------

def collect_rows() -> list[dict]:
    rows = []
    for manifest in sorted((BOOKS / "dataset").glob("*/manifest.tsv")):
        book = manifest.parent.name
        with manifest.open(encoding="utf-8") as f:
            for r in csv.DictReader(f, delimiter="\t"):
                path = manifest.parent / r["audio"]
                if not path.exists() or not r["text"].strip():
                    continue
                with wave.open(str(path)) as w:
                    n = w.getnframes()
                rows.append({"book": book, "rel": f"{book}/{r['audio']}", "samples": n, "text": r["text"].strip()})
    return rows


# ---------- 2. алфавит токенизатора ----------

def tokenizer_alphabet(texts: list[str]) -> tuple[dict[str, str], list[str]]:
    """Возвращает (замены символов, неизвестные модели символы)."""
    from fairseq2.data.tokenizers.hub import load_tokenizer

    tok = load_tokenizer(TOKENIZER)
    enc = tok.create_encoder()
    unk = tok.vocab_info.unk_idx

    def known(ch: str) -> bool:
        try:
            ids = enc(ch)
            ids = ids.tolist() if hasattr(ids, "tolist") else list(ids)
            return len(ids) > 0 and unk not in ids
        except Exception:  # noqa: BLE001
            return False

    mapping: dict[str, str] = {}
    pal = next((v for v in PALOCHKA_VARIANTS if known(v)), None)
    if pal is None:
        log("  ВНИМАНИЕ: модель не знает ни одного варианта «палочки» — она будет убрана из текста")
        mapping["ӏ"] = ""
    elif pal != "ӏ":
        mapping["ӏ"] = pal
    log(f"  «палочка» в модели пишется как: {pal!r}")

    chars = sorted({c for t in texts for c in t if not c.isspace()})
    unknown = [c for c in chars if c not in mapping and not known(c)]
    for c in unknown:
        mapping[c] = ""
    if unknown:
        log(f"  символы, которых модель не знает (будут убраны): {''.join(unknown)}")
    return mapping, unknown


def apply_map(text: str, mapping: dict[str, str]) -> str:
    for a, b in mapping.items():
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text).strip()


# ---------- 3. файлы для рецепта ----------

def write_split(data_dir: Path, split: str, rows: list[dict]) -> None:
    with (data_dir / f"{split}.tsv").open("w", encoding="utf-8", newline="\n") as f:
        f.write(f"{BOOKS / 'dataset'}\n")
        for r in rows:
            f.write(f"{r['rel']}\t{r['samples']}\n")
    with (data_dir / f"{split}.wrd").open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(r["text"] + "\n")


def write_card(data_dir: Path) -> None:
    card = OASR / "src" / "omnilingual_asr" / "cards" / "datasets" / f"{DATASET_NAME}.yaml"
    card.write_text(
        f"name: {DATASET_NAME}\n"
        f"dataset_family: manifest_asr_dataset\n"
        f"dataset_config:\n  data: {data_dir}\n"
        f"tokenizer_ref: {TOKENIZER}\n",
        encoding="utf-8",
    )


def dataset_block(max_elems: int, shuffle_window: int = 1000) -> str:
    return f"""dataset:
  name: "{DATASET_NAME}"
  {{splits}}
  storage_mode: "MANIFEST"
  task_mode: "ASR"
  manifest_storage_config:
    read_text: true
  asr_task_config:
    min_audio_len: 16_000
    max_audio_len: 480_000
    max_num_elements: {max_elems}
    normalize_audio: true
    remove_unknown: false  # неизвестные символы убираем сами при подготовке (в рецепте эта опция сломана)
    batch_shuffle_window: 1
    example_shuffle_window: {shuffle_window}
"""


def regime_steps(steps: int) -> tuple[int, int, int, int]:
    """(шагов, метрики каждые, проверка каждые, сохранение каждые) — рецепт требует кратности метрикам."""
    publish = 10 if steps < 500 else 25
    validate = max(publish, round(steps / 10 / publish) * publish)
    steps = max(validate, round(steps / validate) * validate)
    checkpoint = max(validate, round(steps / 2 / validate) * validate)
    return steps, publish, validate, checkpoint


def train_config(steps: int, max_elems: int, grad_acc: int, model: str = MODEL, lr: str = LR) -> str:
    steps, publish, every, checkpoint = regime_steps(steps)
    ds = dataset_block(max_elems).replace("{splits}", 'train_split: "train"\n  valid_split: "dev"')
    return f"""model:
  name: "{model}"

{ds}
tokenizer:
  name: "{TOKENIZER}"

optimizer:
  config:
    lr: {lr}

trainer:
  freeze_encoder_for_n_steps: 0
  mixed_precision:
    dtype: "torch.bfloat16"
  grad_accumulation:
    num_batches: {grad_acc}

regime:
  num_steps: {steps}
  validate_after_n_steps: 0
  validate_every_n_steps: {every}
  checkpoint_after_n_steps: 0
  checkpoint_every_n_steps: {checkpoint}
  publish_metrics_after_n_steps: 0
  publish_metrics_every_n_steps: {publish}
"""


def eval_config(model: str, split: str = "dev") -> str:
    ds = dataset_block(480_000, shuffle_window=1).replace("{splits}", f'valid_split: "{split}"')
    return f"""model:
  name: "{model}"

tokenizer:
  name: "{TOKENIZER}"

{ds}"""


# ---------- 4-5. запуск рецептов ----------

def run(cmd: list[str], log_path: Path) -> tuple[int, str]:
    log(f"  $ {' '.join(cmd)}")
    tail: list[str] = []
    with log_path.open("w", encoding="utf-8") as lf:
        proc = subprocess.Popen(cmd, cwd=OASR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                env={**os.environ, "PYTHONPATH": str(OASR)})
        assert proc.stdout
        for line in proc.stdout:
            lf.write(line)
            lf.flush()
            if re.search(r"(?i)(wer|uer|loss|step|error|exception|valid|checkpoint|memory)", line):
                print("    " + line.rstrip()[:220], flush=True)
            tail = (tail + [line])[-200:]
        code = proc.wait()
    return code, "".join(tail)


def metrics_from(text: str) -> dict:
    """Достаёт последние значения wer/uer (ошибка в словах/буквах) из журнала рецепта."""
    out = {}
    for key in ("uer", "wer"):
        vals = re.findall(rf"(?i)\b{key}\b[^0-9\-]{{0,6}}([0-9]+(?:\.[0-9]+)?)", text)
        if vals:
            out[key] = float(vals[-1])
    return out


def evaluate(run_dir: Path, model: str, split: str, tag: str) -> dict:
    cfg = run_dir / f"eval_{tag}_{split}.yaml"
    cfg.write_text(eval_config(model, split), encoding="utf-8")
    code, tail = run([sys.executable, "-m", "workflows.recipes.wav2vec2.asr.eval", str(run_dir / f"eval_{tag}_{split}"),
                      "--config-file", str(cfg)], run_dir / f"eval_{tag}_{split}.log")
    return metrics_from(tail) if code == 0 else {"error": f"см. eval_{tag}_{split}.log"}


def export_model(out_dir: Path, run_dir: Path) -> Path | None:
    """Копирует веса последнего шага в models/chechen-ctc-vN и регистрирует их для замера."""
    ckpts = sorted(out_dir.glob("ws_*/checkpoints/step_*"), key=lambda p: int(p.name.split("_")[1]))
    if not ckpts:
        return None
    weights = ckpts[-1] / "model" / "pp_00" / "tp_00" / "sdp_00.pt"
    if not weights.exists():
        return None
    target = weights
    if MODELS.exists():
        n = 1 + max([int(m) for p in MODELS.glob("chechen-ctc-v*") for m in re.findall(r"v(\d+)$", p.name)] or [0])
        dst = MODELS / f"chechen-ctc-v{n}"
        dst.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy(weights, dst / "model.pt")
        yaml_src = ckpts[-1].parent / "model.yaml"
        if yaml_src.exists():
            shutil.copy(yaml_src, dst / "model.yaml")
        target = dst / "model.pt"
        log(f"  модель сохранена: models\\{dst.name}")
    card = OASR / "src" / "omnilingual_asr" / "cards" / "models" / "chechen_trained_eval.yaml"
    card.write_text(f"name: chechen_trained_eval\nmodel_family: wav2vec2_asr\nmodel_arch: 300m_v2\n"
                    f"checkpoint: {target}\ntokenizer_ref: {TOKENIZER}\n", encoding="utf-8")
    return target


def main() -> int:
    import torch

    if not torch.cuda.is_available():
        log("Видеокарта недоступна в Docker — обучение на процессоре займёт слишком много времени. Остановлено.")
        return 1
    log(f"Видеокарта: {torch.cuda.get_device_name(0)}, памяти {torch.cuda.get_device_properties(0).total_memory / 2**30:.1f} ГБ")

    rows = collect_rows()
    if not rows:
        log("Нет нарезанных фраз. Сначала запустите align-book.bat.")
        return 1
    run_dir = BOOKS / "train_runs" / datetime.now().strftime("%Y%m%d-%H%M")
    data_dir = run_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    log("\n1. Данные")
    mapping, unknown = tokenizer_alphabet([r["text"] for r in rows])
    for r in rows:
        r["text"] = apply_map(r["text"], mapping)
    rows = [r for r in rows if r["text"]]
    test = [r for r in rows if r["book"].startswith(TEST_PREFIX)]
    rest = [r for r in rows if not r["book"].startswith(TEST_PREFIX)]
    dev = [r for i, r in enumerate(rest) if i % DEV_EVERY == DEV_EVERY // 2]
    train = [r for i, r in enumerate(rest) if i % DEV_EVERY != DEV_EVERY // 2]
    start_model, lr, epochs = MODEL, LR, EPOCHS
    prev_models = sorted(MODELS.glob("chechen-ctc-v*/model.pt"),
                         key=lambda p: int(re.findall(r"v(\d+)", p.parent.name)[0])) if MODELS.exists() else []
    if MODE == "continue" and prev_models:
        prev = prev_models[-1]
        info_file = prev.parent / "итог_обучения.json"
        seen = set(json.loads(info_file.read_text(encoding="utf-8")).get("books", [])) if info_file.exists() else set()
        if not seen:
            log(f"  У модели {prev.parent.name} нет списка книг, на которых она училась — обучаю заново на всём.")
        else:
            new_train = [r for r in train if r["book"] not in seen]
            if not new_train:
                log(f"Новых данных нет: модель {prev.parent.name} уже училась на всех нарезанных записях.")
                log("Добавьте новые записи в books и нарежьте их (align-book.bat), либо выберите обучение заново.")
                return 1
            import random
            old_rows = [r for r in train if r["book"] in seen]
            random.Random(7).shuffle(old_rows)
            new_sec = sum(r["samples"] for r in new_train) / SR
            need = max(REPLAY_MIN_SEC, new_sec * REPLAY_RATIO)
            replay, acc = [], 0.0
            for r in old_rows:
                if acc >= need:
                    break
                replay.append(r)
                acc += r["samples"] / SR
            train = new_train + replay
            card = OASR / "src" / "omnilingual_asr" / "cards" / "models" / "chechen_start.yaml"
            card.write_text(f"name: chechen_start\nmodel_family: wav2vec2_asr\nmodel_arch: 300m_v2\n"
                            f"checkpoint: {prev}\ntokenizer_ref: {TOKENIZER}\n", encoding="utf-8")
            start_model, lr, epochs = "chechen_start", os.environ.get("LR", "1e-05"), float(os.environ.get("EPOCHS", "15"))
            log(f"  Режим: дообучение {prev.parent.name}. Новое: {new_sec / 60:.1f} мин "
                f"({', '.join(sorted({r['book'] for r in new_train}))}); для памяти добавлено {acc / 60:.1f} мин старых фраз.")
    else:
        log("  Режим: обучение заново на всех данных.")
    write_split(data_dir, "train", train)
    write_split(data_dir, "dev", dev)
    splits = ["dev"]
    if test:
        write_split(data_dir, "test", test)
        splits.append("test")
        log(f"  отдельная проверка (живая речь, в обучение не идёт): {len(test)} фраз, "
            f"{sum(r['samples'] for r in test) / SR / 60:.1f} мин")
    write_card(data_dir)
    train_sec = sum(r["samples"] for r in train) / SR
    books = sorted({r["book"] for r in rows if not r["book"].startswith(TEST_PREFIX)})
    log(f"  книг: {len(books)}; обучение: {len(train)} фраз, {train_sec / 60:.1f} мин; проверка: {len(dev)} фраз")

    info = {"model": MODEL, "books": books, "train_phrases": len(train), "dev_phrases": len(dev),
            "test_phrases": len(test),
            "train_minutes": round(train_sec / 60, 1), "char_mapping": mapping, "lr": lr, "epochs": epochs,
            "mode": "continue" if start_model == "chechen_start" else "full"}

    log("\n2. Замер ДО обучения (исходная модель)")
    info["before"] = {sp: evaluate(run_dir, MODEL, sp, "before") for sp in splits}
    log(f"  исходная модель Meta: {info['before']}")
    prev = sorted(MODELS.glob("chechen-ctc-v*/model.pt"), key=lambda p: int(re.findall(r"v(\d+)", p.parent.name)[0])) \
        if MODELS.exists() else []
    if prev:
        card = OASR / "src" / "omnilingual_asr" / "cards" / "models" / "chechen_prev_eval.yaml"
        card.write_text(f"name: chechen_prev_eval\nmodel_family: wav2vec2_asr\nmodel_arch: 300m_v2\n"
                        f"checkpoint: {prev[-1]}\ntokenizer_ref: {TOKENIZER}\n", encoding="utf-8")
        info["previous_model"] = prev[-1].parent.name
        info["previous"] = {sp: evaluate(run_dir, "chechen_prev_eval", sp, "previous") for sp in splits}
        log(f"  прошлая обученная модель ({prev[-1].parent.name}): {info['previous']}")

    log("\n3. Обучение")
    max_elems, grad_acc = 1_600_000, 2  # ~100 с звука в батче, 2 батча на шаг
    for attempt in range(3):
        audio_per_step = max_elems / SR * grad_acc * 0.7  # батчи заполняются не полностью
        steps = int(min(5000, max(300, epochs * train_sec / audio_per_step)))
        cfg = run_dir / "train.yaml"
        cfg.write_text(train_config(steps, max_elems, grad_acc, start_model, lr), encoding="utf-8")
        log(f"  шагов: {steps}, батч до {max_elems // SR} с звука × {grad_acc}")
        t0 = time.time()
        out_dir = run_dir / f"train_{attempt}"
        code, tail = run([sys.executable, "-m", "workflows.recipes.wav2vec2.asr", str(out_dir),
                          "--config-file", str(cfg)], run_dir / f"train_{attempt}.log")
        info["train_minutes_spent"] = round((time.time() - t0) / 60, 1)
        if code == 0:
            info["output"] = str(out_dir)
            exported = export_model(out_dir, run_dir)
            if exported:
                info["model_file"] = str(exported)
                log("\n4. Замер ПОСЛЕ обучения")
                info["after"] = {sp: evaluate(run_dir, "chechen_trained_eval", sp, "after") for sp in splits}
            else:
                info["after"] = {"dev": metrics_from(tail)}
            break
        if re.search(r"(?i)out of memory|OutOfMemoryError|CUDA error: out of memory", tail):
            max_elems //= 2
            grad_acc *= 2
            log("  Не хватило видеопамяти — уменьшаю батч и пробую снова.")
            continue
        info["after"] = {"error": f"см. train_{attempt}.log"}
        break

    (run_dir / "итог.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    if info.get("model_file") and Path(info["model_file"]).parent.parent == MODELS:
        (Path(info["model_file"]).parent / "итог_обучения.json").write_text(
            json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    log("\nИтог:")
    log(json.dumps({k: info.get(k) for k in ("train_minutes", "before", "previous_model", "previous", "after",
                                             "train_minutes_spent")},
                   ensure_ascii=False, indent=2))
    log(f"\nВсё сохранено в books\\train_runs\\{run_dir.name}")
    log("uer — доля ошибок в буквах, wer — в словах (меньше = лучше).")
    log("dev — фразы книг, которых модель не видела; test — живая речь (записи test-...), только для проверки.")
    return 0 if "error" not in info.get("after", {}) else 1


if __name__ == "__main__":
    sys.exit(main())
