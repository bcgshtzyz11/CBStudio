@echo off
rem ============================================================
rem  build_exe.bat - 把工作台打成 CBStudio.exe（单目录 + 控制台）
rem
rem  用法:
rem      build_exe.bat          直接打包
rem      build_exe.bat --clean  先清掉 build\ / dist\ / 旧产物再打包
rem
rem  产物（直接落在工程根目录，双击即用）:
rem      CBStudio.exe
rem      _internal\        Python 运行时（必须和 exe 一起拷，不能单独拿 exe）
rem
rem  其余目录照旧留在原地，现场可改，不用重打包:
rem      web\  scenarios\  config.json  can_engine.py  can_pack.py  sdk\
rem
rem  前置: Python 3.10+（装 PyInstaller；脚本会自动 pip install）
rem  32 位 Python 与本脚本无关，只影响 CAN 收发子进程。
rem ============================================================
setlocal
cd /d "%~dp0"

set "PYCMD="
where py >nul 2>nul
if errorlevel 1 goto trypython
py -3.14 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYCMD=py -3.14"
if not "%PYCMD%"=="" goto havepy
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYCMD=py -3"
if not "%PYCMD%"=="" goto havepy
:trypython
where python >nul 2>nul
if errorlevel 1 goto nopy
set "PYCMD=python"
:havepy
echo [*] 解释器: %PYCMD%

%PYCMD% -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo [*] 未装 PyInstaller，正在安装...
    %PYCMD% -m pip install pyinstaller
    if errorlevel 1 (
        echo [!] 安装失败。手动执行: %PYCMD% -m pip install pyinstaller
        pause
        exit /b 1
    )
)

if /i "%~1"=="--clean" (
    echo [*] 清理旧产物...
    if exist build rmdir /s /q build
    if exist dist rmdir /s /q dist
    if exist _internal rmdir /s /q _internal
    if exist CBStudio.exe del /q CBStudio.exe
)

echo [*] 打包中（约 1 分钟）...
%PYCMD% -m PyInstaller --noconfirm --distpath dist --workpath build cbstudio.spec
if errorlevel 1 (
    echo [!] 打包失败，见上方日志。
    pause
    exit /b 1
)

echo [*] 释放到工程根目录...
copy /y "dist\CBStudio\CBStudio.exe" "CBStudio.exe" >nul
if exist _internal rmdir /s /q _internal
xcopy "dist\CBStudio\_internal" "_internal\" /E /I /Q /Y >nul
if errorlevel 1 (
    echo [!] 复制 _internal 失败。
    pause
    exit /b 1
)
rmdir /s /q dist
rmdir /s /q build

echo.
echo [OK] %~dp0CBStudio.exe
echo [i]  双击启动；参数与 run_studio.bat 相同：--sim / --port 9000 / --no-browser / --dry-run
echo [i]  发布整个工程文件夹时，CBStudio.exe 与 _internal\ 要一起带上。
pause
endlocal
