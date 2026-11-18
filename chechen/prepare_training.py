"""Prepare the approved corpus without copying the audio files."""
import csv
import json
from pathlib import Path, PurePosixPath
import wave

EHA_PARTS = tuple(f"eha-buysanash-{i:02d}" for i in range(1, 11))
SPLITS = ("train", "dev", "test")


def bible_rows(books, split):
    source = books / "bible-che" / "splits"
    audio = (source / f"{split}.tsv").read_text(encoding="utf-8").splitlines()[1:]
    words = (source / f"{split}.wrd").read_text(encoding="utf-8").splitlines()
    for entry, text in zip(audio, words, strict=True):
        name, count = entry.split("\t")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or not path.parts[0].startswith("bible-che-"):
            raise ValueError(f"Недопустимый путь Библии: {name}")
        yield f"bible-che/pairs/dataset/{name}", int(count), text


def eha_rows(books):
    for part in EHA_PARTS:
        folder = books / "dataset" / part
        with (folder / "manifest.tsv").open(encoding="utf-8", newline="") as manifest:
            for row in csv.DictReader(manifest, delimiter="\t"):
                path = folder / row["audio"]
                with wave.open(str(path)) as audio:
                    if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (16000, 1, 2):
                        raise ValueError(f"Неподдерживаемый формат WAV: {path}")
                    yield path.relative_to(books).as_posix(), audio.getnframes(), row["text"]


def prepare(books):
    bible = {split: list(bible_rows(books, split)) for split in SPLITS}
    eha = list(eha_rows(books))
    sets = {**bible, "train": bible["train"] + eha}
    seen = set()
    for rows in sets.values():
        for rel, frames, text in rows:
            if rel in seen or frames <= 0 or not text.strip() or not (books / rel).is_file():
                raise ValueError(f"Некорректная или повторяющаяся пара: {rel}")
            seen.add(rel)
    target = books / "training-che"
    target.mkdir(exist_ok=True)
    summary = {
        "eha_parts": list(EHA_PARTS),
        "eha_clips": len(eha),
        "eha_hours": round(sum(n for _, n, _ in eha) / 57_600_000, 3),
        "splits": {},
    }
    for split, rows in sets.items():
        (target / f"{split}.tsv").write_text(
            "/books\n" + "".join(f"{rel}\t{n}\n" for rel, n, _ in rows), encoding="utf-8"
        )
        (target / f"{split}.wrd").write_text(
            "".join(" ".join(text.split()) + "\n" for _, _, text in rows), encoding="utf-8"
        )
        summary["splits"][split] = {
            "clips": len(rows), "hours": round(sum(n for _, n, _ in rows) / 57_600_000, 3)
        }
    (target / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    print(json.dumps(prepare(Path(__file__).resolve().parents[1] / "books"), ensure_ascii=False, indent=2))
