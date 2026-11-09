@echo off
setlocal
set "ROOT=%~dp0"
set "JOBSCRAPER_DIR=%ROOT%jobscraper"
set "PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [err] Missing .venv. Run: py -3.12 -m venv .venv ^& .venv\Scripts\pip install pymysql cryptography
  pause
  exit /b 1
)
echo [info] Starting HireDesk...
"%PY%" -u "%ROOT%webapp\server.py" %*
if errorlevel 1 pause
endlocal
