@echo off
cd /d "%~dp0"
echo Starting MealSeq Engine...
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "scheduler\api_server.py"
  goto :done
)

where py >nul 2>nul
if not errorlevel 1 (
  py -3 "scheduler\api_server.py"
  goto :done
)

python "scheduler\api_server.py"

:done
pause
