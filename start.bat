@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title HINATA - start

echo ============================================
echo   HINATA - local AI assistant for Windows
echo ============================================
echo.

:MENU
echo Choose an option by pressing a number:
echo   1. Open HINATA normally
echo   2. Connected setup or full repair
echo   3. Check this PC and local model
echo   4. Offline readiness and acceptance guide
echo   5. Exit
echo.
echo Normal launch is local-first but DOES NOT block internet access.
choice /c 12345 /n /m "Your choice [1-5]: "
if errorlevel 5 exit /b 0
if errorlevel 4 goto GUIDE
if errorlevel 3 goto DIAGNOSTICS
if errorlevel 2 goto CONNECTED_SETUP
if errorlevel 1 goto CHECK_LAUNCH
exit /b 1

:CHECK_LAUNCH
if exist "%~dp0.venv\Scripts\python.exe" goto START_APP
echo.
echo HINATA is not installed yet. Choose option 2 for connected setup.
pause
goto MENU

:CONNECTED_SETUP
echo.
echo Connected setup or full repair can download packages and models.
echo It can be rerun if a previous setup was interrupted.
echo HINATA will:
echo   1. install Python 3.11+ if it is missing (asks first, via winget)
echo   2. create an isolated environment and install HINATA
echo   3. install/check Ollama and download the local AI + speech models
echo   4. run the tests, then open the HINATA desktop window
echo.
echo Internet is needed for the downloads. Keep this window open.
echo.

set "ANS="
set /p "ANS=Start connected installation and downloads now? [y/N] "
if /i not "%ANS%"=="y" goto MENU
call :ensure_python
if errorlevel 1 goto PYTHON_FAIL

call "%~dp0setup.cmd" -Run
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto SETUP_FAIL
exit /b 0

:START_APP
if not exist "%~dp0config.toml" (
    copy "%~dp0config.example.toml" "%~dp0config.toml" >nul
    echo Created config.toml from the reviewed example.
)

rem Repair the install before opening: GUI, offline speech (Whisper), TTS,
rem audio control, and the cached voice model. Internet is used only for
rem the parts that are actually missing on this computer.
set "ANS="
echo Optional repair may download packages and speech models from the internet.
set /p "ANS=Run connected repair now? [y/N] "
if /i "%ANS%"=="y" call :ensure_components
if /i "%ANS%"=="y" if errorlevel 1 goto COMPONENTS_FAIL

call :ensure_ollama
echo Starting the HINATA desktop...
"%~dp0.venv\Scripts\python.exe" -m hinata gui
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto RUN_FAIL
exit /b 0

:DIAGNOSTICS
echo.
if not exist "%~dp0.venv\Scripts\python.exe" goto DIAGNOSTICS_MISSING
echo Checking installed components and local runtime. This is not acceptance testing.
"%~dp0.venv\Scripts\python.exe" -m hinata doctor
echo.
echo Read any missing-component guidance above. For setup choose option 2.
pause
goto MENU

:DIAGNOSTICS_MISSING
echo HINATA is not installed yet. Choose option 2 for connected setup.
pause
goto MENU

:GUIDE
echo.
echo =========== Offline readiness checklist ===========
echo 1. While online, choose option 2 and approve setup. Wait for it to finish.
echo    Setup installs the chat model and speech assets, but not the embedding model.
echo 2. For semantic document search, open Command Prompt while online and run:
echo       ollama pull nomic-embed-text
echo    This is a separate download. If you changed ollama.embedding_model in
echo    config.toml, pull that configured model instead. Lexical search needs no model.
echo 3. Choose option 3 to inspect local readiness. In config.toml keep
echo    ollama.auto_pull = false for offline use. Prepare all files before disconnecting.
echo 4. Choose option 1 and test local chat, a document index and search, memory,
echo    an approved tool, and microphone and speaker on your actual PC. Restart and
echo    repeat the memory and search checks. Review results yourself.
echo 5. To enforce network isolation, prepare Windows Sandbox with networking disabled.
echo    This requires Windows Sandbox and extra preparation; it is not automatic.
echo    Read docs\OFFLINE_ISOLATION.md for exact setup and important limitations.
echo    The normal start option does NOT disable networking or monitor all connections.
echo.
echo General objective verification and autonomous specialist agents are limited.
echo Windows Sandbox, physical voice, and your actual model need testing on your PC.
echo ====================================================
echo.
choice /c OM /n /m "Press O to open the isolation guide in Notepad, or M for menu: "
if errorlevel 2 goto MENU
if errorlevel 1 start "" notepad.exe "%~dp0docs\OFFLINE_ISOLATION.md"
goto MENU

rem ----------------------------------------------------------------
rem Install Python 3.11+ via winget when it is missing. Asks first.
rem ----------------------------------------------------------------
:ensure_python
py -3.11 -c "import sys" >nul 2>&1 && exit /b 0
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && exit /b 0
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && exit /b 0
where winget.exe >nul 2>&1 || exit /b 1
echo Python 3.11 or newer was not found on this PC.
set "ANS="
set /p "ANS=Install it now with Windows Package Manager (winget)? [y/N] "
if /i not "%ANS%"=="y" exit /b 1
winget.exe install -e --id Python.Python.3.11 --accept-package-agreements --accept-source-agreements
if errorlevel 1 exit /b 1
rem winget only updates PATH for new terminals; refresh it here so the
rem setup steps below can find the freshly installed Python in this window.
for /f "delims=" %%P in ('powershell.exe -NoProfile -Command "[Environment]::GetEnvironmentVariable('PATH','Machine') + ';' + [Environment]::GetEnvironmentVariable('PATH','User')"') do set "PATH=%%P"
py -3.11 -c "import sys" >nul 2>&1 && exit /b 0
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && exit /b 0
exit /b 1

rem ----------------------------------------------------------------
rem Verify the runtime pieces and the offline voice model, installing
rem whatever is missing BEFORE the desktop window opens. The helper
rem script prints exactly what it is doing.
rem ----------------------------------------------------------------
:ensure_components
"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\ensure_components.py"
exit /b %ERRORLEVEL%

rem ----------------------------------------------------------------
rem Best effort: make sure the loopback Ollama API answers so the local
rem AI brain works. Structured commands work even without it.
rem ----------------------------------------------------------------
:ensure_ollama
where ollama.exe >nul 2>&1 || exit /b 0
powershell.exe -NoProfile -Command "try { Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 exit /b 0
echo Starting the local Ollama service...
start "HINATA Ollama" /min ollama.exe serve
for /l %%I in (1,1,30) do (
    timeout /t 1 /nobreak >nul 2>&1
    powershell.exe -NoProfile -Command "try { Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
    if not errorlevel 1 exit /b 0
)
echo Ollama is not responding yet; HINATA will still open and structured commands will work.
echo HINATA retries the connection automatically; use Reconnect inside the window.
exit /b 0

:PYTHON_FAIL
echo.
echo Python 3.11 or newer is required but could not be installed automatically.
echo Install it manually from https://www.python.org/downloads/windows/
echo (tick "Add python.exe to PATH"), then double-click start.bat again.
pause
exit /b 1

:SETUP_FAIL
echo.
echo Setup did not complete (exit code %RC%). Read the messages above.
echo Fix the issue, then double-click start.bat again.
pause
exit /b %RC%

:COMPONENTS_FAIL
echo.
echo The missing components (Whisper, GUI, TTS) could not be installed.
echo Check the internet connection, read the messages above, then
echo double-click start.bat again.
pause
exit /b 1

:RUN_FAIL
echo.
echo HINATA exited with error code %RC%.
echo Run "hinata.cmd doctor" for diagnostics.
pause
exit /b %RC%
