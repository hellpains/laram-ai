#!/bin/sh
# YouTube часто меняет сайт, поэтому перед каждым скачиванием обновляем yt-dlp (если нет сети — работаем со старой версией)
pip install -q --no-cache-dir -U "yt-dlp[default]" >/dev/null 2>&1 || true

FORMAT="${FORMAT:-mp3}"
if [ "$FORMAT" = "wav" ]; then
  # WAV 16 кГц моно — формат для моделей распознавания речи
  set -- -x --audio-format wav --postprocessor-args "ExtractAudio:-ar 16000 -ac 1" "$@"
else
  set -- -x --audio-format mp3 --audio-quality 2 "$@"
fi

# Имя файла: заданное пользователем (OUT_NAME) или название видео. В плейлисте добавляется номер видео.
if [ -n "$OUT_NAME" ]; then
  TEMPLATE="/out/${OUT_NAME}.%(ext)s"
  PL_TEMPLATE="/out/${OUT_NAME}-%(playlist_index)02d.%(ext)s"
else
  TEMPLATE="/out/%(title)s [%(id)s].%(ext)s"
  PL_TEMPLATE="$TEMPLATE"
fi

exec yt-dlp --no-progress --newline --windows-filenames --trim-filenames 120 --no-overwrites \
  -f "bestaudio/best" -o "$TEMPLATE" -o "pl_video:$PL_TEMPLATE" "$@"
