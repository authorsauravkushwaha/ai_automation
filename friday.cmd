@echo off
setlocal
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" -m friday %*
) else (
    py -3.11 -m friday %*
)
exit /b %ERRORLEVEL%
