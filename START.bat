@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
if errorlevel 1 goto failed
if exist ".venv\Scripts\python.exe" if exist ".venv\pyvenv.cfg" goto check_environment

py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 (
    set "DM_PYTHON=py -3"
    goto create_environment
)
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 (
    set "DM_PYTHON=python"
    goto create_environment
)
python3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 (
    set "DM_PYTHON=python3"
    goto create_environment
)
echo Python 3.10 or newer is required. Install Python with pip, Tkinter and Add Python to PATH.
goto failed

:create_environment
%DM_PYTHON% -m venv ".venv"
if errorlevel 1 goto failed

:check_environment
".venv\Scripts\python.exe" -c "import sys; sys.exit(sys.prefix == sys.base_prefix or sys.version_info < (3, 10))"
if errorlevel 1 (
    echo The existing .venv uses an older Python. Rename .venv and try again with Python 3.10+.
    goto failed
)
".venv\Scripts\python.exe" -c "import aiohttp, bs4; from importlib.metadata import version; a=tuple(map(int,version('aiohttp').split('.')[:2])); b=tuple(map(int,version('beautifulsoup4').split('.')[:2])); assert (3,10) <= a < (4,0) and (4,12) <= b < (5,0)" >nul 2>&1
if not errorlevel 1 goto run_app
echo Installing dependencies into .venv. Internet access is required.
".venv\Scripts\python.exe" -m ensurepip --upgrade
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip --isolated install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto failed

:run_app
".venv\Scripts\python.exe" gift_flipper_app.py %*
if errorlevel 1 goto failed
exit /b 0

:failed
echo DM Hunter could not start. See the error above.
pause
exit /b 1
