@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title HINATA - start

echo ============================================
echo   HINATA - local AI assistant for Windows
echo ============================================
echo.

rem Already installed? Then just make sure Ollama is up and launch the app.
if exist "%~dp0.venv\Scripts\python.exe" goto START_APP

rem -------- First run: set everything up, then launch --------
echo This is the first run. HINATA will now:
echo   1. install Python 3.11+ if it is missing (asks first, via winget)
echo   2. create an isolated environment and install HINATA
echo   3. install/check Ollama and download the local AI + speech models
echo   4. run the tests, then open the HINATA desktop window
echo.
echo Internet is needed for the downloads. Keep this window open.
echo.

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
call :ensure_ollama
echo Starting the HINATA desktop...
"%~dp0.venv\Scripts\python.exe" -m hinata gui
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto RUN_FAIL
exit /b 0

rem ----------------------------------------------------------------
rem Install Python 3.11+ via winget when it is missing. Asks first.
rem ----------------------------------------------------------------
:ensure_python
py -3.11 -c "import sys" >nul 2>&1 && exit /b 0
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && exit /b 0
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && exit /b 0
where winget.exe >nul 2>&1 || exit /b 1
echo Python 3.11 or newer was not found on this PC.
set /p "ANS=Install it now with Windows Package Manager (winget)? [Y/n] "
if /i "%ANS%"=="n" exit /b 1
winget.exe install -e --id Python.Python.3.11 --accept-package-agreements --accept-source-agreements
if errorlevel 1 exit /b 1
rem winget only updates PATH for new terminals; refresh it here so the
rem setup steps below can find the freshly installed Python in this window.
for /f "delims=" %%P in ('powershell.exe -NoProfile -Command "[Environment]::GetEnvironmentVariable('PATH','Machine') + ';' + [Environment]::GetEnvironmentVariable('PATH','User')"') do set "PATH=%%P"
py -3.11 -c "import sys" >nul 2>&1 && exit /b 0
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && exit /b 0
exit /b 1

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

:RUN_FAIL
echo.
echo HINATA exited with error code %RC%.
echo Run "hinata.cmd doctor" for diagnostics.
pause
exit /b %RC%
