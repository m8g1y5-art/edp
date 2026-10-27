<#
具身数据平台 · Windows 单机安装包构建脚本（在一台能联网的 Windows 电脑上运行一次）

用法（在仓库根目录打开 PowerShell）：
    powershell -ExecutionPolicy Bypass -File packaging\windows\build_windows.ps1

产出：
    dist\edp-<版本>-windows\          绿色版目录（自带 Python 和全部依赖，客户电脑不用联网）
    dist\edp-<版本>-windows.zip       压缩包
    dist\edp-<版本>-setup.exe         安装程序（装了 Inno Setup 6 才会生成）

核心代码保护：
    装了 Python 3.11（python.org 版）+ Visual Studio Build Tools（“使用 C++ 的桌面开发”）时，核心模块编译成 .pyd；
    否则退一步只发 .pyc 字节码（能防直接看源码，但比 .pyd 容易还原），脚本会提示。
#>
param(
  [string]$Out = "dist",
  [string]$Cuda = "cu126",
  [switch]$NoTorch
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
Set-Location (Resolve-Path (Join-Path $PSScriptRoot "..\.."))
$Root = Get-Location
$ver = (Select-String -Path app\version.py -Pattern 'VERSION = "(.+)"').Matches[0].Groups[1].Value
$dist = Join-Path $Root "$Out\edp-$ver-windows"
$tmp = Join-Path $Root "$Out\_tmp"
$PyVer = "3.11.9"
$Tuna = "https://pypi.tuna.tsinghua.edu.cn/simple"
function Step($m) { Write-Host "`n== $m" -ForegroundColor Cyan }
function Get-File($urls, $dst) {
  foreach ($u in $urls) { try { Write-Host "下载 $u"; Invoke-WebRequest -Uri $u -OutFile $dst -UseBasicParsing; return } catch { Write-Host "  失败：$($_.Exception.Message)" } }
  throw "下载失败：$dst"
}

Step "准备目录 $dist（版本 $ver）"
if (Test-Path $dist) { Remove-Item -Recurse -Force $dist }
New-Item -ItemType Directory -Force -Path $dist, $tmp, "$dist\models", "$dist\data" | Out-Null

Step "嵌入式 Python $PyVer"
$zip = "$tmp\python-$PyVer-embed-amd64.zip"
if (-not (Test-Path $zip)) {
  Get-File @("https://registry.npmmirror.com/-/binary/python/$PyVer/python-$PyVer-embed-amd64.zip",
             "https://www.python.org/ftp/python/$PyVer/python-$PyVer-embed-amd64.zip") $zip
}
Expand-Archive -Path $zip -DestinationPath "$dist\python" -Force
# 打开 site-packages，并把程序目录（python 的上一级）加进搜索路径
$pth = Get-ChildItem "$dist\python\python*._pth" | Select-Object -First 1
(Get-Content $pth.FullName) -replace '^#\s*import site', 'import site' | Set-Content $pth.FullName -Encoding ascii
Add-Content $pth.FullName "..`r`nLib\site-packages" -Encoding ascii
$py = "$dist\python\python.exe"

Step "pip 和依赖（约 3–4 GB，需要一段时间）"
$gp = "$tmp\get-pip.py"
if (-not (Test-Path $gp)) { Get-File @("https://bootstrap.pypa.io/get-pip.py") $gp }
& $py $gp --no-warn-script-location -i $Tuna
& $py -m pip install --no-warn-script-location -i $Tuna -r requirements.txt
if (-not $NoTorch) {
  & $py -m pip install --no-warn-script-location torch torchvision --index-url "https://download.pytorch.org/whl/$Cuda"
  if ($LASTEXITCODE -ne 0) { & $py -m pip install --no-warn-script-location torch torchvision -f "https://mirrors.aliyun.com/pytorch-wheels/$Cuda/" -i $Tuna }
  & $py -m pip install --no-warn-script-location --no-deps -i $Tuna ultralytics ultralytics-thop
  & $py -m pip install --no-warn-script-location -i $Tuna polars psutil matplotlib pillow requests
}
& $py -c "import numpy, cv2, mediapipe, fastapi, cryptography; print('依赖检查通过')"
if ($LASTEXITCODE -ne 0) { throw "依赖安装不完整" }

Step "SAM 2 权重"
$w = "$tmp\sam2.1_s.pt"
if (-not (Test-Path $w)) {
  Get-File @("https://github.com/ultralytics/assets/releases/download/v8.3.0/sam2.1_s.pt",
             "https://ghfast.top/https://github.com/ultralytics/assets/releases/download/v8.3.0/sam2.1_s.pt") $w
}
Copy-Item $w "$dist\models\"

Step "程序代码"
$src = "$tmp\src"
if (Test-Path $src) { Remove-Item -Recurse -Force $src }
New-Item -ItemType Directory -Force -Path $src | Out-Null
Copy-Item -Recurse app, launch.py $src
Get-ChildItem $src -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
$compiled = $false
$sysPy = $null
try { $sysPy = (& py -3.11 -c "import sys;print(sys.executable)" 2>$null) } catch {}
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$hasMsvc = (Test-Path $vswhere) -and (& $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath)
if ($sysPy -and $hasMsvc) {
  Step "编译核心模块为 .pyd（Cython + MSVC）"
  & $sysPy -m pip install -q -i $Tuna "cython>=3.0" setuptools
  & $sysPy packaging\compile.py $src "$tmp\rel"
  if ($LASTEXITCODE -eq 0) { $compiled = $true; Copy-Item -Recurse "$tmp\rel\*" $dist }
}
if (-not $compiled) {
  Write-Host "`n[提示] 没有找到 Python 3.11 + Visual Studio Build Tools，核心模块只编译成 .pyc 字节码。" -ForegroundColor Yellow
  Write-Host "       正式交付前建议装好二者重新构建（.pyd 比 .pyc 难还原得多）。" -ForegroundColor Yellow
  Copy-Item -Recurse "$src\*" $dist
  & $py -m compileall -q -b "$dist\app"
  $keep = @("__init__.py", "server.py", "worker.py", "manage.py", "version.py")
  Get-ChildItem "$dist\app\*.py" | Where-Object { $keep -notcontains $_.Name } | Remove-Item
  Get-ChildItem "$dist\app" -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
}
Copy-Item packaging\windows\start.bat "$dist\启动.bat"
Copy-Item packaging\windows\tools.bat "$dist\管理工具.bat"
Copy-Item packaging\docs\Windows单机版说明.md "$dist\使用说明.md" -ErrorAction SilentlyContinue
Set-Content "$dist\VERSION" $ver -Encoding ascii
try { Set-Content "$dist\COMMIT" (git rev-parse --short HEAD) -Encoding ascii } catch {}

Step "自检"
Push-Location $dist
& .\python\python.exe -c "import app.pipeline, app.objects, app.license, app.server; print('模块导入正常')"
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "自检失败" }
Pop-Location

Step "打包 zip"
$z = Join-Path $Root "$Out\edp-$ver-windows.zip"
if (Test-Path $z) { Remove-Item $z }
& tar.exe -a -c -f $z -C (Split-Path $dist) (Split-Path $dist -Leaf)

$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
  Step "生成安装程序（Inno Setup）"
  & $iscc "/DAppVersion=$ver" "/DSrcDir=$dist" "/O$(Join-Path $Root $Out)" packaging\windows\edp.iss
} else {
  Write-Host "`n没有装 Inno Setup 6，跳过 setup.exe；直接发 zip 也可以（解压到纯英文路径，双击“启动.bat”）。"
}
Write-Host "`n完成：$dist" -ForegroundColor Green
