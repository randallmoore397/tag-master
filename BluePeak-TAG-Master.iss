; BluePeak TAG Master - Inno Setup installer script
; Build with:  ISCC.exe BluePeak-TAG-Master.iss
; Produces:    Output\BluePeak-TAG-Master-Setup.exe

#define AppName "BluePeak TAG Master"
#ifndef AppVersion
#define AppVersion "1.4.0"
#endif
#define AppPublisher "BluePeak"
#define AppExeName "BluePeak TAG Master.exe"
#ifndef BuildDir
#define BuildDir "dist"
#endif

[Setup]
AppId={{7E1C4A92-3B6D-4F1E-9C2A-8D5F0B3E6A17}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=BluePeak-TAG-Master-Setup
SetupIconFile=BluePeak-TAG-Master.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
SetupLogging=yes
; The app copies its executable into build_out\ during packaging, so the running
; process can hold the target file open. An in-app update calls the app's own
; /api/exit route and then waits for the process to stop before launching the
; installer, so letting the user continue or cancel is the safest default here.
CloseApplications=no
; The app stores its database (bluepeak_tag_master.db) next to the exe, so it must be
; installed somewhere the user can write to. This per-user location needs no admin rights.
DefaultDirName={localappdata}\{#AppName}

[Code]
// An in-app update quits the app and then launches this installer. Wait (up to a
// minute) for the old process to release its files before copying anything over.
function InitializeSetup(): Boolean;
var
  i: Integer;
  rc: Integer;
begin
  Result := True;
  for i := 1 to 60 do
  begin
    if not Exec(ExpandConstant('{cmd}'), '/c tasklist /FI "IMAGENAME eq {#AppExeName}" | find /I "{#AppExeName}" >nul',
      '', SW_HIDE, ewWaitUntilTerminated, rc) then Break;
    if rc <> 0 then Break;
    Sleep(1000);
  end;
end;

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
; Never ship a database, log or backups from a build folder. The user's data in
; {app} must survive upgrades, and the installer must never overwrite it.
Source: "{#BuildDir}\BluePeak TAG Master\*"; DestDir: "{app}"; Excludes: "*.db,*.db-wal,*.db-shm,*.log,backups\*"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
; In-app updates run the installer silently, so relaunch the app ourselves.
Filename: "{app}\{#AppExeName}"; Flags: nowait; Check: WizardSilent

[UninstallDelete]
; Keep the database file on uninstall so user data is not lost; only remove program files.
Type: filesandordirs; Name: "{app}\_internal"
