# install_manager.ps1
#
# Registers the AQ Manager as a Windows Task Scheduler task so it starts
# automatically at logon and orchestrates all managed MT5 agent accounts.
#
# WHY TASK SCHEDULER (not a service):
#   The Manager spawns agent sub-processes that attach to MT5 terminals.
#   MT5 terminals run in the user's interactive session; Task Scheduler
#   at LogOn runs in the same session, so agents can reach their terminals.
#   A Windows Service runs in Session 0 and cannot attach to a user-session MT5.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File install_manager.ps1
#   powershell -ExecutionPolicy Bypass -File install_manager.ps1 -Action uninstall
#   powershell -ExecutionPolicy Bypass -File install_manager.ps1 -Action update

param(
    [ValidateSet("install", "uninstall", "update")]
    [string]$Action = "install"
)

$ErrorActionPreference = "Stop"

# Task creation/removal requires an Administrator token. Inno Setup already
# supplies one, but manual updates should elevate themselves instead of
# partially succeeding.
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) {
    $scriptPath = $MyInvocation.MyCommand.Path
    $args = "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`" -Action $Action"
    $process = Start-Process powershell.exe -Verb RunAs -ArgumentList $args -Wait -PassThru
    exit $process.ExitCode
}

$TaskName       = "AQ Manager"
$TaskFolder  = "\Apex Quantel\"
$Description = "Apex Quantel Manager - orchestrates multi-agent MT5 trade execution"
$EngineDir   = Split-Path -Parent $MyInvocation.MyCommand.Path

# Resolve exe - Inno Setup installs directly under {app}\apex-quant-trader-agent\
$InstalledExe = Join-Path $EngineDir "apex-quant-trader-agent\apex-quant-trader-agent.exe"
$PackagedExe  = Join-Path $EngineDir "dist\apex-quant-trader-agent\apex-quant-trader-agent.exe"

$AppExe = $null
if     (Test-Path -LiteralPath $InstalledExe) { $AppExe = $InstalledExe }
elseif (Test-Path -LiteralPath $PackagedExe)  { $AppExe = $PackagedExe  }

# Helpers

function Stop-NamedTask ($name) {
    $t = Get-ScheduledTask -TaskName $name -TaskPath $TaskFolder -ErrorAction SilentlyContinue
    if ($t) {
        if ($t.State -eq "Running") {
            Write-Host "  Stopping task '$name'..."
            Stop-ScheduledTask -TaskName $name -TaskPath $TaskFolder -ErrorAction SilentlyContinue
            Start-Sleep 3
        }
        Write-Host "  Removing task '$name'..."
        Unregister-ScheduledTask -TaskName $name -TaskPath $TaskFolder -Confirm:$false -ErrorAction SilentlyContinue
    }
}

function Stop-ManagerTask {
    $t = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskFolder -ErrorAction SilentlyContinue
    if ($t -and $t.State -eq "Running") {
        Write-Host "  Stopping running Manager task..."
        Stop-ScheduledTask -TaskName $TaskName -TaskPath $TaskFolder -ErrorAction SilentlyContinue
        Start-Sleep 3
    }
}

function Remove-ManagerTask {
    Stop-ManagerTask
    if (Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskFolder -ErrorAction SilentlyContinue) {
        Write-Host "  Removing existing Manager task..."
        Unregister-ScheduledTask -TaskName $TaskName -TaskPath $TaskFolder -Confirm:$false
    }
}

function Kill-ManagerOrphans {
    # 1) Kill by command-line match — catches manager AND agent workers
    #    (both use the same exe; workers have --agent, manager has --manager)
    $escapedDir = [regex]::Escape($EngineDir)
    Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $PID -and
        $_.CommandLine -and
        $_.CommandLine -match $escapedDir -and
        ($_.CommandLine -match "--manager" -or $_.CommandLine -match "--agent") -and
        ($_.Name -like "apex-quant*" -or $_.Name -like "python*")
    } | ForEach-Object {
        Write-Host "  Stopping orphan PID $($_.ProcessId): $($_.Name)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

    # 2) Kill by port - any process still holding the manager API/channel ports
    foreach ($port in @(8870, 8871)) {
        $pids = (Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue).OwningProcess |
                Sort-Object -Unique
        foreach ($p in $pids) {
            if ($p -le 0 -or $p -eq $PID) { continue }
            $proc = Get-Process -Id $p -ErrorAction SilentlyContinue
            if ($proc) {
                Write-Host "  Killing port-$port holder PID $p ($($proc.Name))"
                Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
            }
        }
    }

    Start-Sleep 1   # give OS time to release the port
}

function Ensure-DataDirs {
    # Create ProgramData directories for the manager and agent data
    $base = Join-Path $env:PROGRAMDATA "Apex Quantel\manager"
    foreach ($d in @(".", "logs", "agents")) {
        $p = Join-Path $base $d
        if (-not (Test-Path $p)) {
            New-Item -ItemType Directory -Path $p -Force | Out-Null
            Write-Host "  Created: $p" -ForegroundColor DarkGray
        }
    }
    # Grant the current user modify rights (so the process can write registry.db etc.)
    $acl = Get-Acl $base
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        "$env:USERDOMAIN\$env:USERNAME",
        "Modify",
        "ContainerInherit,ObjectInherit",
        "None",
        "Allow"
    )
    $acl.SetAccessRule($rule)
    Set-Acl -Path $base -AclObject $acl -ErrorAction SilentlyContinue
    Set-Acl -Path (Join-Path $base "agents") -AclObject $acl -ErrorAction SilentlyContinue
}

function Validate-Exe {
    if (-not $AppExe) {
        Write-Host ""
        Write-Host "ERROR: No executable found." -ForegroundColor Red
        Write-Host "  Expected (installed): $InstalledExe" -ForegroundColor Red
        Write-Host "  Expected (dev build): $PackagedExe"  -ForegroundColor Red
        Write-Host ""
        Write-Host "  Run the build first:"  -ForegroundColor Yellow
        Write-Host "    powershell -ExecutionPolicy Bypass -File installer\build.ps1" -ForegroundColor Yellow
        exit 1
    }
}

# Install

function _install {
    Validate-Exe
    Remove-ManagerTask
    Kill-ManagerOrphans
    Ensure-DataDirs

    Write-Host ""
    Write-Host "  Registering AQ Manager scheduled task..."
    Write-Host "    Task  : $TaskFolder$TaskName"
    Write-Host "    Exe   : $AppExe"
    Write-Host "    Args  : --manager"
    Write-Host "    User  : $env:USERDOMAIN\$env:USERNAME"
    Write-Host "    Delay : 20 s after logon"

    $action = New-ScheduledTaskAction `
        -Execute          $AppExe `
        -Argument         "--manager" `
        -WorkingDirectory $EngineDir

    # Run at logon for this user. The Manager is the sole Gateway owner.
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $trigger.Delay = "PT20S"

    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances      IgnoreNew `
        -ExecutionTimeLimit     ([TimeSpan]::Zero) `
        -RestartCount           5 `
        -RestartInterval        (New-TimeSpan -Minutes 2) `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable

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
    Write-Host "  Starting Manager now..."
    Start-ScheduledTask -TaskName $TaskName -TaskPath $TaskFolder
    Start-Sleep 3

    $state = (Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskFolder).State
    $col   = if ($state -eq "Running") { "Green" } else { "Yellow" }
    Write-Host "  Task state: $state" -ForegroundColor $col

    Write-Host ""
    Write-Host "  AQ Manager will start automatically 20 s after each login."
    Write-Host "  Data  : $env:PROGRAMDATA\Apex Quantel\manager\"
    Write-Host "  Logs  : $env:PROGRAMDATA\Apex Quantel\manager\logs\manager.log"
    Write-Host ""
    Write-Host "  Open the AQ Agent GUI to add and manage MT5 accounts." -ForegroundColor Cyan
}

function _update {
    Validate-Exe
    _install
}

# Entry point

switch ($Action) {
    "uninstall" {
        Remove-ManagerTask
        Kill-ManagerOrphans
        Write-Host "AQ Manager uninstalled."
    }
    "update"  { _update }
    default   { _install }
}
