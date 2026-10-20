@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not exist ".venv\Scripts\python.exe" goto missing
".venv\Scripts\python.exe" run.py
if errorlevel 1 pause
exit /b
:missing
echo install.bat를 먼저 실행하세요.
pause
exit /b 1
