@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Mage-VL 语义服务
set "HF_HOME=%~dp0hf_cache"
set "HF_HUB_OFFLINE=1"
set "PYTHONIOENCODING=utf-8"
"%~dp0venv\Scripts\python.exe" "%~dp0mage_server.py"
echo.
echo 服务已退出。如果上面有报错，请截图发给我。
pause
