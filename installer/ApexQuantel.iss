; ApexQuantel.iss — Inno Setup installer for AQ Agent (multi-agent edition)
;
; What this installer does:
;   1. Copies the packaged AQ Agent files to Program Files
;   2. Creates ProgramData directories for the Manager and agent accounts
;   3. Registers the AQ Manager as a logon-triggered scheduled task
;   4. Creates Start Menu shortcuts and an optional desktop shortcut
;   5. Offers to launch the control panel when finished
;
; Build (from execution-engine-multi\ dir):
;   powershell -ExecutionPolicy Bypass -File installer\build.ps1
;
; Version is passed from build.ps1 via /DMyAppVersion=x.y.z

#define MyAppName      "AQ Agent"
#define MyAppPublisher "Apex Quantel"
#define MyAppURL       "https://app.somicast.com"
#define MyAppExeName   "apex-quant-trader-agent\apex-quant-trader-agent.exe"
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
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
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
; ProgramData — shared data store for Manager + all agents
; install_manager.ps1 applies a private runtime-identity/Admin/SYSTEM ACL.
Name: "{commonappdata}\Apex Quantel"
Name: "{commonappdata}\Apex Quantel\manager"
Name: "{commonappdata}\Apex Quantel\manager\logs"
Name: "{commonappdata}\Apex Quantel\manager\agents"

; ============================================================================
[Files]
; ============================================================================
; Packaged engine (onedir output from PyInstaller)
Source: "..\dist\apex-quant-trader-agent\*"; \
    DestDir: "{app}\apex-quant-trader-agent"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

; Version stamp
Source: "..\version.txt"; DestDir: "{app}"; Flags: ignoreversion

; Manager task installer  (runs on install/uninstall via [Run] / [UninstallRun])
Source: "..\install_manager.ps1"; DestDir: "{app}"; Flags: ignoreversion

; Optional utility scripts
Source: "..\scripts\update.ps1"; \
    DestDir: "{app}\scripts"; \
    Flags: ignoreversion skipifsourcedoesntexist
Source: "..\scripts\support-bundle.ps1"; \
    DestDir: "{app}\scripts"; \
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

; Desktop shortcut (optional task)
Name: "{commondesktop}\AQ Agent"; \
    Filename: "{app}\{#MyAppExeName}"; \
    WorkingDir: "{app}"; \
    IconFilename: "{app}\{#MyAppExeName}"; \
    Tasks: desktopicon

; ============================================================================
[Run]
; ============================================================================

; Register + start the AQ Manager scheduled task — always required
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
    Parameters: "-ExecutionPolicy Bypass -NonInteractive -File ""{app}\install_manager.ps1"" -Action install"; \
    StatusMsg: "Registering AQ Manager service..."; \
    Flags: runhidden waituntilterminated

; Offer to launch the GUI from the Finish page
Filename: "{app}\{#MyAppExeName}"; \
    Description: "Launch AQ Agent"; \
    Flags: postinstall nowait skipifsilent shellexec; \
    WorkingDir: "{app}"

; ============================================================================
[UninstallRun]
; ============================================================================

; Stop and remove the AQ Manager scheduled task
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
    Parameters: "-ExecutionPolicy Bypass -NonInteractive -File ""{app}\install_manager.ps1"" -Action uninstall"; \
    RunOnceId: "RemoveManagerTask"; \
    Flags: runhidden

; ============================================================================
[UninstallDelete]
; ============================================================================
; Remove the clean shipped default config that lives inside the PyInstaller
; _internal\ bundle.  The user's real config lives in %ProgramData% and is
; intentionally kept across uninstalls (it holds agent registrations and trade
; history).  Agents can delete %ProgramData%\Apex Quantel\ manually for a
; clean slate.
Type: files; Name: "{app}\apex-quant-trader-agent\_internal\config.yaml"
; NOTE: {commonappdata}\Apex Quantel\Multi\ is intentionally NOT deleted on
; uninstall — it contains the agent registry, trade history, and user config.

; ============================================================================
; [Code] must be last — Pascal parser runs until EOF, not until next section
; ============================================================================
[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
// Kill all running instances before file copy so Inno Setup never shows
// the "close these applications" dialog to non-technical users.
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'),
       '/F /IM apex-quant-trader-agent.exe',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(1500);
  Result := '';
end;
