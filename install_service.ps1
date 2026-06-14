# install_service.ps1
#
# Registers AQ Agent as a Windows Task Scheduler task that runs under your
# user account at logon. This replaces the old NSSM service approach.
#
# WHY TASK SCHEDULER INSTEAD OF A SERVICE:
#   Windows services run in Session 0 (no desktop). The MT5 Python API
#   cannot attach to a terminal running in the user's session from Session 0,
#   so the service would launch an invisible MT5 that can never log in.
#   A scheduled task runs as the logged-in user in their own session —
#   MT5 is fully visible and the agent connects normally.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File install_service.ps1
#   powershell -ExecutionPolicy Bypass -File install_service.ps1 uninstall
#   powershell -ExecutionPolicy Bypass -File install_service.ps1 update
#   powershell -ExecutionPolicy Bypass -File install_service.ps1 -VenvName .venv

param(
    [ValidateSet("install", "uninstall", "update")]
    [string]$Action = "install",

    [string]$VenvName = "venv"
)

$ErrorActionPreference = "Stop"

$TaskName    = "AQ Agent"
$TaskFolder  = "\Apex Quantel\"
$Description = "Apex Quantel AQ Agent - automated trade execution engine for MetaTrader 5"
$EngineDir   = Split-Path -Parent $MyInvocation.MyCommand.Path

# ── Resolve executable ────────────────────────────────────────────────────────
# Installed via Inno Setup: files land directly under the install dir (no dist\ prefix)
$InstalledExe = Join-Path $EngineDir "apex-quant-trader-agent\apex-quant-trader-agent.exe"
# Dev / manual build: PyInstaller output is under dist\
$PackagedExe  = Join-Path $EngineDir "dist\apex-quant-trader-agent\apex-quant-trader-agent.exe"
$VenvExe      = Join-Path $EngineDir "$VenvName\Scripts\execution-engine.exe"

$AppExe = $null
if (Test-Path -LiteralPath $InstalledExe) {
    $AppExe = $InstalledExe
    Write-Host "  Mode: installed (Program Files)" -ForegroundColor DarkGray
} elseif (Test-Path -LiteralPath $PackagedExe) {
    $AppExe = $PackagedExe
    Write-Host "  Mode: packaged build (dev)" -ForegroundColor DarkGray
} elseif (Test-Path -LiteralPath $VenvExe) {
    $AppExe = $VenvExe
    Write-Host "  Mode: venv install (dev)" -ForegroundColor DarkYellow
}

# ── Helpers ───────────────────────────────────────────────────────────────────
function Stop-Task {
    $t = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskFolder -ErrorAction SilentlyContinue
    if ($t -and $t.State -eq "Running") {
        Write-Host "  Stopping running task..."
        Stop-ScheduledTask -TaskName $TaskName -TaskPath $TaskFolder -ErrorAction SilentlyContinue
        Start-Sleep 3
    }
}

function Remove-Task {
    Stop-Task
    if (Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskFolder -ErrorAction SilentlyContinue) {
        Write-Host "  Removing existing task..."
        Unregister-ScheduledTask -TaskName $TaskName -TaskPath $TaskFolder -Confirm:$false
    }
}

function Remove-OldNssmService {
    # Migrate: remove the old NSSM service if it is still installed
    $OldService = "apex-quant-trader-agent"
    $NssmExe    = Join-Path $EngineDir "nssm\nssm-2.24\win64\nssm.exe"
    if (Get-Service $OldService -ErrorAction SilentlyContinue) {
        Write-Host "  Removing legacy NSSM service ($OldService)..." -ForegroundColor DarkYellow
        if (Test-Path -LiteralPath $NssmExe) {
            try { & $NssmExe stop   $OldService confirm 2>$null | Out-Null } catch {}
            try { & $NssmExe remove $OldService confirm 2>$null | Out-Null } catch {}
        }
        try { sc.exe delete $OldService 2>$null | Out-Null } catch {}
        Start-Sleep 2
    }
}

function Cleanup-Orphans {
    $escapedDir = [regex]::Escape($EngineDir)
    Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $PID -and
        $_.CommandLine -and
        $_.CommandLine -match $escapedDir -and
        ($_.Name -like "apex-quant*" -or $_.Name -like "execution-engine*" -or $_.Name -like "python*")
    } | ForEach-Object {
        Write-Host "  Stopping orphan PID $($_.ProcessId): $($_.Name)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Validate-Exe {
    if (-not $AppExe) {
        Write-Host ""
        Write-Host "ERROR: No executable found." -ForegroundColor Red
        Write-Host "  Expected (packaged): $PackagedExe" -ForegroundColor Red
        Write-Host "  Expected (dev venv): $VenvExe" -ForegroundColor Red
        Write-Host ""
        Write-Host "  Build the packaged exe:" -ForegroundColor Yellow
        Write-Host "    powershell -ExecutionPolicy Bypass -File installer\build.ps1" -ForegroundColor Yellow
        Write-Host "  Or install the dev venv:" -ForegroundColor Yellow
        Write-Host "    $VenvName\Scripts\pip install -e ." -ForegroundColor Yellow
        exit 1
    }
}

# ── Install ───────────────────────────────────────────────────────────────────
function _install {
    Validate-Exe
    Remove-OldNssmService
    Remove-Task
    Cleanup-Orphans

    Write-Host ""
    Write-Host "  Registering scheduled task..."
    Write-Host "    Task : $TaskFolder$TaskName"
    Write-Host "    Exe  : $AppExe"
    Write-Host "    CWD  : $EngineDir"
    Write-Host "    User : $env:USERDOMAIN\$env:USERNAME"

    # Action: run the agent headlessly from the engine directory
    $action = New-ScheduledTaskAction `
        -Execute         $AppExe `
        -Argument        "--headless" `
        -WorkingDirectory $EngineDir

    # Trigger: at logon for this user, 30-second delay so MT5 can start first
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $trigger.Delay = "PT30S"

    # Settings: no execution time limit, restart up to 10x on failure
    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances      IgnoreNew `
        -ExecutionTimeLimit     ([TimeSpan]::Zero) `
        -RestartCount           10 `
        -RestartInterval        (New-TimeSpan -Minutes 1) `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable

    # Principal: the logged-in user, highest available privilege
    $principal = New-ScheduledTaskPrincipal `
        -UserId    "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive `
        -RunLevel  Highest

    Register-ScheduledTask `
        -TaskName    $TaskName `
        -TaskPath    $TaskFolder `
        -Action      $action `
        -Trigger     $trigger `
        -Settings    $settings `
        -Principal   $principal `
        -Description $Description `
        -Force | Out-Null

    Write-Host ""
    Write-Host "  Starting task now..."
    Start-ScheduledTask -TaskName $TaskName -TaskPath $TaskFolder

    Start-Sleep 3
    $state = (Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskFolder).State
    Write-Host "  Task state: $state" -ForegroundColor $(if ($state -eq "Running") { "Green" } else { "Yellow" })

    Write-Host ""
    Write-Host "  AQ Agent will start automatically 30 s after each login."
    Write-Host "  Logs: $EngineDir\dist\logs\"
}

# ── Update ────────────────────────────────────────────────────────────────────
function _update {
    Validate-Exe
    # Full re-register so the exe path is always current
    _install
}

# ── Entry point ───────────────────────────────────────────────────────────────
switch ($Action) {
    "uninstall" {
        Remove-OldNssmService
        Remove-Task
        Cleanup-Orphans
        Write-Host "Uninstall complete."
    }
    "update"  { _update }
    default   { _install }
}
