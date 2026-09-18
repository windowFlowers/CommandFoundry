@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [CommandFoundry] Creating virtual environment...
  py -3 -m venv .venv
)

echo [CommandFoundry] Installing backend dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -e ".[dev,desktop]"

echo [CommandFoundry] Setup complete.
