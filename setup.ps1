#Requires -Version 5.1
<#
.SYNOPSIS
  Reproducible HINATA setup for Windows 11. Internet is needed only for setup downloads.
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
    [switch]$Dev,
    [switch]$CheckSyntax
)

# This early exit lets the real setup.cmd launcher verify parsing without
# installing dependencies, downloading models, or starting the application.
if ($CheckSyntax) {
    Write-Host "Windows setup script syntax OK."
    return
}

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

function Find-Ollama {
    $command = Get-Command ollama.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    # The installer registers these paths but only updates PATH for new terminals.
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"),
        (Join-Path $env:ProgramFiles "Ollama\ollama.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Ollama\ollama.exe")
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) { return $candidate }
    }
    return $null
}

# Make the local runtime answer on loopback so HINATA's first chat request works.
# Best effort: structured commands remain available when this fails.
function Start-OllamaService {
    param([string]$Executable, [int]$TimeoutSeconds = 25)
    if (Test-Ollama) { return $true }
    if (-not $Executable) { return $false }
    Write-Host "Starting the local Ollama service on 127.0.0.1:11434..."
    $env:OLLAMA_HOST = "127.0.0.1:11434"
    Start-Process -FilePath $Executable -ArgumentList @("serve") -WindowStyle Hidden
    for ($Attempt = 0; $Attempt -lt ($TimeoutSeconds * 2); $Attempt++) {
        Start-Sleep -Milliseconds 500
        if (Test-Ollama) { return $true }
    }
    return $false
}

Write-Host "`nHINATA setup - local, offline-first assistant" -ForegroundColor Cyan
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
    Write-Host "[1/7] Creating isolated Python environment..."
    Invoke-Native $PythonExe ($PythonPrefix + @("-m", "venv", ".venv"))
}
else {
    Write-Host "[1/7] Using existing .venv"
}

Write-Host "[2/7] Installing HINATA and selected optional components..."
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

Write-Host "[3/7] Running portable tests before model downloads..."
Invoke-Native $VenvPython @("scripts\run_tests.py")

$Model = (& $VenvPython -c "from hinata.config import load_config; print(load_config().ollama.model)").Trim()
if (-not $SkipOllama) {
    Write-Host "[4/7] Checking local Ollama runtime..."
    $OllamaPath = Find-Ollama
    if (-not $OllamaPath) {
        if (Get-Command winget.exe -ErrorAction SilentlyContinue) {
            Write-Host "Installing Ollama through Windows Package Manager..."
            & winget.exe install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -ne 0) {
                throw "Ollama installation failed. Install from https://ollama.com/download/windows and rerun setup."
            }
            $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
            $OllamaPath = Find-Ollama
        }
        else {
            throw "Ollama is missing and winget is unavailable. Install https://ollama.com/download/windows or rerun with -SkipOllama."
        }
    }
    if (-not $OllamaPath) {
        throw "Ollama was installed but ollama.exe was not found in PATH or the default install folders. Install from https://ollama.com/download/windows and rerun setup."
    }
    Write-Host "Ollama executable: $OllamaPath"
    # Connect automatically: a fresh install leaves the service stopped, which is
    # what made the first HINATA message fail with "not a valid JSON plan".
    if (Start-OllamaService -Executable $OllamaPath) {
        Write-Host "Ollama is connected on http://127.0.0.1:11434."
    }
    else {
        Write-Warning "Ollama is installed but is not answering yet. HINATA also starts it automatically when it opens."
    }
    $env:OLLAMA_HOST = "127.0.0.1:11434"
    Write-Host "[5/7] Making sure the local model $Model is present..."
    $InstalledModels = (& $OllamaPath list 2>$null) -join "`n"
    if ($InstalledModels -match [regex]::Escape($Model)) {
        Write-Host "$Model is already downloaded; nothing to fetch."
    }
    elseif ($SkipModelDownload) {
        Write-Host "[5/7] Skipping Ollama model download"
    }
    else {
        Write-Host "[5/7] Pulling $Model (internet setup download)..."
        Invoke-Native $OllamaPath @("pull", $Model)
    }
}
else {
    Write-Host "[4/7] Ollama setup skipped; structured commands remain available"
    Write-Host "[5/7] Model setup skipped"
}

if (-not $Lite -and -not $SkipSpeechDownload) {
    Write-Host "[6/7] Preparing the tiny INT8 offline Whisper model..."
    Write-Host "The first preparation downloads model weights; subsequent transcription is offline."
    Invoke-Native $VenvPython @("-m", "hinata", "setup-speech")
}
else {
    Write-Host "[6/7] Speech model preparation skipped"
}

Write-Host "[7/7] Running a real text-pipeline smoke test..."
Invoke-Native $VenvPython @("-m", "hinata", "--no-llm", "ask", "system", "info")
if (-not $SkipOllama -and -not $Lite) {
    # Prove the automatic connection end to end before the owner sees the window.
    Write-Host "Checking the local model connection..."
    & $VenvPython -m hinata model status
}

Write-Host "`nHINATA setup completed successfully." -ForegroundColor Green
Write-Host "Next recommended step: .\.venv\Scripts\python.exe -m hinata setup-pin"
Write-Host "Desktop:  .\hinata.cmd gui"
Write-Host "Terminal: .\hinata.cmd"
Write-Host "Diagnose: .\hinata.cmd doctor"
Write-Host "Normal use does not require internet.`n"

if ($Run) {
    if (-not $Lite) {
        Invoke-Native $VenvPython @("-m", "hinata", "gui")
    }
    else {
        Invoke-Native $VenvPython @("-m", "hinata")
    }
}
