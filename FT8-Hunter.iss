; Inno Setup 6 script for the Windows installer of FT8 Hunter. CI builds it
; from the packaged FT8-Hunter.exe (see .github/workflows/build.yml):
;
;   ISCC /DAppVersion=<version> /DSrcDir=<dist> /DOutDir=<dir> FT8-Hunter.iss
;
; Settings and logs live in %APPDATA%\FT8-Hunter and stay on uninstall.
; The hunter updates itself by running a newer copy of this installer with
; /SILENT /UPDATE=1 once you say yes: it closes the running hunter and
; starts the new one when it's done.
;
; WS (the ws-hunter build) comes along in {app}\<WsDir>, a folder named after
; its build (/DWsDir=ws-hunter-<tag>). Its files are copied only when they
; aren't there yet, so an update with the same WS never touches (or closes) a
; running WS; a new WS build installs into a new folder.

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#ifndef SrcDir
  #define SrcDir "dist"
#endif
#ifndef OutDir
  #define OutDir "."
#endif
#ifndef WsDir
  #define WsDir "ws-hunter"
#endif

[Setup]
AppId={{3C9A6E14-7B2F-4D58-9E0C-A41F6B8D2C75}
AppName=FT8 Hunter
AppVersion={#AppVersion}
AppVerName=FT8 Hunter {#AppVersion}
AppPublisher=TheBengineer
AppPublisherURL=https://github.com/TheBengineer/FT8-Hunter
AppUpdatesURL=https://github.com/TheBengineer/FT8-Hunter/releases
DefaultDirName={autopf}\FT8 Hunter
DefaultGroupName=FT8 Hunter
DisableProgramGroupPage=yes
; per user by default (no UAC prompt, and updates need none either); the first page offers all users
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\FT8-Hunter.exe
; an update closes the running hunter (it has already turned WS's TX off)
CloseApplications=force
RestartApplications=no
OutputDir={#OutDir}
OutputBaseFilename=FT8-Hunter-windows-x64-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "startup"; Description: "Start FT8 Hunter when I sign in to Windows"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SrcDir}\FT8-Hunter.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SrcDir}\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SrcDir}\ws-hunter\*"; DestDir: "{app}\{#WsDir}"; Flags: onlyifdoesntexist recursesubdirs createallsubdirs

[Icons]
Name: "{group}\FT8 Hunter"; Filename: "{app}\FT8-Hunter.exe"; WorkingDir: "{app}"
Name: "{group}\WS (ws-hunter)"; Filename: "{app}\{#WsDir}\bin\ws.exe"; WorkingDir: "{app}\{#WsDir}\bin"
Name: "{group}\README"; Filename: "{app}\README.md"
Name: "{group}\{cm:UninstallProgram,FT8 Hunter}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\FT8 Hunter"; Filename: "{app}\FT8-Hunter.exe"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{userstartup}\FT8 Hunter"; Filename: "{app}\FT8-Hunter.exe"; WorkingDir: "{app}"; Tasks: startup

[Run]
Filename: "{app}\FT8-Hunter.exe"; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,FT8 Hunter}"; Flags: nowait postinstall skipifsilent
; an update the hunter started: start the new one again
Filename: "{app}\FT8-Hunter.exe"; WorkingDir: "{app}"; Flags: nowait; Check: IsUpdate

[Code]
function IsUpdate: Boolean;
begin
  Result := ExpandConstant('{param:UPDATE|0}') = '1';
end;

function SetEnvironmentVariable(Name: String; Value: String): BOOL;
  external 'SetEnvironmentVariableW@kernel32.dll stdcall';

function InitializeSetup: Boolean;
begin
  // A hunter that started this update passed on its PyInstaller variables (up
  // to 1.1.3 it didn't clear them): the hunter started below would look for
  // Python in the old one's unpacked folder ("Failed to load Python DLL"):
  // it runs the same exe path, so it takes them for its own. Ask for a reset,
  // and empty them too (an empty _PYI_ARCHIVE_FILE alone forces one).
  SetEnvironmentVariable('PYINSTALLER_RESET_ENVIRONMENT', '1');
  SetEnvironmentVariable('_PYI_ARCHIVE_FILE', '');
  SetEnvironmentVariable('_PYI_APPLICATION_HOME_DIR', '');
  SetEnvironmentVariable('_PYI_PARENT_PROCESS_LEVEL', '');
  SetEnvironmentVariable('_PYI_SPLASH_IPC', '');
  Result := True;
end;
