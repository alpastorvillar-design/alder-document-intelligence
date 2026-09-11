<#
.SYNOPSIS
One URL, either way round: http://127.0.0.1:8000/ui/dossiers

.DESCRIPTION
There were two, and they quietly broke each other. Two API processes can share
this database and not share a filesystem - the container with its volume, a
host process with a local folder - so a document uploaded through one is
registered in a row the other can read while its bytes sit where only the
first can see them. `POST /process` queues the work and a worker executes it,
so the upload-and-process path fails across the pair, and a report generated
on one answers 404 on the other.

So: one at a time, and on the same port either way.

  -Mode container   everything in Docker. Local Ollama models, PDF, OCR,
                    persistence in volumes. One command. The default.

  -Mode host        the API and the worker run here, Docker keeps only
                    PostgreSQL and the source simulator. Adds the `claude`
                    and `codex` models, because those binaries live on this
                    machine and a Linux container cannot run them.

.EXAMPLE
  .\scripts\dev.ps1
  .\scripts\dev.ps1 -Mode host
  .\scripts\dev.ps1 -Stop
#>
[CmdletBinding()]
param(
    [ValidateSet("container", "host")]
    [string]$Mode = "container",
    [switch]$Stop,
    [switch]$WithN8n
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repo

$url = "http://127.0.0.1:8000/ui/dossiers"

function Stop-HostProcesses {
    # Whatever holds :8000 that is not Docker: a previous run of this script.
    $connections = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
    foreach ($connection in $connections) {
        $process = Get-Process -Id $connection.OwningProcess -ErrorAction SilentlyContinue
        if ($process -and $process.ProcessName -eq "python") {
            Write-Host "  deteniendo la API del host (pid $($process.Id))"
            Stop-Process -Id $process.Id -Force
        }
    }
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*iep.worker.runner*" } |
        ForEach-Object {
            Write-Host "  deteniendo el worker del host (pid $($_.ProcessId))"
            Stop-Process -Id $_.ProcessId -Force
        }
}

if ($Stop) {
    Write-Host "Parando todo."
    Stop-HostProcesses
    docker compose stop
    Write-Host "Los volumenes se conservan: la demo sigue ahi."
    exit 0
}

# PostgreSQL and the source simulator are in Docker in both modes: the corpus
# needs pgvector, and the connectors need a page and an API to read.
$services = @("postgres", "devsources")
if ($Mode -eq "container") { $services += @("api", "worker") }

# Before anything binds :8000: a host-mode run from earlier still holds it,
# and Compose would fail with "port is already allocated" - which reads as a
# broken stack rather than as the previous mode still running. One at a time is
# the whole point of this script.
Stop-HostProcesses

Write-Host "Levantando: $($services -join ', ')"
docker compose up -d --wait @services
if ($WithN8n) { docker compose --profile n8n up -d n8n }

if ($Mode -eq "container") {
    Write-Host ""
    Write-Host "Listo.  $url"
    Write-Host "  modelos: los locales de Ollama (el contenedor llega por host.docker.internal)"
    Write-Host "  el PDF del informe lo genera el chromium de la imagen"
    exit 0
}

# Host mode. The container's API must not also hold the port, and - the part
# that is easy to miss - its worker must not also be consuming the queue, or
# half the jobs get executed by a process whose filesystem this one cannot
# read.
Write-Host "Deteniendo la API y el worker del contenedor para no duplicarlos."
docker compose stop api worker | Out-Null

$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "No hay venv en .venv - crea uno e instala requirements-dev.lock" }

# The object store and the report directory live here in this mode, so both
# processes below read and write the same bytes.
New-Item -ItemType Directory -Force -Path "var\objects", "var\reports" | Out-Null

# And the bytes already in the volume have to come along. The database is the
# same one, so every document and report seeded in container mode is in a row
# this process can read, naming a file it cannot - the evidence viewer and the
# report would answer 404 on the URL that had just worked. `docker compose cp`
# merges into an existing directory instead of nesting inside it, and the
# trailing `/.` says so explicitly; nothing already here is deleted. About
# 12 MB for the seeded corpus.
$present = docker compose ps -a --format "{{.Service}}" 2>$null
if ($present -contains "api") {
    Write-Host "Trayendo objetos e informes del volumen (la base de datos es la misma)."
    docker compose cp api:/var/lib/iep/objects/. var\objects 2>&1 | Out-Null
    docker compose cp api:/var/lib/iep/reports/. var\reports 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "No se pudo copiar: las evidencias sembradas en el contenedor daran 404 aqui"
    }
}
else {
    Write-Warning "No existe el contenedor api, asi que no hay corpus que traer - arranca antes el modo contenedor si lo quieres"
}

$env:IEP_DATABASE_URL = "postgresql+psycopg://iep:iep@127.0.0.1:55432/iep"
$env:IEP_STORAGE_ROOT = "var/objects"
$env:IEP_REPORT_ROOT = "var/reports"
$env:IEP_REGISTRY_API_BASE_URL = "http://127.0.0.1:8080"
$env:IEP_EMBEDDING_PROVIDER = "ollama"
$env:IEP_OLLAMA_EMBEDDING_MODEL = "qwen3-embedding:4b"
$env:IEP_RAG_PROVIDER = "cli"
$env:IEP_RAG_CLI_TOOL = "claude"

# `pytesseract` calls the binary by name, so OCR needs it on the PATH of these
# two processes - a scan that cannot be read is not a rule that fires, it is a
# document that fails.
$tesseract = "C:\Program Files\Tesseract-OCR"
if (Test-Path $tesseract) { $env:PATH = "$tesseract;$env:PATH" }
else { Write-Warning "Tesseract no esta en $tesseract - los escaneos no se leeran" }

# Chrome renders the report to PDF here; the image's chromium is not on this
# machine's PATH.
foreach ($candidate in @(
    "C:\Program Files\Google\Chrome\Application\chrome.exe",
    "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
)) {
    if (Test-Path $candidate) { $env:IEP_PDF_RENDERER = $candidate; break }
}
if (-not $env:IEP_PDF_RENDERER) { Write-Warning "Sin Chrome: el PDF del informe respondera 503" }

# The worker is a second process and needs the same answers. Every value is
# read back out of this process's environment rather than written again here:
# two of them used to be spelled out a second time, and a worker indexing at a
# different width than the API searches at is the failure this whole script is
# about - silent, because vector search compares only what was made with the
# same configuration and simply finds nothing.
Write-Host "Arrancando el worker en una ventana aparte."
$inherited = @(
    "IEP_DATABASE_URL",
    "IEP_STORAGE_ROOT",
    "IEP_REPORT_ROOT",
    "IEP_REGISTRY_API_BASE_URL",
    "IEP_EMBEDDING_PROVIDER",
    "IEP_OLLAMA_EMBEDDING_MODEL",
    "PATH"
)
$assignments = ($inherited | ForEach-Object {
    "`$env:$_='$([Environment]::GetEnvironmentVariable($_))'"
}) -join "; "
$workerArgs = "-NoExit", "-Command",
    "Set-Location '$repo'; $assignments; & '$python' -m iep.worker.runner"
Start-Process powershell -ArgumentList $workerArgs | Out-Null

Write-Host ""
Write-Host "Listo.  $url"
Write-Host "  modelos: los locales de Ollama, mas Haiku/Sonnet/Opus y los de codex"
Write-Host "  el PDF del informe lo genera tu Chrome"
Write-Host "  Ctrl+C aqui para la API; el worker esta en la otra ventana"
Write-Host ""
& $python -m uvicorn iep.api.app:create_app --factory --host 127.0.0.1 --port 8000
