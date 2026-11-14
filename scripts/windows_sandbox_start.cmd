@echo off
setlocal EnableExtensions
set "PATH=C:\hinata\python;C:\hinata\ollama;%PATH%"
set "PYTHONPATH=C:\hinata\project"
set "HINATA_DATA_DIR=C:\hinata\data"
set "HINATA_CONFIG=C:\hinata\data\offline-config.toml"
set "OLLAMA_HOST=127.0.0.1:11434"
set "OLLAMA_MODELS=C:\hinata\models"
cd /d C:\hinata\project
if not exist "%HINATA_CONFIG%" goto CONFIG_ERROR
"C:\hinata\python\python.exe" -c "from hinata.config import load_config; import sys; c=load_config(); sys.exit(0 if c.ollama.host.rstrip('/') == 'http://127.0.0.1:11434' and not c.ollama.auto_pull else 1)"
if errorlevel 1 goto CONFIG_ERROR
echo Starting isolated local Ollama. No models will be downloaded by this launcher.
start "HINATA local model" /b "C:\hinata\ollama\ollama.exe" serve
echo Starting HINATA. Windows Sandbox networking must be disabled in the .wsb file.
"C:\hinata\python\python.exe" -m hinata gui
if errorlevel 1 pause
exit /b
:CONFIG_ERROR
echo The sandbox config must exist at C:\hinata\data\offline-config.toml and use
echo ollama.host = "http://127.0.0.1:11434" and ollama.auto_pull = false.
pause
exit /b 1
