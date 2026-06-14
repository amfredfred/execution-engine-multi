param(
    [switch]$SkipPackage,
    [switch]$SkipInstaller,
    [switch]$Clean
)

Set-StrictMode -Off
$ErrorActionPreference = "Stop"

$InstallerDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$EngineDir    = Split-Path -Parent $InstallerDir

Write-Host ""
Write-Host "========================================"  -ForegroundColor Cyan
Write-Host "  AQ Agent - Build Pipeline"         -ForegroundColor Cyan
Write-Host "========================================"  -ForegroundColor Cyan
Write-Host "  Engine dir : $EngineDir"
Write-Host ""

# ---------------------------------------------------------------------------
# Resolve version
# ---------------------------------------------------------------------------
$VersionFile = Join-Path $EngineDir "version.txt"
if (-not (Test-Path $VersionFile)) {
    $ver = "0.1.0"
    $pj  = Join-Path $EngineDir "pyproject.toml"
    if (Test-Path $pj) {
        $m = Select-String -Path $pj -Pattern '^version\s*=\s*"(.+)"' | Select-Object -First 1
        if ($m) { $ver = $m.Matches[0].Groups[1].Value }
    }
    Set-Content -Path $VersionFile -Value $ver -Encoding utf8
    Write-Host "  Created version.txt : $ver" -ForegroundColor DarkGray
}
$EngineVersion = (Get-Content $VersionFile -Raw).Trim()
Write-Host "  Version     : $EngineVersion"
Write-Host ""

# ---------------------------------------------------------------------------
# Helper - find an executable from a list of candidate paths / names
# ---------------------------------------------------------------------------
function Find-Exe {
    param([string[]]$Candidates)
    foreach ($c in $Candidates) {
        if ($c -match '[/\\]') {
            if (Test-Path $c) { return $c }
        } else {
            if (Get-Command $c -ErrorAction SilentlyContinue) { return $c }
        }
    }
    return $null
}

# ---------------------------------------------------------------------------
# Step 1 - PyInstaller
# ---------------------------------------------------------------------------
if ($SkipPackage) {
    Write-Host "[1/2] Skipping PyInstaller (-SkipPackage)" -ForegroundColor DarkGray
} else {
    Write-Host "[1/2] PyInstaller packaging..." -ForegroundColor Yellow

    $pyi = Find-Exe @(
        (Join-Path $EngineDir "venv\Scripts\pyinstaller.exe"),
        (Join-Path $EngineDir ".venv\Scripts\pyinstaller.exe"),
        "pyinstaller"
    )

    if (-not $pyi) {
        Write-Host ""
        Write-Host "  ERROR: pyinstaller not found." -ForegroundColor Red
        Write-Host "  Run:   venv\Scripts\pip install pyinstaller" -ForegroundColor Red
        exit 1
    }

    Write-Host "      Using : $pyi" -ForegroundColor DarkGray

    Push-Location $EngineDir
    try {
        $pyiArgs = [System.Collections.Generic.List[string]]::new()
        $pyiArgs.Add("engine.spec")
        $pyiArgs.Add("--noconfirm")
        if ($Clean) { $pyiArgs.Add("--clean") }

        & $pyi $pyiArgs
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  ERROR: PyInstaller exited with code $LASTEXITCODE" -ForegroundColor Red
            exit 1
        }
    } finally {
        Pop-Location
    }

    $distDir = Join-Path $EngineDir "dist\apex-quant-trader-agent"
    if (Test-Path $distDir) {
        Write-Host "      Done  : dist\apex-quant-trader-agent\" -ForegroundColor Green
    } else {
        Write-Host "  ERROR: dist\apex-quant-trader-agent\ was not created." -ForegroundColor Red
        exit 1
    }
}

Write-Host ""

# ---------------------------------------------------------------------------
# Step 2 - Inno Setup
# ---------------------------------------------------------------------------
if ($SkipInstaller) {
    Write-Host "[2/2] Skipping Inno Setup (-SkipInstaller)" -ForegroundColor DarkGray
} else {
    Write-Host "[2/2] Inno Setup installer..." -ForegroundColor Yellow

    $iscc = Find-Exe @(
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe",
        "C:\Program Files (x86)\Inno Setup 7\ISCC.exe",
        "C:\Program Files\Inno Setup 7\ISCC.exe",
        "C:\Program Files (x86)\Inno Setup 5\ISCC.exe",
        "iscc"
    )

    if (-not $iscc) {
        Write-Host ""
        Write-Host "  WARNING: Inno Setup not found - skipping installer build." -ForegroundColor Yellow
        Write-Host "  Install : https://jrsoftware.org/isdl.php"                -ForegroundColor Yellow
        Write-Host "  The packaged engine in dist\apex-quant-trader-agent\ is usable for testing." -ForegroundColor DarkGray
    } else {
        Write-Host "      Using : $iscc" -ForegroundColor DarkGray

        $issFile = Join-Path $EngineDir "installer\ApexQuantel.iss"
        & $iscc "/DMyAppVersion=$EngineVersion" $issFile
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  ERROR: ISCC exited with code $LASTEXITCODE" -ForegroundColor Red
            exit 1
        }

        $outExe = Join-Path $EngineDir "installer\Output\AQAgentSetup.exe"
        if (Test-Path $outExe) {
            $bytes  = (Get-Item $outExe).Length
            $sizeMB = [math]::Round($bytes / 1048576, 1)
            Write-Host "      Done  : installer\Output\AQAgentSetup.exe - $sizeMB MB" -ForegroundColor Green
        } else {
            Write-Host "      Done  : installer\Output\AQAgentSetup.exe" -ForegroundColor Green
        }
    }
}

Write-Host ""
Write-Host "=============================="        -ForegroundColor Cyan
Write-Host "  AQ Agent v$EngineVersion — built"  -ForegroundColor Cyan
Write-Host "=============================="        -ForegroundColor Cyan
Write-Host ""
