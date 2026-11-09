# Запуск CallNotes на этом компьютере с временным HTTPS-адресом.
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
$Compose = @("compose", "-f", "docker-compose.local.yml")

function Pause-Exit($code) { Write-Host ""; Read-Host "Нажмите Enter, чтобы закрыть окно" | Out-Null; exit $code }

Write-Host "=== CallNotes: запуск ===" -ForegroundColor Green

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "Docker Desktop не установлен." -ForegroundColor Yellow
    Write-Host "Установите его (откроется страница загрузки), перезагрузите компьютер и запустите start.bat снова."
    Start-Process "https://www.docker.com/products/docker-desktop/"
    Pause-Exit 1
}

& docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Docker Desktop не запущен. Пытаюсь запустить..." -ForegroundColor Yellow
    $dd = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (Test-Path $dd) { Start-Process $dd }
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 3
        & docker info *> $null
        if ($LASTEXITCODE -eq 0) { break }
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Docker так и не запустился. Откройте Docker Desktop вручную, дождитесь зелёного статуса и запустите start.bat снова." -ForegroundColor Red
        Pause-Exit 1
    }
}

if (-not (Test-Path ".env")) {
    Write-Host "Нет файла настроек .env рядом с start.bat" -ForegroundColor Red
    Pause-Exit 1
}

Write-Host "Собираю и запускаю сервис. Первый раз это занимает 3-10 минут..."
& docker @Compose up -d --build
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Не удалось собрать или запустить сервис. Пришлите текст ошибки выше." -ForegroundColor Red
    Pause-Exit 1
}

Write-Host "Жду временный адрес от Cloudflare..."
$url = $null
for ($i = 0; $i -lt 60; $i++) {
    $logs = cmd /c "docker compose -f docker-compose.local.yml logs tunnel 2>&1" | Out-String
    $found = [regex]::Matches($logs, "https://[a-z0-9-]+\.trycloudflare\.com")
    if ($found.Count -gt 0) { $url = $found[$found.Count - 1].Value; break }
    Start-Sleep -Seconds 2
}

$pass = ""
foreach ($line in Get-Content ".env" -Encoding UTF8) {
    if ($line -like "APP_PASSWORD=*") { $pass = $line.Substring(13) }
}

$lines = @(
    "CallNotes запущен $(Get-Date -Format 'dd.MM.yyyy HH:mm')",
    "",
    "На этом компьютере:   http://localhost:8000",
    "Временный адрес:      $(if ($url) { $url } else { 'не получен, см. docker compose -f docker-compose.local.yml logs tunnel' })",
    "Логин:                любой",
    "Пароль:               $pass",
    "",
    "Для Битрикс24 (исходящий вебхук, событие ONVOXIMPLANTCALLEND):",
    "Адрес обработчика:    $(if ($url) { "$url/webhooks/bitrix" } else { '-' })",
    "",
    "Временный адрес меняется после каждого перезапуска. Тогда обновите его в исходящем вебхуке Битрикс24."
)
$lines | Set-Content -Path "АДРЕС.txt" -Encoding UTF8
Write-Host ""
$lines | ForEach-Object { Write-Host $_ }
Write-Host ""
Write-Host "Эти данные сохранены в файл АДРЕС.txt рядом с start.bat" -ForegroundColor Green
Start-Process "http://localhost:8000"
Pause-Exit 0
