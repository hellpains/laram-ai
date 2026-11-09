# Тест распознавания чеченской речи моделью Omnilingual ASR (Meta) на этом компьютере.
$ErrorActionPreference = "Continue"
# Вывод из Docker приходит в UTF-8 — без этого русский текст в окне превращается в кракозябры
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location -Path $PSScriptRoot
$Samples = Join-Path (Split-Path $PSScriptRoot -Parent) "samples"
function Pause-Exit($code) { Write-Host ""; Read-Host "Нажмите Enter, чтобы закрыть окно" | Out-Null; exit $code }

Write-Host "=== Тест чеченской модели ===" -ForegroundColor Green
& docker info *> $null
if ($LASTEXITCODE -ne 0) { Write-Host "Docker Desktop не запущен. Откройте его, дождитесь Engine running и запустите снова." -ForegroundColor Red; Pause-Exit 1 }
if (-not (Test-Path $Samples)) { New-Item -ItemType Directory -Path $Samples | Out-Null }
$audio = Get-ChildItem -Path $Samples -File | Where-Object { $_.Extension -match '^\.(mp3|wav|ogg|oga|opus|m4a|aac|amr|flac|webm|mp4|3gp|wma)$' }
if (-not $audio) {
    Write-Host "Папка samples пуста. Положите туда записи с чеченской речью и запустите снова." -ForegroundColor Yellow
    Start-Process explorer.exe $Samples
    Pause-Exit 1
}

Write-Host "Собираю стенд. Первый раз скачивается около 3-5 ГБ, это может занять 10-30 минут..."
$log = Join-Path $PSScriptRoot "build-log.txt"
cmd /c "docker build --progress=plain -t callnotes-chechen . 2>&1" | Tee-Object -FilePath $log
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Сборка не удалась. Полный журнал сохранён в chechen\build-log.txt — скажите Claude, он его прочитает." -ForegroundColor Red
    Pause-Exit 1
}

# Видеокарта NVIDIA: проверяем, видит ли её Docker. Если нет — работаем на процессоре.
$GpuArgs = @()
$check = cmd /c "docker run --rm --gpus all --entrypoint python callnotes-chechen -c ""import torch; print('CUDA_OK' if torch.cuda.is_available() else 'CUDA_NO')"" 2>&1"
if (($check | Out-String) -cmatch "CUDA_OK") {
    $GpuArgs = @("--gpus", "all")
    Write-Host "Видеокарта подключена — работаем на ней." -ForegroundColor Green
} else {
    Write-Host "Видеокарта Docker недоступна — работаем на процессоре (медленнее)." -ForegroundColor Yellow
}

$model = if ($env:CHECHEN_MODEL) { $env:CHECHEN_MODEL } elseif ($GpuArgs.Count -gt 0) { "omniASR_LLM_Unlimited_3B_v2" } else { "omniASR_LLM_Unlimited_1B_v2" }
Write-Host "Модель: $model"
# Дообученные модели лежат в callnotes\models\<имя>\model.pt
$Models = Join-Path (Split-Path $PSScriptRoot -Parent) "models"
$local = @()
if (Test-Path $Models) { $local = @(Get-ChildItem -Path $Models -Directory | Where-Object { Test-Path (Join-Path $_.FullName "model.pt") } | Sort-Object Name -Descending) }
$localArg = ""
if ($local.Count -gt 0) {
    Write-Host ""
    Write-Host "Какой моделью распознавать?"
    for ($i = 0; $i -lt $local.Count; $i++) { Write-Host "  $($i + 1) - дообученная: $($local[$i].Name)" }
    Write-Host "  0 - исходная модель Meta ($model)"
    $pick = Read-Host "Выберите номер [по умолчанию 1]"
    if ($pick -ne "0") {
        $n = 1; [int]::TryParse($pick, [ref]$n) | Out-Null
        if ($n -lt 1 -or $n -gt $local.Count) { $n = 1 }
        $localArg = "-e LOCAL_MODEL=/models/$($local[$n - 1].Name)/model.pt -v ""${Models}:/models:ro"""
        Write-Host "Модель: дообученная $($local[$n - 1].Name) + русская GigaAM, склейка через YandexGPT"
    }
}

# Ключи Yandex из настроек приложения — для склейки чеченской и русской расшифровок через YandexGPT
$EnvFile = Join-Path (Split-Path $PSScriptRoot -Parent) ".env"
$envArg = ""
if (Test-Path $EnvFile) { $envArg = "--env-file ""$EnvFile""" }

$runLog = Join-Path $PSScriptRoot "run-log.txt"
$gpuFlag = $GpuArgs -join " "
cmd /c "docker run --rm $gpuFlag $envArg -e MODEL=$model $localArg -v ""${Samples}:/samples"" -v callnotes-chechen-models:/root/.cache callnotes-chechen 2>&1" | Tee-Object -FilePath $runLog
if ($LASTEXITCODE -ne 0) { Write-Host "Ошибка. Полный журнал сохранён в chechen\run-log.txt — скажите Claude, он его прочитает." -ForegroundColor Red }
if ($LASTEXITCODE -eq 0) {
    $latest = Get-ChildItem -Path (Join-Path $Samples "results") -Filter "результат-*.txt" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($latest) { Start-Process notepad.exe $latest.FullName }
}
Pause-Exit $LASTEXITCODE
