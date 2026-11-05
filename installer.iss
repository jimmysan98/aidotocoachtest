; Inno Setup Script для сборки установщика AIDOTA
; Создаёт автономный установочный файл AIDOTA_Setup.exe
; Версия передаётся из build_exe.py: ISCC.exe /DMyAppVersion=1.2.0 installer.iss

#ifndef MyAppVersion
  #define MyAppVersion "1.2.0"
#endif
#define MyAppName "AIDOTA"
#define MyAppPublisher "AIDOTA Team"
#define MyAppExeName "AIDOTA.exe"

[Setup]
AppId={{E57D284B-9A71-464A-A91B-4E38DFB74BC1}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir=.
OutputBaseFilename=AIDOTA_Setup
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
MinVersion=10.0
DisableProgramGroupPage=auto
DisableWelcomePage=no
PrivilegesRequired=lowest
LicenseFile=LICENSE
InfoBeforeFile=installer_notes.txt
VersionInfoVersion={#MyAppVersion}.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} — голосовой ИИ-тренер по Dota 2
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "autostart"; Description: "Запускать AIDOTA при старте Windows"; GroupDescription: "Автозапуск:"; Flags: unchecked

[Files]
Source: "dist\AIDOTA\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; \
    Excludes: "config.json,.cache\*,logs\*,*.log,__pycache__\*,assets\screenshot-*,assets\readme-banner.png,installer_notes.txt"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\icon.ico"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\icon.ico"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\.cache"
Type: files; Name: "{app}\config.json"
Type: files; Name: "{app}\data\coach_memory.json"
Type: files; Name: "{app}\data\player_profile.json"
