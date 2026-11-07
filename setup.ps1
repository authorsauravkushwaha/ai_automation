#Requires -Version 5.1
<#
.SYNOPSIS
  Reproducible FRIDAY setup for Windows 11. Internet is needed only for setup downloads.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Run
.EXAMPLE
  .\setup.ps1 -Lite -SkipOllama -SkipSpeechDownload -Run
#>
param(
    [switch]$Lite,
    [switch]$Full,
    [switch]$Run,
    [switch]$SkipOllama,
    [switch]$SkipModelDownload,
    [switch]$SkipSpeechDownload,
    [switch]$Dev
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Invoke-Native {
    param([string]$FilePath, [string[]]$Arguments)
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
    }
}

function Test-Python {
    param([string]$FilePath, [string[]]$Prefix)
    & $FilePath @Prefix -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Test-Ollama {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 2 | Out-Null
        return $true
    }
    catch {
        return $false
    }
}

Write-Host "`nFRIDAY setup — local, offline-first assistant" -ForegroundColor Cyan
Write-Host "Repository: $Root"
Write-Host "Target: Windows 11, CPU inference, low-memory defaults`n"

$PythonExe = $null
$PythonPrefix = @()
if (Get-Command py.exe -ErrorAction SilentlyContinue) {
    if (Test-Python "py.exe" @("-3.11")) {
        $PythonExe = "py.exe"
        $PythonPrefix = @("-3.11")
    }
    elseif (Test-Python "py.exe" @("-3")) {
        $PythonExe = "py.exe"
        $PythonPrefix = @("-3")
    }
}
if (-not $PythonExe -and (Get-Command python.exe -ErrorAction SilentlyContinue)) {
    if (Test-Python "python.exe" @()) {
        $PythonExe = "python.exe"
    }
}
if (-not $PythonExe) {
    throw @"
Python 3.11 or newer is required. Install it, then run this script again:
  winget install -e --id Python.Python.3.11
Download alternative: https://www.python.org/downloads/windows/
"@
}

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "[1/7] Creating isolated Python environment…"
    Invoke-Native $PythonExe ($PythonPrefix + @("-m", "venv", ".venv"))
}
else {
    Write-Host "[1/7] Using existing .venv"
}

Write-Host "[2/7] Installing FRIDAY and selected optional components…"
Invoke-Native $VenvPython @("-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel")
$Extras = @("gui", "speech", "tts", "windows-audio")
if ($Lite) {
    $Extras = @()
}
if ($Full) {
    Write-Warning "Full mode installs PyTorch-based speaker recognition and wake-word dependencies. This is not the recommended 4 GB profile."
    $Extras += @("speaker", "wake")
}
if ($Dev) {
    $Extras += @("dev")
}
if ($Extras.Count -eq 0) {
    Invoke-Native $VenvPython @("-m", "pip", "install", "-e", ".")
}
else {
    $PackageSpec = ".[" + ($Extras -join ",") + "]"
    Invoke-Native $VenvPython @("-m", "pip", "install", "-e", $PackageSpec)
}

if (-not (Test-Path (Join-Path $Root "config.toml"))) {
    Copy-Item (Join-Path $Root "config.example.toml") (Join-Path $Root "config.toml")
    Write-Host "Created config.toml from the reviewed example."
}

Write-Host "[3/7] Running portable tests before model downloads…"
Invoke-Native $VenvPython @("scripts\run_tests.py")

$Model = (& $VenvPython -c "from friday.config import load_config; print(load_config().ollama.model)").Trim()
if (-not $SkipOllama) {
    Write-Host "[4/7] Checking local Ollama runtime…"
    if (-not (Get-Command ollama.exe -ErrorAction SilentlyContinue)) {
        if (Get-Command winget.exe -ErrorAction SilentlyContinue) {
            Write-Host "Installing Ollama through Windows Package Manager…"
            & winget.exe install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -ne 0) {
                throw "Ollama installation failed. Install from https://ollama.com/download/windows and rerun setup."
            }
            $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
        }
        else {
            throw "Ollama is missing and winget is unavailable. Install https://ollama.com/download/windows or rerun with -SkipOllama."
        }
    }
    $Ollama = Get-Command ollama.exe -ErrorAction SilentlyContinue
    if (-not $Ollama) {
        throw "Ollama was installed but ollama.exe is not on PATH. Open a new terminal and rerun setup."
    }
    if (-not (Test-Ollama)) {
        Write-Host "Starting the local Ollama service…"
        Start-Process -FilePath $Ollama.Source -ArgumentList @("serve") -WindowStyle Hidden
        $Ready = $false
        for ($Attempt = 0; $Attempt -lt 30; $Attempt++) {
            Start-Sleep -Milliseconds 500
            if (Test-Ollama) {
                $Ready = $true
                break
            }
        }
        if (-not $Ready) {
            throw "Ollama did not start on loopback port 11434. Check its Windows service/logs."
        }
    }
    if (-not $SkipModelDownload) {
        Write-Host "[5/7] Pulling $Model (internet setup download)…"
        Invoke-Native $Ollama.Source @("pull", $Model)
    }
    else {
        Write-Host "[5/7] Skipping Ollama model download"
    }
}
else {
    Write-Host "[4/7] Ollama setup skipped; structured commands remain available"
    Write-Host "[5/7] Model setup skipped"
}

if (-not $Lite -and -not $SkipSpeechDownload) {
    Write-Host "[6/7] Preparing the tiny INT8 offline Whisper model…"
    Write-Host "The first preparation downloads model weights; subsequent transcription is offline."
    Invoke-Native $VenvPython @("-m", "friday", "setup-speech")
}
else {
    Write-Host "[6/7] Speech model preparation skipped"
}

Write-Host "[7/7] Running a real text-pipeline smoke test…"
Invoke-Native $VenvPython @("-m", "friday", "--no-llm", "ask", "system", "info")

Write-Host "`nFRIDAY setup completed successfully." -ForegroundColor Green
Write-Host "Next recommended step: .\.venv\Scripts\python.exe -m friday setup-pin"
Write-Host "Desktop:  .\friday.cmd gui"
Write-Host "Terminal: .\friday.cmd"
Write-Host "Diagnose: .\friday.cmd doctor"
Write-Host "Normal use does not require internet.`n"

if ($Run) {
    if (-not $Lite) {
        Invoke-Native $VenvPython @("-m", "friday", "gui")
    }
    else {
        Invoke-Native $VenvPython @("-m", "friday")
    }
}
