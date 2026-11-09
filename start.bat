@echo off
REM Double-click once. After that Tradewise starts by itself when you log in,
REM and the "Tradewise" icon on your desktop opens it.
cd /d "%~dp0"
where python >nul 2>nul || (echo Python 3.10+ is needed: https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^) & pause & exit /b 1)
if not exist .venv\Scripts\python.exe (
  echo First run: setting up Tradewise ^(about a minute^)...
  python -m venv .venv || (pause & exit /b 1)
  .venv\Scripts\python -m pip install --quiet --upgrade pip
  .venv\Scripts\python -m pip install --quiet -e ".[yahoo]" || (pause & exit /b 1)
)
.venv\Scripts\python -m tradewise.cli start %* || (echo Something went wrong ^(see above^). & pause & exit /b 1)
timeout /t 5 >nul
