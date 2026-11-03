; Per-user installer for DWMfix 1.3.0.
; Does not write the Run key. "Start with Windows" stays inside the program.
; Same AppId as 1.2.8, so this upgrade replaces that install.
; Paths are relative to this file. Build the exe first (dist\DWMfix).

[Setup]
AppId={{8F4C2A17-6B0E-4D55-9A31-7C2E91B4D128}
AppName=DWMfix
AppVersion=1.3.0
AppVerName=DWMfix 1.3.0
AppPublisher=PixelCraft
DefaultDirName={localappdata}\Programs\DWMfix
DefaultGroupName=DWMfix
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
UsedUserAreasWarning=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
CloseApplicationsFilter=DWMfix.exe
RestartApplications=no
OutputDir=release
OutputBaseFilename=DWMfix-Setup-1.3.0
SetupIconFile=dwmfix.ico
UninstallDisplayIcon={app}\DWMfix.exe
UninstallDisplayName=DWMfix
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
VersionInfoVersion=1.3.0.0
VersionInfoCompany=PixelCraft
VersionInfoDescription=DWMfix installer
VersionInfoProductName=DWMfix
VersionInfoProductVersion=1.3.0.0
MinVersion=10.0

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "dist\DWMfix\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\DWMfix"; Filename: "{app}\DWMfix.exe"
Name: "{autodesktop}\DWMfix"; Filename: "{app}\DWMfix.exe"; Tasks: desktopicon

; Also runs after a silent install, so an update started from the app ends with the app running again.
[Run]
Filename: "{app}\DWMfix.exe"; Description: "Launch DWMfix"; Flags: nowait postinstall
