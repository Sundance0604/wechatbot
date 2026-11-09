@echo off
setlocal
cd /d "%~dp0"
set "CLEANER_PYTHON="
if defined VIRTUAL_ENV if exist "%VIRTUAL_ENV%\Scripts\python.exe" set "CLEANER_PYTHON=%VIRTUAL_ENV%\Scripts\python.exe"
if not defined CLEANER_PYTHON if exist "%~dp0.venv\Scripts\python.exe" set "CLEANER_PYTHON=%~dp0.venv\Scripts\python.exe"
if not defined CLEANER_PYTHON set "CLEANER_PYTHON=python"
"%CLEANER_PYTHON%" run.py %*
if errorlevel 1 pause
