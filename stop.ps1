# Остановка CallNotes. Данные (база звонков) сохраняются.
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
& docker compose -f docker-compose.local.yml down
Write-Host ""
Write-Host "Сервис остановлен. База звонков сохранена, при следующем запуске всё будет на месте." -ForegroundColor Green
Read-Host "Нажмите Enter, чтобы закрыть окно" | Out-Null
