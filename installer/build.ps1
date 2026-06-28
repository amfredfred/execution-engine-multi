
# powershell -ExecutionPolicy Bypass -File build.ps1

param(
    [switch]$SkipPackage,
    [switch]$SkipInstaller,
    [switch]$Clean
)

Set-StrictMode -Off
$ErrorActionPreference = "Stop"

$InstallerDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$EngineDir    = Split-Path -Parent $InstallerDir

# Keep NumPy/OpenBLAS imports used by PyInstaller analysis within build-machine
# memory limits. These values are inherited by PyInstaller's isolated workers.
$env:OPENBLAS_NUM_THREADS = "1"
$env:OMP_NUM_THREADS      = "1"
$env:MKL_NUM_THREADS      = "1"

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

    $python = Find-Exe @(
        (Join-Path $EngineDir "venv\Scripts\python.exe"),
        (Join-Path $EngineDir ".venv\Scripts\python.exe"),
        "python"
    )

    if (-not $python) {
        Write-Host ""
        Write-Host "  ERROR: Python environment not found." -ForegroundColor Red
        Write-Host "  Run:   venv\Scripts\pip install pyinstaller" -ForegroundColor Red
        exit 1
    }

    Write-Host "      Using : $python -m PyInstaller" -ForegroundColor DarkGray

    Push-Location $EngineDir
    try {
        $pyiArgs = [System.Collections.Generic.List[string]]::new()
        $pyiArgs.Add("engine.spec")
        $pyiArgs.Add("--noconfirm")
        if ($Clean) { $pyiArgs.Add("--clean") }

        & $python -m PyInstaller $pyiArgs
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

    # --- Inject clean default config (never ship developer credentials) ----------
    # PyInstaller no longer includes config.yaml in datas (engine.spec removed it).
    # We copy config.example.yaml into _internal\ so fresh installs get a
    # working template without any real MT5 credentials or activation keys.
    $exampleCfg  = Join-Path $EngineDir "config.example.yaml"
    $internalDir = Join-Path $distDir "_internal"
    $shippedCfg  = Join-Path $internalDir "config.yaml"
    if (Test-Path $exampleCfg) {
        if (-not (Test-Path $internalDir)) { New-Item -ItemType Directory -Path $internalDir -Force | Out-Null }
        Copy-Item $exampleCfg $shippedCfg -Force
        Write-Host "      Config : _internal\config.yaml  (clean copy from config.example.yaml)" -ForegroundColor DarkGray
    } else {
        Write-Host "  WARNING: config.example.yaml not found — no default config in dist." -ForegroundColor Yellow
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
