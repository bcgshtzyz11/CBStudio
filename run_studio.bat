@echo off
setlocal
cd /d "%~dp0"

set "PYCMD="
where py >nul 2>nul
if errorlevel 1 goto trypy3

py -3.14 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYCMD=py -3.14"
if not "%PYCMD%"=="" goto run

py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYCMD=py -3"
if not "%PYCMD%"=="" goto run

:trypy3
where python >nul 2>nul
if errorlevel 1 goto nopy
set "PYCMD=python"
goto run

:nopy
echo [!] Python 3 not found. Install Python 3.10+ and add it to PATH.
pause
exit /b 1

:run
echo ============================================================
echo   CBStudio - Bench Scenario Studio
echo   URL: http://127.0.0.1:8777/
echo   Bench busy? use:  run_studio.bat --sim
echo ============================================================
echo.
%PYCMD% app.py %*
if errorlevel 1 pause
endlocal
