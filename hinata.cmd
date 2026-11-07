@echo off
setlocal
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" -m hinata %*
) else (
    py -3.11 -m hinata %*
)
exit /b %ERRORLEVEL%
