; 具身数据平台 · Windows 安装程序（Inno Setup 6）
; 由 build_windows.ps1 调用：ISCC /DAppVersion=1.0.0 /DSrcDir=... edp.iss
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
[Setup]
AppId={{6B1F3C2A-0E4D-4D6B-9C1A-EDP000000001}
AppName=具身数据平台
AppVersion={#AppVersion}
AppPublisher=百行数智科技有限公司
; 装到纯英文路径：MediaPipe 在中文路径下加载不了模型
DefaultDirName=C:\EDP
DisableProgramGroupPage=yes
OutputBaseFilename=edp-{#AppVersion}-setup
Compression=lzma2/fast
SolidCompression=no
DiskSpanning=no
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
UninstallDisplayName=具身数据平台 {#AppVersion}
[Languages]
Name: "cn"; MessagesFile: "compiler:Default.isl"
[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion; Excludes: "data\*"
[Dirs]
Name: "{app}\data"; Flags: uninsneveruninstall
[Icons]
Name: "{autodesktop}\具身数据平台"; Filename: "{app}\启动.bat"; WorkingDir: "{app}"
Name: "{autoprograms}\具身数据平台\具身数据平台"; Filename: "{app}\启动.bat"; WorkingDir: "{app}"
Name: "{autoprograms}\具身数据平台\管理工具"; Filename: "{app}\管理工具.bat"; WorkingDir: "{app}"
[Run]
Filename: "{app}\启动.bat"; Description: "现在启动"; Flags: postinstall shellexec nowait skipifsilent
