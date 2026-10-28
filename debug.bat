@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 调试用启动器：保留控制台，能看到异常和输出（也支持 --cli）

if exist "%~dp0runtime\python.exe" (
    set "TCL_LIBRARY=%~dp0runtime\tcl\tcl8.6"
    set "TK_LIBRARY=%~dp0runtime\tcl\tk8.6"
    "%~dp0runtime\python.exe" "%~dp0main.py" %*
    goto done
)

where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 "%~dp0main.py" %*
goto done

:use_python
python "%~dp0main.py" %*

:done
if errorlevel 1 pause
