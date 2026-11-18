Set-Location -Path $PSScriptRoot
docker compose -f compose.chechen.yml down
if ($LASTEXITCODE -ne 0) {
    Read-Host "Не удалось остановить сервис. Нажмите Enter" | Out-Null
    exit 1
}
Write-Host "Сервис распознавания речи остановлен."
