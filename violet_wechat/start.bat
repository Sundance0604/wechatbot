@echo off
setlocal
cd /d "%~dp0"
set "VIOLET_PYTHON="
if exist "%~dp0.venv\Scripts\python.exe" set "VIOLET_PYTHON=%~dp0.venv\Scripts\python.exe"
if not defined VIOLET_PYTHON if defined VIRTUAL_ENV if exist "%VIRTUAL_ENV%\Scripts\python.exe" set "VIOLET_PYTHON=%VIRTUAL_ENV%\Scripts\python.exe"
if not defined VIOLET_PYTHON if defined CONDA_PREFIX if exist "%CONDA_PREFIX%\python.exe" set "VIOLET_PYTHON=%CONDA_PREFIX%\python.exe"
if not defined VIOLET_PYTHON for /f "delims=" %%I in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "VIOLET_PYTHON=%%I"
if not defined VIOLET_PYTHON (
  echo Run setup.bat first.
  pause
  exit /b 1
)
"%VIOLET_PYTHON%" run.py --open %*
if errorlevel 1 pause
