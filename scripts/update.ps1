# scripts\update.ps1 — Apex Quantel auto-updater
#
# Checks the gateway for a newer engine version, downloads it, verifies the
# SHA-256 checksum, and performs a hot-swap of the dist\ directory.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\update.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -Force
#   powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -CheckOnly
#
# Flags:
#   -Force      Install even if version matches (useful for repair)
#   -CheckOnly  Print available version and exit without updating
#
# Called by the service on a schedule or at startup via a wrapper.
# Safe to run while the service is stopped.
#
# Gateway endpoint (add to execution-gateway app.controller.ts):
#   GET /engine-version
#   Response: { "version": "0.2.0", "download_url": "...", "sha256": "..." }

param(
    [switch]$Force,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$EngineDir  = Split-Path -Parent $ScriptDir  # scripts\ → engine root

$VersionFile  = Join-Path $EngineDir "version.txt"
$DistDir      = Join-Path $EngineDir "apex-quant-trader-agent"   # {app}\apex-quant-trader-agent
$ServiceName  = "apex-quant-trader-agent"
$TempDir      = Join-Path $env:TEMP "apexquantel-update"
$BackupDir    = Join-Path $EngineDir "apex-quant-trader-agent.bak"
$StagingDir   = Join-Path $TempDir "staged"

# ── Read gateway URL from config.yaml ────────────────────────────────────
function Get-GatewayBase {
    $configFile = Join-Path $EngineDir "config.yaml"
    if (-not (Test-Path $configFile)) { return $null }
    $content = Get-Content $configFile -Raw
    $match = [regex]::Match($content, 'ws_url\s*:\s*(\S+)')
    if (-not $match.Success) { return $null }
    $ws = $match.Groups[1].Value.Trim('"').Trim("'")
    # Convert wss://host/path → https://host
    $ws -replace '^wss?://', 'https://' -replace '/engine.*$', ''
}

$GatewayBase = Get-GatewayBase
if (-not $GatewayBase) {
    Write-Warning "Could not determine gateway URL from config.yaml — using default"
    $GatewayBase = "https://gateway.apexquantel.io"
}

$VersionEndpoint = "$GatewayBase/engine-version"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
function Get-LocalVersion {
    if (Test-Path $VersionFile) {
        return (Get-Content $VersionFile -Raw).Trim()
    }
    return "0.0.0"
}

function Compare-SemVer([string]$a, [string]$b) {
    # Returns 1 if $b > $a, 0 if equal, -1 if $a > $b
    $pa = [version]($a -replace '[^0-9.]', '')
    $pb = [version]($b -replace '[^0-9.]', '')
    if ($pb -gt $pa) { return 1 }
    if ($pb -lt $pa) { return -1 }
    return 0
}

function Verify-Sha256([string]$filePath, [string]$expected) {
    $actual = (Get-FileHash -Path $filePath -Algorithm SHA256).Hash.ToLower()
    $exp    = $expected.ToLower().Replace("sha256:", "")
    if ($actual -ne $exp) {
        Write-Error "SHA-256 mismatch for $filePath`n  Expected: $exp`n  Actual  : $actual"
        return $false
    }
    Write-Host "  SHA-256 verified: $actual" -ForegroundColor DarkGray
    return $true
}

function Verify-DetachedSignature([string]$filePath, [string]$signatureB64, [string]$thumbprint) {
    if (-not $signatureB64 -or -not $thumbprint) {
        throw "Update manifest must include signature and signer_thumbprint"
    }
    Add-Type -AssemblyName System.Security.Cryptography.Pkcs
    $content = [System.Security.Cryptography.Pkcs.ContentInfo]::new(
        [System.IO.File]::ReadAllBytes($filePath)
    )
    $cms = [System.Security.Cryptography.Pkcs.SignedCms]::new($content, $true)
    $cms.Decode([Convert]::FromBase64String($signatureB64))
    $cms.CheckSignature($false)
    $actual = $cms.SignerInfos[0].Certificate.Thumbprint.Replace(" ", "").ToUpperInvariant()
    $expected = $thumbprint.Replace(" ", "").ToUpperInvariant()
    if ($actual -ne $expected) {
        throw "Update signer mismatch. Expected $expected, got $actual"
    }
    Write-Host "  Detached signature verified: $actual" -ForegroundColor DarkGray
}

function Stop-ServiceSafe {
    $svc = Get-Service $ServiceName -ErrorAction SilentlyContinue
    if (-not $svc) { return }
    if ($svc.Status -ne "Stopped") {
        Write-Host "  Stopping $ServiceName..."
        Stop-Service $ServiceName -Force -ErrorAction SilentlyContinue
        for ($i = 0; $i -lt 15; $i++) {
            $svc = Get-Service $ServiceName -ErrorAction SilentlyContinue
            if (-not $svc -or $svc.Status -eq "Stopped") { return }
            Start-Sleep 1
        }
        Write-Warning "$ServiceName did not stop within 15 s"
    }
}

function Start-ServiceSafe {
    $svc = Get-Service $ServiceName -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -ne "Running") {
        Write-Host "  Starting $ServiceName..."
        Start-Service $ServiceName -ErrorAction SilentlyContinue
        Start-Sleep 3
        $svc = Get-Service $ServiceName -ErrorAction SilentlyContinue
        $status = if ($svc) { $svc.Status } else { "unknown" }
        Write-Host "  Status: $status"
    }
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
$LocalVersion = Get-LocalVersion
Write-Host ""
Write-Host "=== Apex Quantel Updater ===" -ForegroundColor Cyan
Write-Host "  Local version  : $LocalVersion"
Write-Host "  Version check  : $VersionEndpoint"
Write-Host ""

# ── Fetch remote manifest ─────────────────────────────────────────────────
Write-Host "  Checking for updates..." -ForegroundColor DarkGray
$manifest = $null
try {
    $resp = Invoke-WebRequest -Uri $VersionEndpoint -UseBasicParsing -TimeoutSec 10
    $manifest = $resp.Content | ConvertFrom-Json
} catch {
    Write-Warning "Could not reach version endpoint: $_"
    Write-Host "  No update performed."
    exit 0
}

$RemoteVersion = $manifest.version
$DownloadUrl   = $manifest.download_url
$Sha256        = $manifest.sha256
$Signature     = $manifest.signature
$SignerThumbprint = $manifest.signer_thumbprint

Write-Host "  Remote version : $RemoteVersion"

if ($CheckOnly) {
    $cmp = Compare-SemVer $LocalVersion $RemoteVersion
    if ($cmp -gt 0) {
        Write-Host "  Update available: $LocalVersion → $RemoteVersion" -ForegroundColor Yellow
        Write-Host "  Download: $DownloadUrl"
    } else {
        Write-Host "  Already up to date." -ForegroundColor Green
    }
    exit 0
}

# ── Version comparison ────────────────────────────────────────────────────
$cmp = Compare-SemVer $LocalVersion $RemoteVersion
if ($cmp -le 0 -and -not $Force) {
    Write-Host "  Already up to date (v$LocalVersion). Use -Force to reinstall." -ForegroundColor Green
    exit 0
}

if (-not $DownloadUrl) {
    Write-Warning "Remote manifest does not include a download_url — cannot update."
    exit 1
}

Write-Host ""
Write-Host "  Updating $LocalVersion → $RemoteVersion..." -ForegroundColor Yellow

# ── Download ──────────────────────────────────────────────────────────────
New-Item -ItemType Directory -Force -Path $TempDir | Out-Null
$ZipPath = Join-Path $TempDir "apex-quant-trader-agent-$RemoteVersion.zip"

Write-Host "  Downloading $DownloadUrl..."
Invoke-WebRequest -Uri $DownloadUrl -OutFile $ZipPath -UseBasicParsing

# ── Verify checksum ───────────────────────────────────────────────────────
if (-not $Sha256) { throw "Update manifest does not include mandatory SHA-256 checksum" }
if (-not (Verify-Sha256 $ZipPath $Sha256)) { exit 1 }
Verify-DetachedSignature $ZipPath $Signature $SignerThumbprint

# Extract and validate completely before touching the active installation.
if (Test-Path $StagingDir) { Remove-Item $StagingDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $StagingDir | Out-Null
Expand-Archive $ZipPath -DestinationPath $StagingDir -Force
$StagedDist = Join-Path $StagingDir "apex-quant-trader-agent"
if (-not (Test-Path (Join-Path $StagedDist "apex-quant-trader-agent.exe"))) {
    throw "Signed update does not contain apex-quant-trader-agent.exe"
}

# ── Stop service ──────────────────────────────────────────────────────────
$serviceWasRunning = $false
$svc = Get-Service $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq "Running") {
    $serviceWasRunning = $true
    Stop-ServiceSafe
}

# ── Back up existing dist ──────────────────────────────────────────────────
if (Test-Path $DistDir) {
    Write-Host "  Backing up existing engine to apex-quant-trader-agent.bak..."
    if (Test-Path $BackupDir) { Remove-Item $BackupDir -Recurse -Force }
    Rename-Item $DistDir $BackupDir
}

# ── Atomic activation with rollback ──────────────────────────────────────
try {
    Move-Item $StagedDist $DistDir
    if (-not (Test-Path (Join-Path $DistDir "apex-quant-trader-agent.exe"))) {
        throw "Activated update is incomplete"
    }
} catch {
    if (Test-Path $DistDir) { Remove-Item $DistDir -Recurse -Force }
    if (Test-Path $BackupDir) { Rename-Item $BackupDir $DistDir }
    if ($serviceWasRunning) { Start-ServiceSafe }
    throw
}

# ── Update version.txt ────────────────────────────────────────────────────
Set-Content -Path $VersionFile -Value $RemoteVersion -Encoding utf8

# ── Remove backup ─────────────────────────────────────────────────────────
if (Test-Path $BackupDir) { Remove-Item $BackupDir -Recurse -Force -ErrorAction SilentlyContinue }

# ── Restart service ───────────────────────────────────────────────────────
if ($serviceWasRunning) { Start-ServiceSafe }

# ── Cleanup ───────────────────────────────────────────────────────────────
Remove-Item $TempDir -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "  Update complete: v$LocalVersion → v$RemoteVersion" -ForegroundColor Green
Write-Host ""
