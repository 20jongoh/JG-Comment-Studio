@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
echo.
echo [1/3] Python 3.11 이상을 확인합니다.
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 goto python_missing
py -3 -m venv .venv
goto venv_ready
:use_python
where python >nul 2>nul
if errorlevel 1 goto python_missing
python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 goto python_missing
python -m venv .venv
:venv_ready
if errorlevel 1 goto install_failed
if not exist ".venv\Scripts\python.exe" goto install_failed
echo.
echo [2/3] 전용 환경에 필요한 패키지를 설치합니다. 처음 한 번만 필요합니다.
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto install_failed
echo.
echo [3/3] 설치 완료. start.bat를 더블클릭하세요.
echo 실제 게시 시연에는 Google Chrome이 설치되어 있어야 합니다.
echo API 키가 없어도 연습 데이터로 화면을 확인할 수 있습니다.
echo.
pause
exit /b 0
:python_missing
echo.
echo Python 3.11 이상을 찾지 못했습니다.
echo https://www.python.org/downloads/windows/ 에서 Python을 설치하세요.
echo 설치 화면에서 Add Python to PATH를 선택한 뒤 이 파일을 다시 실행하세요.
pause
exit /b 1
:install_failed
echo.
echo 설치를 완료하지 못했습니다. 위에 표시된 오류를 확인하세요.
echo 이 창을 캡처해 전달하면 문제를 함께 확인할 수 있습니다. API 키는 입력하지 마세요.
pause
exit /b 1
