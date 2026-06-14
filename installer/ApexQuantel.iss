; ApexQuantel.iss — Inno Setup installer for AQ Agent
;
; What this installer does:
;   1. Copies the packaged AQ Agent files to Program Files
;   2. Creates a Start Menu group and optional desktop shortcut
;   3. Launches the control panel when finished (user then clicks Install AQ Agent)
;
; All first-run configuration (license key, MT5 details, risk settings)
; is handled by the app's built-in onboarding wizard on first launch.
;
; Build (from execution-engine\ dir):
;   powershell -ExecutionPolicy Bypass -File installer\build.ps1

#define MyAppName      "AQ Agent"
#define MyAppPublisher "Apex Quantel"
#define MyAppURL       "https://app.somicast.com"
#define MyAppExeName   "apex-quant-trader-agent\apex-quant-trader-agent.exe"
; MyAppVersion is passed from build.ps1 via /DMyAppVersion=x.y.z
; Fallback so the .iss can still be opened directly in the Inno Setup IDE
#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif

; ============================================================================
[Setup]
; ============================================================================
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/support
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\Apex Quantel\AQ Agent
DefaultGroupName=Apex Quantel
AllowNoIcons=yes
OutputDir=Output
OutputBaseFilename=AQAgentSetup
SetupIconFile=assets\icon.ico
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
WizardStyle=modern
PrivilegesRequired=admin
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} {#MyAppVersion} Installer
VersionInfoProductName={#MyAppName}
CloseApplications=yes
RestartApplications=no

; ============================================================================
[Languages]
; ============================================================================
Name: "english"; MessagesFile: "compiler:Default.isl"

; ============================================================================
[Tasks]
; ============================================================================
Name: "desktopicon"; \
    Description: "Create a &desktop shortcut"; \
    GroupDescription: "Additional icons:"

; ============================================================================
[Dirs]
; ============================================================================
Name: "{app}\data";  Permissions: authusers-modify
Name: "{app}\logs";  Permissions: authusers-modify

; ============================================================================
[Files]
; ============================================================================
; Packaged engine (onedir output from PyInstaller)
Source: "..\dist\apex-quant-trader-agent\*"; \
    DestDir: "{app}\apex-quant-trader-agent"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

; Version stamp
Source: "..\version.txt"; DestDir: "{app}"; Flags: ignoreversion

; Task Scheduler installer script (used by the app's Install AQ Agent button)
Source: "..\install_service.ps1"; DestDir: "{app}"; Flags: ignoreversion

; Utility scripts
Source: "..\scripts\update.ps1";         DestDir: "{app}\scripts"; \
    Flags: ignoreversion skipifsourcedoesntexist
Source: "..\scripts\support-bundle.ps1"; DestDir: "{app}\scripts"; \
    Flags: ignoreversion skipifsourcedoesntexist

; ============================================================================
[Icons]
; ============================================================================
; Start Menu
Name: "{group}\AQ Agent"; \
    Filename: "{app}\{#MyAppExeName}"; \
    WorkingDir: "{app}"; \
    IconFilename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; \
    Filename: "{uninstallexe}"

; Desktop shortcut (optional)
Name: "{commondesktop}\AQ Agent"; \
    Filename: "{app}\{#MyAppExeName}"; \
    WorkingDir: "{app}"; \
    IconFilename: "{app}\{#MyAppExeName}"; \
    Tasks: desktopicon

; ============================================================================
[Run]
; ============================================================================
; Offer to launch the control panel from the Finish page (ticked by default).
; shellexec lets Windows honour the requireAdministrator manifest.
Filename: "{app}\{#MyAppExeName}"; \
    Description: "Launch AQ Agent"; \
    Flags: postinstall nowait skipifsilent shellexec; \
    WorkingDir: "{app}"

; ============================================================================
[UninstallRun]
; ============================================================================
; Remove the scheduled task on uninstall
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
    Parameters: "-ExecutionPolicy Bypass -NonInteractive -File ""{app}\install_service.ps1"" -Action uninstall"; \
    RunOnceId: "RemoveTask"; \
    Flags: runhidden

; ============================================================================
[UninstallDelete]
; ============================================================================
Type: files;          Name: "{app}\config.yaml"
Type: filesandordirs; Name: "{app}\data"
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\__pycache__"
