; ═══════════════════════════════════════════════════════════════════════
; Magic Key Assistant — Inno Setup Script
; ═══════════════════════════════════════════════════════════════════════
;
; Prerequisites
; ─────────────
;   1. Build the tray exe:  pyinstaller MagicKeyAssistant.spec
;   2. Install Inno Setup:  https://jrsoftware.org/isinfo.php
;   3. Open this file in Inno Setup Compiler and click Build → Compile.
;
; The resulting Setup.exe installs:
;   • MagicKeyAssistant.exe  (system-tray controller)
;   • LeisureLLM/            (bot source + prompts + templates)
;   • launcher.py, start.py  (bootstrap / quick-start helpers)
;   • requirements, configs, docs
;
; On first launch the tray icon detects that no venv exists yet and
; prompts the user to run setup (which creates the venv, installs
; dependencies, and opens the setup wizard).
;
; ═══════════════════════════════════════════════════════════════════════

#define MyAppName      "Magic Key Assistant"
#define MyAppVersion   "0.8.0"
#define MyAppPublisher "LeisureLLM"
#define MyAppURL       "https://github.com/mtrmagickey/LeisureCenterAssistant"
#define MyAppExeName   "MagicKeyAssistant.exe"

#define PythonVersion   "3.13.1"
#define PythonInstaller "python-" + PythonVersion + "-amd64.exe"
#define PythonURL       "https://www.python.org/ftp/python/" + PythonVersion + "/" + PythonInstaller

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
; Allow user-level install (no admin rights needed)
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputBaseFilename=MagicKeyAssistant-Setup-{#MyAppVersion}
SetupIconFile=MTRMK-Assistant-Icon.ico
UninstallDisplayIcon={app}\MagicKeyAssistant\MagicKeyAssistant.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon";  Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startupicon";  Description: "Start automatically with Windows"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
; ── Tray controller (PyInstaller output) ──────────────────────────────
Source: "dist\MagicKeyAssistant\*"; DestDir: "{app}\MagicKeyAssistant"; Flags: ignoreversion recursesubdirs

; ── Bot source code ───────────────────────────────────────────────────
Source: "LeisureLLM\*.py";          DestDir: "{app}\LeisureLLM"; Flags: ignoreversion
; NOTE: *.txt includes requirements.txt and discord_message_schema.txt (NOT docs/)
Source: "LeisureLLM\*.txt";         DestDir: "{app}\LeisureLLM"; Flags: ignoreversion
; EXCLUDED: *.csv (hashes_v3.csv contains deployment-specific paths)
; EXCLUDED: *.db  (database is deployment-specific)
Source: "LeisureLLM\*.bat";         DestDir: "{app}\LeisureLLM"; Flags: ignoreversion
Source: "LeisureLLM\*.ps1";         DestDir: "{app}\LeisureLLM"; Flags: ignoreversion
Source: "LeisureLLM\*.lock";        DestDir: "{app}\LeisureLLM"; Flags: ignoreversion skipifsourcedoesntexist

; Sub-packages
Source: "LeisureLLM\admin\*";       DestDir: "{app}\LeisureLLM\admin";       Flags: ignoreversion recursesubdirs
Source: "LeisureLLM\cogs\*";        DestDir: "{app}\LeisureLLM\cogs";        Flags: ignoreversion recursesubdirs
Source: "LeisureLLM\core\*";        DestDir: "{app}\LeisureLLM\core";        Flags: ignoreversion recursesubdirs
; Only ship reference/template config — deployment-specific state files
; (.setup_complete, .admin_token, org_profile.yaml, bot_settings.json,
; model_router.json, onboarding_*.json/jsonl) are created by the setup wizard.
Source: "LeisureLLM\config\org_profile.example.yaml";  DestDir: "{app}\LeisureLLM\config"; Flags: ignoreversion
Source: "LeisureLLM\config\recommended_models.json";   DestDir: "{app}\LeisureLLM\config"; Flags: ignoreversion skipifsourcedoesntexist
Source: "LeisureLLM\config\workflows.yaml";            DestDir: "{app}\LeisureLLM\config"; Flags: ignoreversion skipifsourcedoesntexist
Source: "LeisureLLM\config\rail_maps.yaml";            DestDir: "{app}\LeisureLLM\config"; Flags: ignoreversion skipifsourcedoesntexist
Source: "LeisureLLM\migrations\*";  DestDir: "{app}\LeisureLLM\migrations";  Flags: ignoreversion recursesubdirs
; Only ship generic prompts — operational_context.txt and system_prompt_backup.txt are deployment-specific
Source: "LeisureLLM\prompts\system_prompt.txt";        DestDir: "{app}\LeisureLLM\prompts"; Flags: ignoreversion
Source: "LeisureLLM\prompts\exercise_types.md";        DestDir: "{app}\LeisureLLM\prompts"; Flags: ignoreversion
Source: "LeisureLLM\prompts\operational_context.example.txt"; DestDir: "{app}\LeisureLLM\prompts"; Flags: ignoreversion
Source: "LeisureLLM\prompts\personas\*";              DestDir: "{app}\LeisureLLM\prompts\personas"; Flags: ignoreversion recursesubdirs skipifsourcedoesntexist
; Optional: bundled wheels for offline dependency install
Source: "LeisureLLM\wheels\*";       DestDir: "{app}\LeisureLLM\wheels";       Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
; EXCLUDED: LeisureLLM\scripts\* (developer-only tools, not needed for deployment)
Source: "LeisureLLM\services\*";    DestDir: "{app}\LeisureLLM\services";    Flags: ignoreversion recursesubdirs

; ── Root helpers ──────────────────────────────────────────────────────
Source: "launcher.py";              DestDir: "{app}"; Flags: ignoreversion
Source: "start.py";                 DestDir: "{app}"; Flags: ignoreversion
Source: "tray.py";                  DestDir: "{app}"; Flags: ignoreversion
Source: "config.py";                DestDir: "{app}"; Flags: ignoreversion
Source: "requirements.txt";         DestDir: "{app}"; Flags: ignoreversion
Source: "MTRMK-Assistant-Icon.ico"; DestDir: "{app}"; Flags: ignoreversion

; ── Documentation ─────────────────────────────────────────────────────
Source: "README.md";                DestDir: "{app}"; Flags: ignoreversion
Source: "GET_STARTED.md";           DestDir: "{app}"; Flags: ignoreversion
Source: "INSTALLATION.md";          DestDir: "{app}"; Flags: ignoreversion
Source: "LICENSE";                  DestDir: "{app}"; Flags: ignoreversion

[Icons]
; Start Menu
Name: "{group}\{#MyAppName}";            Filename: "{app}\MagicKeyAssistant\{#MyAppExeName}"; IconFilename: "{app}\MTRMK-Assistant-Icon.ico"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"

; Desktop (optional)
Name: "{autodesktop}\{#MyAppName}";      Filename: "{app}\MagicKeyAssistant\{#MyAppExeName}"; IconFilename: "{app}\MTRMK-Assistant-Icon.ico"; Tasks: desktopicon

; Startup folder (optional)
Name: "{userstartup}\{#MyAppName}";      Filename: "{app}\MagicKeyAssistant\{#MyAppExeName}"; Tasks: startupicon

[Run]
; Offer to launch after install
Filename: "{app}\MagicKeyAssistant\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Clean up generated files on uninstall
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\LeisureLLM\__pycache__"
Type: files;          Name: "{app}\tray.log"
Type: files;          Name: "{app}\leisurellm.log"

[Code]
// ── Auto-install Python if missing ──────────────────────────────────
//
// 1. Check whether `python --version` succeeds.
// 2. If not, download the official CPython installer (~30 MB).
// 3. Run it silently with PrependPath=1 so the bot can find it.
//
// The download URL points to python.org's stable Windows x64 installer.
// Update the version number here when upgrading.
// ─────────────────────────────────────────────────────────────────────

function PythonInstalled(): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec('python', '--version', '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
            and (ResultCode = 0);
end;

// Download a file via PowerShell (works on all modern Windows)
function DownloadFile(const URL, DestPath: string): Boolean;
var
  ResultCode: Integer;
  CmdLine: string;
begin
  CmdLine := '-NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri ''' + URL + ''' -OutFile ''' + DestPath + ''' -UseBasicParsing"';
  Result := Exec('powershell.exe', CmdLine, '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
            and (ResultCode = 0)
            and FileExists(DestPath);
end;

function InstallPython(): Boolean;
var
  InstallerPath: string;
  ResultCode: Integer;
begin
  Result := False;
  InstallerPath := ExpandConstant('{tmp}\{#PythonInstaller}');

  WizardForm.StatusLabel.Caption := 'Downloading Python {#PythonVersion} …';
  WizardForm.StatusLabel.Update;

  if not DownloadFile('{#PythonURL}', InstallerPath) then
  begin
    MsgBox(
      'Failed to download Python.'#13#10#13#10 +
      'Please install Python 3.10+ manually from'#13#10 +
      'https://www.python.org/downloads/'#13#10#13#10 +
      'Make sure to check "Add python.exe to PATH".',
      mbError, MB_OK);
    Exit;
  end;

  WizardForm.StatusLabel.Caption := 'Installing Python {#PythonVersion} …';
  WizardForm.StatusLabel.Update;

  // Silent user-level install, add to PATH, include pip
  if Exec(InstallerPath,
          '/quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_launcher=0',
          '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    Result := (ResultCode = 0);
  end;

  if not Result then
  begin
    MsgBox(
      'Python installation returned an error (code ' + IntToStr(ResultCode) + ').'#13#10#13#10 +
      'Please install Python 3.10+ manually from'#13#10 +
      'https://www.python.org/downloads/'#13#10#13#10 +
      'Make sure to check "Add python.exe to PATH".',
      mbError, MB_OK);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    if not PythonInstalled() then
    begin
      if MsgBox(
        'Python was not found on this computer.'#13#10#13#10 +
        'Magic Key Assistant needs Python 3.10+ to run.'#13#10 +
        'Would you like the installer to download and install it now?'#13#10#13#10 +
        '(~30 MB download, user-level install, no admin required)',
        mbConfirmation, MB_YESNO) = IDYES then
      begin
        InstallPython();
      end
      else
      begin
        MsgBox(
          'You can install Python later from'#13#10 +
          'https://www.python.org/downloads/'#13#10#13#10 +
          'Make sure to check "Add python.exe to PATH".',
          mbInformation, MB_OK);
      end;
    end;
  end;
end;
