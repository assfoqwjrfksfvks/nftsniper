@echo off
call "%~dp0START.bat" --headless --setup --autostart %*
exit /b %errorlevel%
