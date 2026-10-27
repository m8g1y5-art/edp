@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "EDP_DATA=%~dp0data"
set "EDP_MODELS=%~dp0models"
set "PYTHONUTF8=1"
echo 具身数据平台 · 管理工具
echo.
echo   本机指纹：
"%~dp0python\python.exe" launch.py manage license fingerprint
echo.
echo   常用命令（在下面输入）：
echo     edp license show            查看许可证
echo     edp license install 文件     安装许可证
echo     edp db status               数据库版本和备份
echo     edp db rollback             恢复到升级前的数据库（先关掉“启动.bat”的窗口）
echo.
doskey edp="%~dp0python\python.exe" "%~dp0launch.py" manage $*
cmd /k
