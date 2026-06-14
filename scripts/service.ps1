<#
.SYNOPSIS
    Manage the Apex Quantel Windows service.

.PARAMETER Command
    status | logs | logs-err | restart | start | stop | edit | remove | help

.EXAMPLE
    .\service.ps1 status
    .\service.ps1 logs
    .\service.ps1 restart
#>

param([string]$Command = "status")

$ServiceName = "apex-quant-trader-agent"
$NssmExe    = Join-Path $PSScriptRoot "..\nssm\nssm-2.24\win64\nssm.exe"
if (-not (Test-Path $NssmExe)) { $NssmExe = "nssm" }
$LogDir     = Join-Path $PSScriptRoot "..\logs"

switch ($Command.ToLower()) {

    "status" {
        $status = & $NssmExe status $ServiceName 2>&1
        Write-Host "`n=== Apex Quantel Service ===" -ForegroundColor Cyan
        Write-Host "Status : $status"
        if (Test-Path $LogDir) {
            foreach ($f in "stdout.log","stderr.log") {
                $p = Join-Path $LogDir $f
                if (Test-Path $p) {
                    $mb = [math]::Round((Get-Item $p).Length / 1MB, 2)
                    Write-Host "$f : $mb MB"
                }
            }
        }
        Write-Host ""
    }

    "logs" {
        $f = Join-Path $LogDir "stdout.log"
        if (-not (Test-Path $f)) { Write-Error "Not found: $f"; exit 1 }
        Write-Host "Tailing $f  (Ctrl+C to stop)`n"
        Get-Content $f -Tail 50 -Wait
    }

    "logs-err" {
        $f = Join-Path $LogDir "stderr.log"
        if (-not (Test-Path $f)) { Write-Error "Not found: $f"; exit 1 }
        Get-Content $f -Tail 100
    }

    "restart" {
        & $NssmExe restart $ServiceName
        Start-Sleep 2
        Write-Host (& $NssmExe status $ServiceName 2>&1)
    }

    "start" {
        & $NssmExe start $ServiceName
        Start-Sleep 2
        Write-Host (& $NssmExe status $ServiceName 2>&1)
    }

    "stop" {
        & $NssmExe stop $ServiceName confirm
        Start-Sleep 2
        Write-Host (& $NssmExe status $ServiceName 2>&1)
    }

    "edit" { & $NssmExe edit $ServiceName }

    "remove" {
        & $NssmExe remove $ServiceName confirm
        Write-Host "Removed. Reinstall: powershell -File install_service.ps1"
    }

    default {
        Write-Host "Usage: service.ps1 status|logs|logs-err|restart|start|stop|edit|remove"
    }
}
