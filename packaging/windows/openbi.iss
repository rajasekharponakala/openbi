; Inno Setup script for the OpenBI Windows installer.
;   iscc /DAppVersion=0.1.0 /DSourceExe=dist\openbi.exe /DOutputDir=release packaging\windows\openbi.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceExe
  #define SourceExe "..\..\dist\openbi.exe"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\release"
#endif

[Setup]
AppId={{6B1D3E5A-8F0C-4C3B-9E62-2B7C0F4A91D7}
AppName=OpenBI
AppVersion={#AppVersion}
AppPublisher=OpenBI contributors
AppPublisherURL=https://github.com/rajasekharponakala/openbi
AppSupportURL=https://github.com/rajasekharponakala/openbi/issues
DefaultDirName={autopf}\OpenBI
DefaultGroupName=OpenBI
DisableProgramGroupPage=yes
LicenseFile=..\..\LICENSE
OutputDir={#OutputDir}
OutputBaseFilename=openbi-{#AppVersion}-windows-x86_64-setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequiredOverridesAllowed=commandline dialog
ChangesEnvironment=yes
UninstallDisplayIcon={app}\openbi.exe

[Tasks]
Name: "addtopath"; Description: "Add openbi to the PATH"; Flags: unchecked

[Files]
Source: "{#SourceExe}"; DestDir: "{app}"; DestName: "openbi.exe"; Flags: ignoreversion
Source: "..\..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"
Source: "..\..\README.md"; DestDir: "{app}"

[Icons]
Name: "{group}\OpenBI (demo server)"; Filename: "{app}\openbi.exe"; Parameters: "demo --home ""{localappdata}\OpenBI"""; Comment: "Start OpenBI with sample data on http://127.0.0.1:8000"
Name: "{group}\OpenBI server"; Filename: "{app}\openbi.exe"; Parameters: "serve --home ""{localappdata}\OpenBI"""
Name: "{group}\Open OpenBI in browser"; Filename: "http://127.0.0.1:8000/"
Name: "{group}\Uninstall OpenBI"; Filename: "{uninstallexe}"

[Registry]
Root: HKA; Subkey: "{code:EnvKey}"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Tasks: addtopath; Check: NeedsAddPath(ExpandConstant('{app}'))

[Code]
function EnvKey(Param: string): string;
begin
  if IsAdminInstallMode then
    Result := 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
  else
    Result := 'Environment';
end;

function NeedsAddPath(Dir: string): Boolean;
var
  Paths: string;
  RootKey: Integer;
begin
  if IsAdminInstallMode then RootKey := HKEY_LOCAL_MACHINE else RootKey := HKEY_CURRENT_USER;
  if not RegQueryStringValue(RootKey, EnvKey(''), 'Path', Paths) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Paths) + ';') = 0;
end;
