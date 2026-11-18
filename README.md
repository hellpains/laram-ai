# Чеченская речь

Локальный веб-интерфейс для расшифровки чеченской речи. Он использует
дообученную модель `chechen-ctc-v7` на базе Omnilingual ASR CTC 300M v2
с языком `che_Cyrl`.

## Запуск на Windows

1. Запустите Docker Desktop.
2. Если проект только что клонирован, выполните `git lfs install` и `git lfs pull`
   в корне проекта. Веса модели занимают около 1,3 ГБ.
3. Запустите `start.bat` или выполните команду ниже.
4. Откройте [http://localhost:8001](http://localhost:8001).

```powershell
docker volume create callnotes-chechen-models
docker compose -f compose.chechen.yml up -d --build
```

Нажмите «Начать запись» и разрешите доступ к микрофону. Нажатие
«Остановить и распознать» отправляет запись на обработку. Можно также
выбрать готовый аудиофайл. Предельная длительность — 5 минут,
предельный размер запроса — 32 МБ.

При первой расшифровке модель загружается в память. Токенизатор
скачивается при необходимости и сохраняется в томе `callnotes-chechen-models`.
Аудио обрабатывается локально и удаляется после завершения запроса.
Для остановки запустите `stop.bat`.

Сервис настроен на чеченский язык. На записи на другом языке модель
может выдать ошибочный текст.

Для автоматической правки добавьте `GEMINI_API_KEY` в локальный `.env`
и запустите `start.bat` снова. При включённой правке в Gemini 4 Hydrogen
отправляется текст каждой расшифровки. Аудио обрабатывается локально.
В интерфейсе можно раскрыть исходный текст. При ошибке API приложение
покажет исходную расшифровку.

## Подготовка чеченского корпуса «Делан Йозанаш»

Импорт сохраняет EPUB и пары MP3/TXT по главам в `books/bible-che`.
Скрипт выравнивания использует GPU и сохраняет отфильтрованные клипы
WAV 16 кГц, манифест и страницу проверки для каждой главы.

```powershell
python chechen/import_bible.py
docker build --target tools -t callnotes-chechen chechen
docker volume create callnotes-chechen-models
$root = (Get-Location).Path
docker run --rm --gpus all -e MIN_SCORE=0.30 -e BOOKS_DIR=/corpus/pairs `
  -v "${root}/chechen:/tools:ro" -v "${root}/books/bible-che:/corpus" `
  -v callnotes-chechen-models:/root/.cache --entrypoint python `
  callnotes-chechen /tools/prepare_bible.py
```

Повторный запуск использует имеющиеся файлы и пропускает завершённые
главы. Готовые записи лежат в `books/bible-che/pairs/dataset`. Итоговые
`train`, `dev`, `test` лежат в `books/bible-che/splits`. Они разделены
по книгам, а пути в TSV рассчитаны на то же монтирование `/corpus`.
Для оценки на живой речи используйте отдельные разговорные записи.
