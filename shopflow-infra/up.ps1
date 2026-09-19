#requires -version 5.1
<#
.SYNOPSIS
    ShopFlow local stack bootstrap for Windows.

.DESCRIPTION
    Builds every image, starts the whole stack in dependency order, gates on
    health, runs the inventory-owned seed and finishes with the black-box smoke
    test. Equivalent manual commands are in README.md and docs/runbook.md.

.EXAMPLE
    .\up.ps1
    .\up.ps1 -Reset          # drop all volumes first (deterministic restart)
    .\up.ps1 -SkipSmoke      # stop after the seed
#>
[CmdletBinding()]
param(
    [switch]$Reset,
    [switch]$SkipSmoke,
    [int]$Timeout = 180
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$python = if ($env:PYTHON) { $env:PYTHON } else { 'python' }

function Invoke-Step {
    param([string]$Label, [scriptblock]$Action)
    Write-Host "==> $Label" -ForegroundColor Cyan
    & $Action
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

if ($Reset) {
    Invoke-Step 'docker compose down -v (dropping all state)' { docker compose down -v }
}

Invoke-Step 'docker compose up -d --build' { docker compose up -d --build }
Invoke-Step "waiting for every /health endpoint (timeout ${Timeout}s)" { & $python scripts/wait_for_health.py --timeout $Timeout }
Invoke-Step 'inventory-owned deterministic seed' { & $python scripts/seed.py }

if (-not $SkipSmoke) {
    Invoke-Step 'end-to-end smoke test through the gateway' { & $python scripts/smoke_test.py }
}

$webPort = if ($env:WEB_PORT) { $env:WEB_PORT } else { '3000' }
$gatewayPort = if ($env:GATEWAY_PORT) { $env:GATEWAY_PORT } else { '8080' }

Write-Host '==> stack is ready' -ForegroundColor Green
Write-Host "    web      http://localhost:$webPort"
Write-Host "    gateway  http://localhost:$gatewayPort/api/products"
