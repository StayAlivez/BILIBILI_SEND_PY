@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 免安装版：优先使用内置运行时（pythonw 不弹控制台窗口）
if exist "%~dp0runtime\pythonw.exe" goto portable

rem 源码版：使用系统 Python
where py >nul 2>nul
if not errorlevel 1 goto use_py
where python >nul 2>nul
if errorlevel 1 goto nopython
python main.py %*
if errorlevel 1 pause
exit /b 0

:use_py
py -3 main.py %*
if errorlevel 1 pause
exit /b 0

:portable
set "TCL_LIBRARY=%~dp0runtime\tcl\tcl8.6"
set "TK_LIBRARY=%~dp0runtime\tcl\tk8.6"
start "" "%~dp0runtime\pythonw.exe" "%~dp0main.py"
exit /b 0

:nopython
echo 未找到 Python，也没有内置运行时。
echo 请下载 Releases 里的免安装版，或到 python.org 安装 Python 3.9+。
pause
exit /b 1
