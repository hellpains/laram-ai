# Нарезка аудиокниг на фразы с текстом (датасет для обучения чеченской модели).
# Пары лежат в callnotes\books: одинаковое имя у аудио и у текста, например kniga.wav + kniga.txt
$ErrorActionPreference = "Continue"
# Вывод из Docker приходит в UTF-8 — без этого русский текст в окне превращается в кракозябры
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location -Path $PSScriptRoot
$Books = Join-Path (Split-Path $PSScriptRoot -Parent) "books"
function Pause-Exit($code) { Write-Host ""; Read-Host "Нажмите Enter, чтобы закрыть окно" | Out-Null; exit $code }

Write-Host "=== Нарезка аудиокниги для обучения ===" -ForegroundColor Green
& docker info *> $null
if ($LASTEXITCODE -ne 0) { Write-Host "Docker Desktop не запущен. Откройте его, дождитесь Engine running и запустите снова." -ForegroundColor Red; Pause-Exit 1 }
& docker image inspect callnotes-chechen *> $null
if ($LASTEXITCODE -ne 0) { Write-Host "Сначала один раз запустите test-chechen.bat — он собирает нужный образ." -ForegroundColor Yellow; Pause-Exit 1 }
if (-not (Test-Path $Books)) { New-Item -ItemType Directory -Path $Books | Out-Null }

# Видеокарта NVIDIA: проверяем, видит ли её Docker. Если нет — работаем на процессоре.
$GpuArgs = @()
$check = cmd /c "docker run --rm --gpus all --entrypoint python callnotes-chechen -c ""import torch; print('CUDA_OK' if torch.cuda.is_available() else 'CUDA_NO')"" 2>&1"
if (($check | Out-String) -cmatch "CUDA_OK") {
    $GpuArgs = @("--gpus", "all")
    Write-Host "Видеокарта подключена — работаем на ней." -ForegroundColor Green
} else {
    Write-Host "Видеокарта Docker недоступна — работаем на процессоре (медленнее)." -ForegroundColor Yellow
}

Write-Host "Папка с книгами: $Books"
Write-Host "Первый запуск скачивает модель выравнивания (~1 ГБ). На видеокарте час записи обрабатывается за несколько минут, на процессоре — 10-30 минут."
$tools = $PSScriptRoot
& docker run --rm @GpuArgs -v "${tools}:/tools:ro" -v "${Books}:/books" -v callnotes-chechen-models:/root/.cache `
    --entrypoint python callnotes-chechen /tools/align.py
$code = $LASTEXITCODE
$ds = Join-Path $Books "dataset"
if (Test-Path $ds) { Start-Process explorer.exe $ds }
Pause-Exit $code
