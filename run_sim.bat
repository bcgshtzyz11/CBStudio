@echo off
rem 仿真演示模式：不接任何硬件，用内置模拟数据看界面
setlocal
cd /d "%~dp0"
call run_studio.bat --sim %*
endlocal
