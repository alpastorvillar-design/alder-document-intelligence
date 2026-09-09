[CmdletBinding()]
param(
    [switch]$WithN8n,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$projectName = "iep-demo"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot

docker compose -p $projectName config --quiet
if (-not $SkipBuild) {
    docker compose -p $projectName build
}
docker compose -p $projectName up -d --wait postgres devsources api worker

docker compose -p $projectName exec -T api python -m corpus.generate --out /tmp/corpus
docker compose -p $projectName exec -T api iep seed --corpus /tmp/corpus `
    --call-page-url http://devsources:8080/public/convocatoria.html

foreach ($reference in @("INN-2025-041", "INN-2025-042")) {
    docker compose -p $projectName exec -T api iep process --reference $reference
    docker compose -p $projectName exec -T api iep report --reference $reference
}

if ($WithN8n) {
    docker compose -p $projectName --profile n8n run --rm --no-deps n8n `
        import:workflow --input=/workflows/dossier-review.json
    docker compose -p $projectName --profile n8n run --rm --no-deps n8n `
        update:workflow --id=iep-dossier-review --active=true
    docker compose -p $projectName --profile n8n up -d --wait n8n
}

docker compose -p $projectName ps
docker compose -p $projectName exec -T api iep status
Write-Host "API docs: http://127.0.0.1:8000/docs"
Write-Host "Review queue: http://127.0.0.1:8000/ui/dossiers"
Write-Host "Stop: docker compose -p $projectName --profile n8n down"
