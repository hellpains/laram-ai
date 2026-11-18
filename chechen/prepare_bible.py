"""Align imported Bible chapters and export disjoint splits by book."""
import csv
import json
from time import sleep
import wave

from align import BOOKS, process_pair

DEV_BOOKS = {"Mark", "Ruth"}
TEST_BOOKS = {"Jas", "Jonah", "Phil"}


def export_splits(root, stems):
    split_dir = root / "splits"
    split_dir.mkdir(exist_ok=True)
    totals = {}
    for split in ("train", "dev", "test"):
        count = frames = 0
        with (
            (split_dir / f"{split}.tsv").open("w", encoding="utf-8", newline="\n") as audio,
            (split_dir / f"{split}.wrd").open("w", encoding="utf-8", newline="\n") as words,
        ):
            audio.write(f"{BOOKS / 'dataset'}\n")
            for stem in sorted(stems):
                book = stem.split("-")[2]
                group = "dev" if book in DEV_BOOKS else "test" if book in TEST_BOOKS else "train"
                if group != split:
                    continue
                chapter = BOOKS / "dataset" / stem
                with (chapter / "manifest.tsv").open(encoding="utf-8") as manifest:
                    for row in csv.DictReader(manifest, delimiter="\t"):
                        if not row["text"].strip():
                            continue
                        with wave.open(str(chapter / row["audio"])) as clip:
                            assert (clip.getframerate(), clip.getnchannels(), clip.getsampwidth()) == (16000, 1, 2)
                            size = clip.getnframes()
                        audio.write(f"{stem}/{row['audio']}\t{size}\n")
                        words.write(f"{row['text']}\n")
                        count += 1
                        frames += size
        totals[split] = {"clips": count, "hours": round(frames / 16000 / 3600, 3)}
    return totals


def main():
    root = BOOKS.parent
    completed = set()
    errors = {}
    report_path = root / "sources.json"
    while True:
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            report = None
        expected = {row["chapter"] for row in report["pairs"]} if report else None
        for audio in sorted(BOOKS.glob("bible-che-*.mp3")):
            stem = audio.stem
            text = audio.with_suffix(".txt")
            if stem in completed or stem in errors or not text.exists():
                continue
            if expected is not None and stem not in expected:
                continue
            if not (BOOKS / "dataset" / stem / "stats.json").exists():
                try:
                    process_pair(audio, text)
                except Exception as exc:
                    errors[stem] = str(exc)
                    print(f"Failed {stem}: {exc}", flush=True)
                    continue
            completed.add(stem)
        if expected is not None and expected <= completed | errors.keys():
            break
        sleep(5)
    summary = {"chapters_aligned": len(completed), "alignment_errors": errors,
               "download_errors": report["failed"], "splits": export_splits(root, completed & expected)}
    (root / "dataset-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if errors or report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
