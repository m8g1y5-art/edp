@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 具身数据平台
set "EDP_DATA=%~dp0data"
set "EDP_MODELS=%~dp0models"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
echo.
echo  具身数据平台正在启动：http://127.0.0.1:8765
echo  浏览器会自动打开。用完关掉这个窗口即可停止。
echo.
"%~dp0python\python.exe" launch.py server --open
echo.
echo 程序已退出。如果上面有报错，请截图发给供应商。
pause
