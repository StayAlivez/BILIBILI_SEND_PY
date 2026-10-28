@echo off
chcp 65001 >nul
cd /d "%~dp0"

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

:nopython
echo 未找到 Python。请到 python.org 安装 Python 3.9+，安装时勾选 "Add python.exe to PATH"。
pause
exit /b 1
