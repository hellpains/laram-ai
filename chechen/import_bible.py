"""Download the IBT Chechen edition and pair each available chapter with its text."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
from time import sleep
from urllib.parse import urljoin
from urllib.request import urlopen
from xml.etree import ElementTree as ET
from zipfile import ZipFile

AUDIO = "https://ibt.org.ru/ftpmirror/pub/audio/CHE/CHE/"
EPUB = "https://ibtrussia.org/sites/default/files/ebm/ebook/657c7w7j6/che_cyrillic_compilation.epub"
ROOT = Path(__file__).resolve().parents[1] / "books" / "bible-che"


def fetch(url):
    for delay in (0, 1, 3):
        sleep(delay)
        try:
            with urlopen(url, timeout=45) as response:
                return response.read()
        except OSError as exc:
            error = exc
    raise error


def download(url, target):
    if not target.is_file() or target.stat().st_size == 0:
        data = fetch(url)
        if not data or data.lstrip().startswith((b"<!DOCTYPE", b"<html")):
            raise ValueError(f"Not a media file: {url}")
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(target)


def verses(path):
    result = {}
    with ZipFile(path) as book:
        for name in sorted(book.namelist()):
            if not re.fullmatch(r"xhtml/CHE_\w+\.xhtml", name):
                continue
            ref = None
            pieces = []

            def visit(node):
                nonlocal ref, pieces
                marker = re.fullmatch(r"(\w+)\.(\d+)\.(\d+)", node.get("id", ""))
                if marker:
                    if ref:
                        result[ref] = " ".join("".join(pieces).split())
                    key, chapter, verse = marker.groups()
                    ref = (key.lstrip("_"), int(chapter), int(verse))
                    pieces = []
                tag = node.tag.rsplit("}", 1)[-1]
                classes = node.get("class", "")
                if tag in {"head", "sup", "h1", "h2", "h3", "h4"} or re.search(
                    "note|chapter-number|verse-number", classes
                ):
                    return
                if ref and node.text:
                    pieces.append(node.text)
                for child in node:
                    visit(child)
                    if ref and child.tail:
                        pieces.append(child.tail)
                if ref and tag in {"p", "div", "br"}:
                    pieces.append(" ")

            visit(ET.fromstring(book.read(name)))
            if ref:
                result[ref] = " ".join("".join(pieces).split())
    chapters = {}
    for (name, chapter, _), text in sorted(result.items()):
        if text:
            chapters.setdefault((name, chapter), []).append(text)
    return chapters


def links(url, pattern):
    page = fetch(url).decode("utf-8", errors="replace")
    return sorted(set(re.findall(r'href="(' + pattern + r')"', page)))


def chapter_number(book, file_number):
    # The publisher's archive puts Exodus 10 in file 035 and shifts 11–35 by one.
    if book == "Exod/":
        if file_number == 35:
            return 10
        if 10 <= file_number < 35:
            return file_number + 1
    return file_number


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pilot", action="store_true", help="Download only Genesis chapter 1")
    args = parser.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    epub = ROOT / "source.epub"
    download(EPUB, epub)
    chapters = verses(epub)
    pairs = ROOT / "pairs"
    pairs.mkdir(exist_ok=True)
    books = ["Gen/"] if args.pilot else links(AUDIO, r"[1-3]?[A-Z][A-Za-z]+/")
    with ThreadPoolExecutor(max_workers=4) as pool:
        inventories = list(pool.map(lambda book: links(urljoin(AUDIO, book), r"\d{3}\.mp3"), books))
        jobs, unmatched = [], []
        matched = set()
        for book, files in zip(books, inventories):
            for name in files:
                number = chapter_number(book, int(name[:3]))
                if args.pilot and number != 1:
                    continue
                key = (book.rstrip("/"), number)
                url = urljoin(AUDIO, book + name)
                if key not in chapters:
                    unmatched.append(url)
                else:
                    matched.add(key)
                    jobs.append((url, f"bible-che-{key[0]}-{number:03}", chapters[key]))
        if not jobs:
            raise RuntimeError("No matching chapter pairs found")
        print(f"Found {len(jobs)} audio/text pairs and {len(unmatched)} unmatched recordings", flush=True)

        def save(job):
            url, stem, lines = job
            download(url, pairs / f"{stem}.mp3")
            text = pairs / f"{stem}.txt"
            temporary = text.with_suffix(".txt.part")
            temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
            temporary.replace(text)
            return {"chapter": stem, "audio": url, "verses": len(lines)}

        metadata = []
        failures = []
        def attempt(job):
            try:
                return save(job), None
            except (OSError, ValueError) as exc:
                return None, {"audio": job[0], "error": str(exc)}

        for index, (row, failure) in enumerate(pool.map(attempt, jobs), 1):
            if row:
                metadata.append(row)
            if failure:
                failures.append(failure)
            if index % 25 == 0 or index == len(jobs):
                print(f"{index}/{len(jobs)} checked, {len(failures)} failed", flush=True)
    report = {"text_source": EPUB, "audio_source": AUDIO, "pairs": metadata,
              "audio_without_text": unmatched, "text_without_audio": sorted(set(chapters) - matched),
              "failed": failures}
    target = ROOT / ("pilot.json" if args.pilot else "sources.json")
    marker = target.with_suffix(".json.tmp")
    marker.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    marker.replace(target)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
