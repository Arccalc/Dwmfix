; Per-user installer for DWMfix 1.2.8.
; Does not write the Run key. "Start with Windows" stays inside the program.

[Setup]
AppId={{8F4C2A17-6B0E-4D55-9A31-7C2E91B4D128}
AppName=DWMfix
AppVersion=1.2.8
AppVerName=DWMfix 1.2.8
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
OutputDir=C:\Users\Ilyuha\Desktop\av-matrix2\release-1.2.8
OutputBaseFilename=DWMfix-Setup-1.2.8
SetupIconFile=C:\Users\Ilyuha\Desktop\av-matrix2\src\R128-release\dwmfix.ico
UninstallDisplayIcon={app}\DWMfix.exe
UninstallDisplayName=DWMfix
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
VersionInfoVersion=1.2.8.0
VersionInfoCompany=PixelCraft
VersionInfoDescription=DWMfix installer
VersionInfoProductName=DWMfix
VersionInfoProductVersion=1.2.8.0
MinVersion=10.0

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "C:\Users\Ilyuha\Desktop\av-matrix2\dist\R128-release\DWMfix\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\DWMfix"; Filename: "{app}\DWMfix.exe"
Name: "{autodesktop}\DWMfix"; Filename: "{app}\DWMfix.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\DWMfix.exe"; Description: "Launch DWMfix"; Flags: nowait postinstall skipifsilent
