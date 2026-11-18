$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

docker info --format '{{.ServerVersion}}' *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Откройте Docker Desktop, дождитесь запуска и повторите попытку."
    Read-Host "Нажмите Enter" | Out-Null
    exit 1
}

$weights = Get-Item "models/chechen-ctc-v7/model.pt" -ErrorAction SilentlyContinue
if (!$weights -or $weights.Length -lt 1024) {
    Write-Host "Веса модели не загружены. Выполните git lfs install и git lfs pull."
    Read-Host "Нажмите Enter" | Out-Null
    exit 1
}

docker volume create callnotes-chechen-models | Out-Null
if ($LASTEXITCODE -ne 0) { exit 1 }
docker compose -f compose.chechen.yml up -d --build
if ($LASTEXITCODE -ne 0) {
    Read-Host "Сервис не запустился. Нажмите Enter" | Out-Null
    exit 1
}

$url = "http://localhost:8001"
Write-Host "Распознавание чеченской речи: $url"
Start-Process $url
