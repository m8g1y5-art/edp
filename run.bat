@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
rem 运行环境装在不含中文的位置：MediaPipe 在中文路径下加载不了模型
set "VENV=%LOCALAPPDATA%\edp\venv"
if not exist "%LOCALAPPDATA%\edp" mkdir "%LOCALAPPDATA%\edp"
title 具身数据平台

set "PY="
if exist "%VENV%\Scripts\python.exe" set "PY=%VENV%\Scripts\python.exe"
if exist "%VENV%\python.exe" set "PY=%VENV%\python.exe"
if defined PY if exist "%VENV%\.ok" goto run

:create
echo 正在准备运行环境（只需一次，约 3-8 分钟，期间请不要关闭窗口）...
set "CHECK=import sys; sys.exit(0 if (3,9)<=sys.version_info[:2]<=(3,12) else 1)"
if defined PY goto install
for %%v in (3.11 3.12 3.10 3.9) do (
  py -%%v -c "!CHECK!" >nul 2>&1 && ( py -%%v -m venv "%VENV%" && goto install )
)
for %%b in ("%USERPROFILE%\anaconda3" "%USERPROFILE%\miniconda3" "%ProgramData%\anaconda3" "%ProgramData%\miniconda3") do (
  if exist "%%~b\python.exe" (
    "%%~b\python.exe" -c "!CHECK!" >nul 2>&1 && ( "%%~b\python.exe" -m venv "%VENV%" && goto install )
  )
)
for %%b in ("%USERPROFILE%\anaconda3" "%USERPROFILE%\miniconda3" "%ProgramData%\anaconda3" "%ProgramData%\miniconda3") do (
  if exist "%%~b\Scripts\conda.exe" (
    "%%~b\Scripts\conda.exe" create -p "%VENV%" python=3.11 -y && goto install
  )
)
echo.
echo 没找到可用的 Python 3.9-3.12。请安装 Python 3.11（python.org）后再双击本文件。
pause
exit /b 1

:install
set "PY=%VENV%\Scripts\python.exe"
if exist "%VENV%\python.exe" set "PY=%VENV%\python.exe"
"%PY%" -m pip install -q --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
"%PY%" -m pip install !PIPX! -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple || "%PY%" -m pip install !PIPX! -r requirements.txt
if errorlevel 1 (
  echo 依赖安装失败，请把上面的报错截图发给我。
  pause
  exit /b 1
)
echo ok> "%VENV%\.ok"

:run
rem 用 Anaconda 自带的 Python 建的环境，需要把它的 DLL 目录加进 PATH，否则 numpy 等库可能加载失败
set "PYHOME="
for /f "tokens=1,* delims==" %%a in ('findstr /b /c:"home" "%VENV%\pyvenv.cfg" 2^>nul') do set "PYHOME=%%b"
if defined PYHOME set "PYHOME=!PYHOME:~1!"
if defined PYHOME set "PATH=!PYHOME!;!PYHOME!\Library\bin;!PYHOME!\Library\usr\bin;!PYHOME!\DLLs;%PATH%"

"%PY%" -c "import numpy, cv2, mediapipe" >nul 2>&1
if errorlevel 1 (
  echo 检查到依赖加载失败，正在查找损坏的文件并修复...
  "%PY%" -m app.repair
  if errorlevel 2 (
    if defined PIPX (
      echo.
      echo 重建之后文件仍然被写坏，多半是安全软件在拦截。请把 %LOCALAPPDATA%\edp 加入信任后再试，并把这个窗口截图发给我。
      pause
      exit /b 1
    )
    echo.
    echo 删除损坏的运行环境并重新安装...
    rmdir /s /q "%VENV%"
    set "PY="
    set "PIPX=--no-cache-dir"
    goto create
  )
  "%PY%" -c "import numpy, cv2, mediapipe"
  if errorlevel 1 (
    echo.
    echo 依赖仍然加载失败。请把上面完整的报错（从 Traceback 到最后一行）截图发给我。
    pause
    exit /b 1
  )
)
echo.
echo  具身数据平台已启动：http://127.0.0.1:8765
echo  浏览器会自动打开。用完关掉这个窗口即可停止。
echo.
"%PY%" -m app.server --open
echo.
echo 服务已退出。如果上面有报错，请截图发给我。
pause
