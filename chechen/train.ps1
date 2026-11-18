# Дообучение чеченской модели распознавания на нарезанных книгах (books\training-che) на видеокарте.
$ErrorActionPreference = "Continue"
# Вывод из Docker приходит в UTF-8 — без этого русский текст в окне превращается в кракозябры
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location -Path $PSScriptRoot
$Books = Join-Path (Split-Path $PSScriptRoot -Parent) "books"
function Pause-Exit($code) { Write-Host ""; Read-Host "Нажмите Enter, чтобы закрыть окно" | Out-Null; exit $code }

Write-Host "=== Обучение чеченской модели ===" -ForegroundColor Green
& docker info *> $null
if ($LASTEXITCODE -ne 0) { Write-Host "Docker Desktop не запущен. Откройте его, дождитесь Engine running и запустите снова." -ForegroundColor Red; Pause-Exit 1 }
if (-not (Test-Path (Join-Path $Books "training-che\summary.json"))) { Write-Host "Сначала выполните python chechen/prepare_training.py из корня проекта." -ForegroundColor Yellow; Pause-Exit 1 }

Write-Host "Обновляю стенд (первый раз скачиваются рецепты обучения, 1-3 минуты)..."
$log = Join-Path $PSScriptRoot "build-log.txt"
cmd /c "docker build --progress=plain -t callnotes-chechen . 2>&1" | Tee-Object -FilePath $log
if ($LASTEXITCODE -ne 0) { Write-Host "Сборка не удалась. Журнал: chechen\build-log.txt — скажите Claude." -ForegroundColor Red; Pause-Exit 1 }

Write-Host ""
Write-Host "Дообучение v3: один проход по десяти частям «Еха буьйсенаш» и Библии."
Write-Host ""
Write-Host "Во время обучения видеокарта будет загружена полностью. Не запускайте игры и тяжёлые программы." -ForegroundColor Yellow
Write-Host "Окно можно свернуть. Журналы пишутся в books\train_runs."
Write-Host ""
$tools = $PSScriptRoot
$Models = Join-Path (Split-Path $PSScriptRoot -Parent) "models"
if (-not (Test-Path $Models)) { New-Item -ItemType Directory -Path $Models | Out-Null }
$runLog = Join-Path $PSScriptRoot "train-log.txt"
cmd /c "docker run --rm --gpus all --shm-size=8g -e TRAIN_MODE=curated -e EPOCHS=1 -v ""${tools}:/tools:ro"" -v ""${Books}:/books"" -v ""${Models}:/models"" -v callnotes-chechen-models:/root/.cache --entrypoint python callnotes-chechen /tools/train.py 2>&1" | Tee-Object -FilePath $runLog
$code = $LASTEXITCODE
if ($code -ne 0) { Write-Host "Обучение завершилось с ошибкой. Скажите Claude — он прочитает журналы." -ForegroundColor Red }
$runs = Join-Path $Books "train_runs"
if (Test-Path $runs) { Start-Process explorer.exe $runs }
Pause-Exit $code
