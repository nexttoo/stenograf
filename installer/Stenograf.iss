; Стенограф — установщик (Inno Setup 6)
; Установка для текущего пользователя, без прав администратора.

#define AppName "Стенограф"
#define AppVersion "1.0.0"
#define AppPublisher "NexTToo Media"
#define AppExe "Stenograf.exe"
#define AppId "{{801A204D-0157-48A2-B472-54AA6121BFE3}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppCopyright=© 2026 NexTToo Media
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription=Локальная диктовка и расшифровка речи
DefaultDirName={localappdata}\Programs\Stenograf
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Stenograf-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
MinVersion=10.0.17763
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
AllowNoIcons=yes
CloseApplications=no
RestartApplications=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"; Flags: checkedonce
Name: "autostart"; Description: "Запускать при входе в Windows (папка «Автозагрузка»)"; GroupDescription: "Дополнительно:"; Flags: checkedonce

[Files]
Source: "payload\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{group}\Удалить {#AppName}"; Filename: "{uninstallexe}"
Name: "{group}\Инструкция"; Filename: "{app}\README.md"
Name: "{group}\Лицензии компонентов"; Filename: "{app}\docs\LICENSES.md"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: autostart

[Run]
Filename: "{app}\{#AppExe}"; Description: "Запустить {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\data\window-profile"
Type: filesandordirs; Name: "{app}\data\tmp"
Type: filesandordirs; Name: "{app}\data\uploads"

[Code]
procedure KillApp();
var
  ResultCode: Integer;
begin
  Exec('taskkill.exe', '/IM {#AppExe} /F', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(700);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  KillApp();
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    KillApp();
  end;

  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{app}\data');
    if DirExists(DataDir) then
    begin
      // при тихом удалении (автоматизация) историю не трогаем — только явный ответ
      if (not WizardSilent()) and
         (MsgBox('Удалить историю распознаваний и настройки?' + #13#10 + #13#10 +
                 DataDir + #13#10 + #13#10 +
                 'Нажмите «Нет», чтобы сохранить их для будущей установки.',
                 mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES) then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
