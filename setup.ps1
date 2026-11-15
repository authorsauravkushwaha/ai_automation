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
    [switch]$LenovoLOQFleet,
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

function Confirm-OptionalDownload {
    param([string]$Question)
    # A redirected/CI setup must never interpret EOF as consent or hang forever.
    try {
        if ([Console]::IsInputRedirected) {
            Write-Host "Non-interactive setup: skipping optional download (no consent)."
            return $false
        }
        $Reply = Read-Host "$Question [y/N]"
        return ($Reply -match '^(y|yes)$')
    }
    catch {
        Write-Host "No interactive consent received; skipping optional download."
        return $false
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
    $env:OLLAMA_MAX_LOADED_MODELS = "1"
    $env:OLLAMA_FLASH_ATTENTION = "1"
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
if ($LenovoLOQFleet) {
    Write-Host "-LenovoLOQFleet still requires interactive [y/N] consent; no automatic downloads."
}

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
    # Keep typed replies audible even on the small, no-microphone install.
    $Extras = @("tts")
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
        if (-not (Confirm-OptionalDownload "Ollama is missing. Install the local Ollama runtime now?")) {
            throw "Ollama installation declined. Rerun with -SkipOllama for structured commands only."
        }
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
    $OllamaVersionText = (& $OllamaPath --version) -join " "
    if ($OllamaVersionText -match '(\d+\.\d+\.\d+)') {
        $OllamaVersion = [version]$Matches[1]
        if ($OllamaVersion -lt [version]"0.6.2") {
            Write-Warning "Ollama 0.6.2 or newer is recommended for the Lenovo LOQ model fleet."
        }
        Write-Host "Ollama version: $OllamaVersion (minimum 0.6.2)."
    }
    else {
        Write-Warning "Cannot determine Ollama version. Ensure Ollama 0.6.2 or newer is installed."
    }
    # Connect automatically: a fresh install leaves the service stopped, which is
    # what made the first HINATA message fail with "not a valid JSON plan".
    if (Start-OllamaService -Executable $OllamaPath) {
        Write-Host "Ollama is connected on http://127.0.0.1:11434."
    }
    else {
        Write-Warning "Ollama is installed but is not answering yet. HINATA also starts it automatically when it opens."
    }
    $env:OLLAMA_HOST = "127.0.0.1:11434"
    $env:OLLAMA_MAX_LOADED_MODELS = "1"
    $env:OLLAMA_FLASH_ATTENTION = "1"
    $FleetInstalled = $false
    if (-not $SkipModelDownload -and -not $Lite) {
        $PackInfoJson = & $VenvPython -c "import json; from hinata.core.jarvis_models import select_optimal_fleet_pack; from hinata.core.providers import HardwareCapabilityService; p=select_optimal_fleet_pack(HardwareCapabilityService().detect()); print(json.dumps(p.to_dict()))"
        if ($LASTEXITCODE -ne 0) { throw "Could not detect the Lenovo LOQ i5 fleet" }
        $Pack = $PackInfoJson | ConvertFrom-Json
        Write-Host "Recommended Lenovo LOQ i5 fleet: $($Pack.title)"
        Write-Host "Selected model downloads (several GB total; internet and free disk space required):"
        foreach ($FleetModel in $Pack.ollama_pull_commands) { Write-Host "  $FleetModel" }
        if (Confirm-OptionalDownload "Download and activate this multi-AI fleet during setup?") {
            if (-not $OllamaVersion -or $OllamaVersion -lt [version]"0.6.2") {
                throw "Ollama 0.6.2 or newer is required for the selected fleet. Update via https://ollama.com/download/windows."
            }
            foreach ($FleetModel in $Pack.ollama_pull_commands) {
                $Name = ($FleetModel -split ' ', 3)[-1]
                Invoke-Native $OllamaPath @("pull", $Name)
            }
            # Only update [ollama] settings after every pull succeeds; preserve other sections.
            Invoke-Native $VenvPython @("-m", "hinata", "model", "apply-fleet", "--pack", $Pack.pack_id)
            $FleetInstalled = $true
            Write-Host "Multi-AI fleet is installed and configured; previous config.toml is backed up as config.toml.bak*."
        }
        else { Write-Host "Multi-AI fleet declined. No specialist models were downloaded." }
    }
    if (-not $FleetInstalled) {
        if ($SkipModelDownload) { Write-Host "[5/7] Model downloads skipped (-SkipModelDownload)" }
        else {
            Write-Host "The baseline local chat model is: $Model"
            $InstalledModels = (& $OllamaPath list 2>$null) -join "`n"
            if ($InstalledModels -match [regex]::Escape($Model)) {
                Write-Host "$Model is already downloaded; nothing to fetch."
            }
            elseif (Confirm-OptionalDownload "Download this baseline chat model during setup?") {
                Invoke-Native $OllamaPath @("pull", $Model)
            }
            else { Write-Host "Baseline model declined; chat waits until a model is installed." }
        }
    }
}
else {
    Write-Host "[4/7] Ollama setup skipped; structured commands remain available"
    Write-Host "[5/7] Model setup skipped"
}

# Optional local image generation uses a real SD-Turbo diffusion model, not Qwen
# pretending to render photos. Packages/weights are installed ONLY after consent.
if (-not $Lite -and -not $SkipModelDownload) {
    Write-Host "Optional real PNG image generation: SD-Turbo (large PyTorch install + multi-GB weights)."
    Write-Host "Inference runs locally after setup; CUDA needs a compatible NVIDIA driver. CPU works slowly."
    if (Confirm-OptionalDownload "Install image packages and download SD-Turbo weights now?") {
        Invoke-Native $VenvPython @("-m", "pip", "install", "-e", ".[image]")
        $CudaAvailable = (& $VenvPython -c "import torch; print('yes' if torch.cuda.is_available() else 'no')").Trim()
        if ($CudaAvailable -ne "yes") {
            Write-Warning "PyTorch CUDA is not available. Real image generation will use CPU and may be very slow. Check your NVIDIA driver/CUDA PyTorch wheel."
        }
        Invoke-Native $VenvPython @("-m", "hinata", "image", "setup")
    }
    else { Write-Host "Image weights declined; SVG creation and existing loopback image servers remain available." }
}

if (-not $Lite -and -not $SkipSpeechDownload -and -not $SkipModelDownload -and
    (Confirm-OptionalDownload "Prepare/download the tiny offline Whisper speech model now?")) {
    Write-Host "[6/7] Preparing the tiny INT8 offline Whisper model..."
    Write-Host "The first preparation downloads model weights; subsequent transcription is offline."
    Invoke-Native $VenvPython @("-m", "hinata", "setup-speech")
}
else {
    Write-Host "[6/7] Speech model preparation skipped"
}

Write-Host "[7/7] Running a real text-pipeline smoke test..."
Invoke-Native $VenvPython @("-m", "hinata", "--no-llm", "--no-speak", "ask", "system", "info")
if (-not $SkipOllama -and -not $Lite) {
    # Prove the automatic connection end to end before the owner sees the window.
    Write-Host "Checking the local model connection..."
    & $VenvPython -m hinata model status
}

# Existing settings may have disabled replies in an older release. Ask instead
# of silently overriding a saved privacy choice or requiring config editing.
$VoiceStatusJson = & $VenvPython -m hinata voice-output status
if ($LASTEXITCODE -eq 0) {
    $VoiceStatus = $VoiceStatusJson | ConvertFrom-Json
    if (-not $VoiceStatus.replies_enabled) {
        Write-Host "Existing HINATA settings have spoken replies muted."
        if (Confirm-OptionalDownload "Enable spoken replies for typed and voice requests?") {
            Invoke-Native $VenvPython @("-m", "hinata", "voice-output", "enable")
        }
    }
    if (Confirm-OptionalDownload "Play a short offline voice test through your speakers now?") {
        & $VenvPython -m hinata voice-output test
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Voice test failed. Check the audio device or run: .\hinata.cmd voice-output status"
        }
    }
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
