[CmdletBinding()]
param(
    [switch]$WithN8n,
    [switch]$SkipBuild,
    # Delete the demo dossiers before seeding. A dossier under review refuses
    # new documents by design, so this is how the demo is made repeatable.
    [switch]$Fresh
)

$ErrorActionPreference = "Stop"
# The project name is declared in compose.yaml. Overriding it here would
# create a second set of containers and volumes alongside the declared stack.
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot

docker compose config --quiet
if (-not $SkipBuild) {
    docker compose build
}
docker compose up -d --wait postgres devsources api worker

# Every dossier the corpus defines, so the demonstration shows the clean path,
# the fully seeded one, and the three ordinary claims in between.
$references = @(
    "INN-2025-041",
    "INN-2025-042",
    "INN-2025-043",
    "INN-2025-044",
    "INN-2025-045"
)

if ($Fresh) {
    foreach ($reference in $references) {
        docker compose exec -T api iep reset --reference $reference
    }
}

docker compose exec -T api python -m corpus.generate --out /tmp/corpus
docker compose exec -T api iep seed --corpus /tmp/corpus `
    --call-page-url http://devsources:8080/public/convocatoria.html

foreach ($reference in $references) {
    docker compose exec -T api iep process --reference $reference
    docker compose exec -T api iep report --reference $reference
}

if ($WithN8n) {
    # Import while the n8n server is stopped: the local demo uses SQLite and
    # must not have two processes writing the same file. Workflows arrive
    # inactive; only the two webhook entry points need activation.
    docker compose --profile n8n stop n8n
    $workflows = @(
        @{ File = "dossier-review.json"; Id = "iep-dossier-review"; Activate = $true },
        @{ File = "review-queue-digest.json"; Id = "iep-review-queue-digest"; Activate = $false },
        @{ File = "approved-dossier-handoff.json"; Id = "iep-approved-dossier-handoff"; Activate = $true }
    )
    foreach ($workflow in $workflows) {
        docker compose --profile n8n run --rm --no-deps n8n `
            import:workflow --input="/workflows/$($workflow.File)"
        if ($workflow.Activate) {
            docker compose --profile n8n run --rm --no-deps n8n `
                update:workflow --id=$($workflow.Id) --active=true
        }
    }
    docker compose --profile n8n up -d --wait n8n
}

docker compose ps
docker compose exec -T api iep status
Write-Host "API docs: http://127.0.0.1:8000/docs"
Write-Host "Review queue: http://127.0.0.1:8000/ui/dossiers"
Write-Host "Stop: docker compose --profile n8n down"
