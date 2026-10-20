@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto missing
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements-dev.txt
if errorlevel 1 goto done
".venv\Scripts\python.exe" -m pytest -q
:done
pause
exit /b
:missing
echo install.bat를 먼저 실행하세요.
pause
exit /b 1
