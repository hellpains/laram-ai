# Скачивает звук с YouTube по ссылке и сохраняет в папку callnotes\books (рядом с текстами книг)
$ErrorActionPreference = "Continue"
# Вывод из Docker приходит в UTF-8 — без этого русский текст в окне превращается в кракозябры
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location -Path $PSScriptRoot
$Books = Join-Path (Split-Path $PSScriptRoot -Parent) "books"
$Out = $Books
function Pause-Exit($code) { Write-Host ""; Read-Host "Нажмите Enter, чтобы закрыть окно" | Out-Null; exit $code }

Write-Host "=== Звук с YouTube ===" -ForegroundColor Green
& docker info *> $null
if ($LASTEXITCODE -ne 0) { Write-Host "Docker Desktop не запущен. Откройте его, дождитесь Engine running и запустите снова." -ForegroundColor Red; Pause-Exit 1 }
if (-not (Test-Path $Out)) { New-Item -ItemType Directory -Path $Out | Out-Null }

# Сборка (повторно — за секунды из кэша)
Write-Host "Подготавливаю загрузчик..."
cmd /c "docker build -q -t callnotes-youtube . 2>&1" | Out-File -FilePath (Join-Path $PSScriptRoot "build-log.txt") -Encoding utf8
if ($LASTEXITCODE -ne 0) { Write-Host "Не удалось подготовить загрузчик. Журнал: youtube\build-log.txt — скажите Claude." -ForegroundColor Red; Pause-Exit 1 }

Write-Host ""
Write-Host "Формат: 1 - MP3 (обычный звук), 2 - WAV 16 кГц моно (для моделей распознавания)"
$choice = Read-Host "Выберите 1 или 2 [по умолчанию 1]"
$format = if ($choice -eq "2") { "wav" } else { "mp3" }

# Выбор папки для сохранения: books или одна из её папок (служебные dataset и train_runs не показываем)
function Choose-Folder {
    $dirs = @(Get-ChildItem -Path $Books -Directory | Where-Object { $_.Name -notin @("dataset", "train_runs") } | Sort-Object Name)
    Write-Host ""
    Write-Host "Куда сохранять звук?"
    Write-Host "  0 - в саму папку books (для нарезки и обучения)"
    for ($i = 0; $i -lt $dirs.Count; $i++) { Write-Host "  $($i + 1) - books\$($dirs[$i].Name)" }
    Write-Host "  или введите название новой папки — она будет создана в books"
    $ans = (Read-Host "Ваш выбор [по умолчанию 0]").Trim()
    if ($ans -eq "" -or $ans -eq "0") { return $Books }
    $n = 0
    if ([int]::TryParse($ans, [ref]$n) -and $n -ge 1 -and $n -le $dirs.Count) { return $dirs[$n - 1].FullName }
    $name = $ans -replace '[\\/:*?"<>|]', '_'
    $path = Join-Path $Books $name
    if (-not (Test-Path $path)) { New-Item -ItemType Directory -Path $path | Out-Null; Write-Host "Создана папка books\$name" -ForegroundColor Green }
    return $path
}

$Out = Choose-Folder
Write-Host "Сохраняю в: $Out" -ForegroundColor Green

while ($true) {
    Write-Host ""
    $url = Read-Host "Вставьте ссылку на видео или плейлист YouTube (пусто - выход)"
    if ([string]::IsNullOrWhiteSpace($url)) { break }
    $url = $url.Trim().Trim('"')
    if ($url -notmatch '^https?://(www\.|m\.|music\.)?(youtube\.com|youtu\.be)/') {
        Write-Host "Это не похоже на ссылку YouTube." -ForegroundColor Yellow
        continue
    }
    Write-Host "Имя файла должно совпадать с именем текста книги, например: eha-buysanash-03"
    $name = Read-Host "Имя файла без расширения (пусто - взять название видео)"
    $name = ($name.Trim() -replace '[\\/:*?"<>|]', '_')
    & docker run --rm -e FORMAT=$format -e "OUT_NAME=$name" -v "${Out}:/out" callnotes-youtube $url
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Готово. Файл в папке $Out" -ForegroundColor Green
    } else {
        Write-Host "Скачать не удалось. Частые причины: YouTube недоступен или очень медленный без VPN, видео закрыто или с ограничением по возрасту." -ForegroundColor Red
    }
}
Start-Process explorer.exe $Out
