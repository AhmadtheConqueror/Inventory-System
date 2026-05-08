@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)
set FLASK_APP=app.py
set FLASK_ENV=development
%PYTHON% -m flask --app app run --host 127.0.0.1 --port 5052
pause
