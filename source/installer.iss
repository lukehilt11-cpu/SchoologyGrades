; Builds the Windows installer. build.bat runs it for you:
;   ISCC /DMyAppVersion=1.0 /DAppSource=<program folder> /O<releases folder> installer.iss
; Needs Inno Setup 6 (free): winget install JRSoftware.InnoSetup

#ifndef MyAppVersion
  #define MyAppVersion "1.0"
#endif
#ifndef AppSource
  #define AppSource "dist\SchoologyGrades"
#endif
#define MyAppName "Schoology Grade Checker"

[Setup]
AppId={{BFB6D6DB-5644-43C7-B2D3-96CBA92D85E0}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
; Per-user install: no administrator rights needed. {autopf} then means
; %LOCALAPPDATA%\Programs, a folder the programs can write their data into.
PrivilegesRequired=lowest
DefaultDirName={autopf}\SchoologyGrades
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputBaseFilename=SchoologyGrades-{#MyAppVersion}-Setup
SetupIconFile=icons\grades.ico
WizardImageFile=icons\wizard_large.bmp
WizardSmallImageFile=icons\wizard_small.bmp
UninstallDisplayIcon={app}\SchoologyGrades.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Updating over a running copy closes it first.
CloseApplications=yes
RestartApplications=no

[Files]
; The user's saved settings live in a "data" folder inside the install folder.
; It isn't part of this list, so installing or updating never touches it.
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Schoology Grades"; Filename: "{app}\SchoologyGrades.exe"
Name: "{group}\Schoology Setup"; Filename: "{app}\SchoologySetup.exe"
Name: "{autodesktop}\Schoology Grades"; Filename: "{app}\SchoologyGrades.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked

[Run]
; The last page of the installer offers the next sensible step: a first install
; goes to Setup (which then offers to check the grades), an update goes straight
; to checking the grades.
Filename: "{app}\SchoologySetup.exe"; Description: "Choose your classes (Setup)"; Flags: nowait postinstall skipifsilent; Check: not HasSettings
Filename: "{app}\SchoologyGrades.exe"; Description: "Check my grades now"; Flags: nowait postinstall skipifsilent; Check: HasSettings

[Code]
// True if this computer already has saved classes (a previous install).
function HasSettings: Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\data\config.json'));
end;

// After uninstalling, offer to delete the saved login and settings too.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{app}\data');
    if DirExists(DataDir) and (not UninstallSilent) then
    begin
      if MsgBox('Also delete your saved login and settings?' + #13#10 + #13#10 +
                'Choose No to keep them in case you install again later.',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
    end;
    RemoveDir(ExpandConstant('{app}'));
  end;
end;
