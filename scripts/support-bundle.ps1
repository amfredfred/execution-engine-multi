# scripts\support-bundle.ps1 — Apex Quantel diagnostic bundle collector
#
# Collects logs, redacted config, service status, and system info into a
# zip archive that can be sent to support without exposing secrets.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\support-bundle.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\support-bundle.ps1 -OutDir C:\Temp
#
# Output: Apex Quantel-Support-<date>.zip (in -OutDir, default: Desktop)

param(
    [string]$OutDir = [Environment]::GetFolderPath("Desktop"),
    [int]$LogLines  = 5000
)

$ErrorActionPreference = "Continue"

$ScriptDir    = Split-Path -Parent $MyInvocation.MyCommand.Path
$EngineDir    = Split-Path -Parent $ScriptDir
$ServiceName  = "apex-quant-trader-agent"
$Timestamp    = Get-Date -Format "yyyyMMdd-HHmmss"
$BundleName   = "Apex Quantel-Support-$Timestamp"
$StagingDir   = Join-Path $env:TEMP $BundleName
$ZipOut       = Join-Path $OutDir "$BundleName.zip"

# Secrets to redact from config.yaml (YAML key: value format)
$SecretKeys = @(
    "password",
    "activation_key",
    "signal_hmac_secret",
    "credential_hash",
    "activation_key_hash",
    # Legacy .env keys (backward compat)
    "MT5_PASSWORD",
    "APEX_ACTIVATION_KEY",
    "SIGNAL_HMAC_SECRET"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
function Write-Section([string]$title) {
    Write-Host ""
    Write-Host "  [$title]" -ForegroundColor Cyan
}

function Redact-Secrets([string]$content) {
    foreach ($key in $SecretKeys) {
        $esc  = [regex]::Escape($key)
        # .env style: KEY=value
        $content = [regex]::Replace($content, "(?mi)^($esc\s*=\s*).*$", '${1}[REDACTED]')
        # YAML style: key: value
        $content = [regex]::Replace($content, "(?mi)^(\s*$esc\s*:\s*).*$", '${1}[REDACTED]')
    }
    return $content
}

function Copy-LastNLines([string]$src, [string]$dst, [int]$n) {
    if (-not (Test-Path $src)) {
        Set-Content $dst "(file not found: $src)" -Encoding utf8
        return
    }
    $lines = Get-Content $src -Tail $n -ErrorAction SilentlyContinue
    if ($lines) {
        $lines | Set-Content $dst -Encoding utf8
    } else {
        Set-Content $dst "(empty)" -Encoding utf8
    }
}

function Run-Command([string]$cmd, [string[]]$args, [string]$outFile) {
    try {
        $out = & $cmd @args 2>&1
        $out | Set-Content $outFile -Encoding utf8
    } catch {
        "Command failed: $cmd $($args -join ' ')`n$_" | Set-Content $outFile -Encoding utf8
    }
}

# ---------------------------------------------------------------------------
# Setup staging dir
# ---------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $StagingDir | Out-Null
New-Item -ItemType Directory -Force -Path $OutDir      | Out-Null

Write-Host ""
Write-Host "=====================================" -ForegroundColor Cyan
Write-Host "  Apex Quantel — Support Bundle"        -ForegroundColor Cyan
Write-Host "=====================================" -ForegroundColor Cyan
Write-Host "  Engine dir : $EngineDir"
Write-Host "  Staging    : $StagingDir"
Write-Host "  Output     : $ZipOut"

# ---------------------------------------------------------------------------
# 1. Logs — last N lines of stdout.log and stderr.log
# ---------------------------------------------------------------------------
Write-Section "Logs (last $LogLines lines)"

$LogsDir = Join-Path $EngineDir "logs"
Copy-LastNLines (Join-Path $LogsDir "stdout.log") (Join-Path $StagingDir "stdout.log") $LogLines
Copy-LastNLines (Join-Path $LogsDir "stderr.log") (Join-Path $StagingDir "stderr.log") $LogLines
Write-Host "  stdout.log, stderr.log copied"

# ---------------------------------------------------------------------------
# 2. Config — config.yaml with secrets redacted
# ---------------------------------------------------------------------------
Write-Section "Config (redacted)"

$configFile = Join-Path $EngineDir "config.yaml"
if (Test-Path $configFile) {
    $raw     = Get-Content $configFile -Raw
    $cleaned = Redact-Secrets $raw
    Set-Content (Join-Path $StagingDir "config.yaml") $cleaned -Encoding utf8
    Write-Host "  config.yaml (redacted)"
} else {
    "config.yaml not found" | Set-Content (Join-Path $StagingDir "config.yaml") -Encoding utf8
    Write-Host "  config.yaml not found"
}

# ---------------------------------------------------------------------------
# 3. .env — existence + keys only (values fully redacted)
# ---------------------------------------------------------------------------
Write-Section ".env (keys only)"

$envFile = Join-Path $EngineDir ".env"
if (Test-Path $envFile) {
    $envLines = Get-Content $envFile -ErrorAction SilentlyContinue
    $envSafe  = $envLines | ForEach-Object {
        if ($_ -match "^([^=]+)=(.+)$") { "$($Matches[1])=[REDACTED]" } else { $_ }
    }
    $envSafe | Set-Content (Join-Path $StagingDir "env-keys.txt") -Encoding utf8
    Write-Host "  .env keys written (values redacted)"
} else {
    ".env not found" | Set-Content (Join-Path $StagingDir "env-keys.txt") -Encoding utf8
    Write-Host "  .env not found"
}

# ---------------------------------------------------------------------------
# 4. Service status
# ---------------------------------------------------------------------------
Write-Section "Service status"

$svcOut = Join-Path $StagingDir "service-status.txt"
$svc    = Get-Service $ServiceName -ErrorAction SilentlyContinue

if ($svc) {
    $svc | Format-List * | Out-String | Set-Content $svcOut -Encoding utf8
    Write-Host "  Service: $ServiceName — $($svc.Status)"

    # NSSM status if available
    $NssmExe = Join-Path $EngineDir "nssm\nssm-2.24\win64\nssm.exe"
    if (Test-Path $NssmExe) {
        $nssmOut = & $NssmExe status $ServiceName 2>&1
        "`nNSSM status:`n$nssmOut" | Add-Content $svcOut -Encoding utf8
        Write-Host "  NSSM status appended"
    }
} else {
    "Service '$ServiceName' not found (not installed)" | Set-Content $svcOut -Encoding utf8
    Write-Host "  Service $ServiceName not installed"
}

# ---------------------------------------------------------------------------
# 5. System info
# ---------------------------------------------------------------------------
Write-Section "System info"

$sysFile = Join-Path $StagingDir "system-info.txt"

@"
=== System Info — $Timestamp ===

OS:
$($(Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version, OSArchitecture, BuildNumber | Format-List | Out-String).Trim())

CPU:
$($(Get-CimInstance Win32_Processor | Select-Object Name, NumberOfCores, NumberOfLogicalProcessors | Format-List | Out-String).Trim())

RAM:
$($(Get-CimInstance Win32_OperatingSystem | Select-Object @{N='TotalRAM_GB';E={[math]::Round($_.TotalVisibleMemorySize/1MB,1)}}, @{N='FreeRAM_GB';E={[math]::Round($_.FreePhysicalMemory/1MB,1)}} | Format-List | Out-String).Trim())

Disk (C:):
$($(Get-PSDrive C | Select-Object Used, Free | Format-List | Out-String).Trim())

PowerShell version: $($PSVersionTable.PSVersion)
"@ | Set-Content $sysFile -Encoding utf8

Write-Host "  OS, CPU, RAM, disk written"

# ---------------------------------------------------------------------------
# 6. Engine version
# ---------------------------------------------------------------------------
Write-Section "Engine version"

$verFile = Join-Path $EngineDir "version.txt"
if (-not (Test-Path $verFile)) {
    $verFile = Join-Path $EngineDir "apex-quant-trader-agent\version.txt"
}
if (Test-Path $verFile) {
    $ver = (Get-Content $verFile -Raw).Trim()
    Write-Host "  Engine version: $ver"
    "engine_version=$ver" | Set-Content (Join-Path $StagingDir "version.txt") -Encoding utf8
} else {
    "version.txt not found" | Set-Content (Join-Path $StagingDir "version.txt") -Encoding utf8
    Write-Host "  version.txt not found"
}

# ---------------------------------------------------------------------------
# 7. Recent pending outbox events (last 50 rows)
# ---------------------------------------------------------------------------
Write-Section "Pending outbox events"

$dbFile     = Join-Path $EngineDir "data\engine.db"
$outboxFile = Join-Path $StagingDir "outbox-pending.txt"

if (Test-Path $dbFile) {
    try {
        # Use sqlite3.exe if available, otherwise note it
        $sqlite = Get-Command "sqlite3" -ErrorAction SilentlyContinue
        if ($sqlite) {
            $query = "SELECT id, event, substr(payload_json,1,120), sent, created_at FROM event_outbox WHERE sent=0 ORDER BY id DESC LIMIT 50;"
            $out   = & sqlite3 $dbFile $query 2>&1
            $out | Set-Content $outboxFile -Encoding utf8
            Write-Host "  Outbox query complete (sqlite3 cli)"
        } else {
            "sqlite3 CLI not found — skipping outbox query.`nDB path: $dbFile" |
                Set-Content $outboxFile -Encoding utf8
            Write-Host "  sqlite3 not on PATH — outbox skipped"
        }
    } catch {
        "Error querying outbox: $_" | Set-Content $outboxFile -Encoding utf8
        Write-Host "  Outbox query failed: $_"
    }
} else {
    "engine.db not found at $dbFile" | Set-Content $outboxFile -Encoding utf8
    Write-Host "  engine.db not found"
}

# ---------------------------------------------------------------------------
# 8. Bundle manifest
# ---------------------------------------------------------------------------
$manifest = Join-Path $StagingDir "MANIFEST.txt"
@"
Apex Quantel Support Bundle
Generated : $Timestamp
Machine   : $env:COMPUTERNAME
User      : $env:USERNAME

Files in this bundle:
  stdout.log        — last $LogLines lines of stdout
  stderr.log        — last $LogLines lines of stderr
  config.yaml       — config with secrets redacted
  env-keys.txt      — .env key names (values fully redacted)
  service-status.txt — Windows service status
  system-info.txt   — OS/CPU/RAM/disk info
  version.txt       — engine version
  outbox-pending.txt — pending outbox events (if sqlite3 available)
  MANIFEST.txt      — this file
"@ | Set-Content $manifest -Encoding utf8

# ---------------------------------------------------------------------------
# Zip it
# ---------------------------------------------------------------------------
Write-Section "Zipping"
if (Test-Path $ZipOut) { Remove-Item $ZipOut -Force }
Compress-Archive -Path "$StagingDir\*" -DestinationPath $ZipOut -CompressionLevel Optimal

$sizeMB = [math]::Round((Get-Item $ZipOut).Length / 1KB, 0)
Write-Host "  Created: $ZipOut ($sizeMB KB)" -ForegroundColor Green

# Cleanup staging
Remove-Item $StagingDir -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "=====================================" -ForegroundColor Cyan
Write-Host "  Bundle ready: $ZipOut"              -ForegroundColor Green
Write-Host "=====================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Send this file to support@apexquantel.io"
Write-Host ""
