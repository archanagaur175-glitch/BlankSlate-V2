#include "download_defines.iss"
#define OllamaSrc "C:\Users\archa\AppData\Local\Programs\Ollama"

[Setup]
AppId={{5B21F8E4-6C1D-4A7E-9E3A-7C4D2A1F0B93}
AppName=BlankSlate
AppVersion=2.0.0
AppPublisher=BlankSlate
DefaultDirName={localappdata}\BlankSlate
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
OutputDir=.\output
OutputBaseFilename=BlankSlate-Setup-2.0.0
Compression=lzma2/normal
SolidCompression=no
DiskSpanning=yes
DiskSliceSize=2100000000
SetupIconFile=..\hud\src-tauri\icons\icon.ico
UninstallDisplayIcon={app}\BlankSlate.exe
UninstallDisplayName=BlankSlate
WizardStyle=modern
VersionInfoVersion=2.0.0.0
VersionInfoProductVersion=2.0.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "..\hud\src-tauri\target\release\blankslate-hud.exe"; DestDir: "{app}"; DestName: "BlankSlate.exe"; BeforeInstall: OnFileG1
Source: "..\daemon\dist\blankslate-daemon\*"; DestDir: "{app}\daemon"; Flags: recursesubdirs createallsubdirs; BeforeInstall: OnFileG2
Source: "{#OllamaSrc}\ollama.exe"; DestDir: "{app}\ollama"; BeforeInstall: OnFileG3
Source: "{#OllamaSrc}\ollama app.exe"; DestDir: "{app}\ollama"; BeforeInstall: OnFileG3
Source: "{#OllamaSrc}\app.ico"; DestDir: "{app}\ollama"; BeforeInstall: OnFileG3
Source: "{#OllamaSrc}\lib\*"; DestDir: "{app}\ollama\lib"; Flags: recursesubdirs createallsubdirs; BeforeInstall: OnFileG3
Source: ".\assets\THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"; BeforeInstall: OnFileG4
Source: ".\staging\seed_ollama_models\*"; DestDir: "{%USERPROFILE}\.ollama\models"; Flags: recursesubdirs createallsubdirs onlyifdoesntexist; BeforeInstall: OnFileG5
Source: ".\staging\seed_hf_hub\*"; DestDir: "{%USERPROFILE}\.cache\huggingface\hub"; Flags: recursesubdirs createallsubdirs onlyifdoesntexist; BeforeInstall: OnFileG5
Source: ".\assets\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; BeforeInstall: OnFileG4

[Icons]
Name: "{userprograms}\BlankSlate\BlankSlate"; Filename: "{app}\BlankSlate.exe"
Name: "{userprograms}\BlankSlate\Uninstall BlankSlate"; Filename: "{uninstallexe}"
Name: "{userdesktop}\BlankSlate"; Filename: "{app}\BlankSlate.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\BlankSlate.exe"; Description: "Launch BlankSlate"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
var
  ETALabel: TNewStaticText;
  DLPage: TDownloadWizardPage;
  G1C, G2C, G3C, G4C, G5C: Integer;
  LabelTick, InstLastTick, InstLastMB, InstSpeed, DlLastTick: Integer;
  DlTotBytes, DlLastBytes, DlSpeed: Int64;

function GetTickCount: Integer;
  external 'GetTickCount@kernel32.dll stdcall';

function SliceName(idx: Integer): String;
begin
  Result := '';
  #ifdef BIN1_NAME
  if idx = 1 then Result := '{#BIN1_NAME}';
  #endif
  #ifdef BIN2_NAME
  if idx = 2 then Result := '{#BIN2_NAME}';
  #endif
  #ifdef BIN3_NAME
  if idx = 3 then Result := '{#BIN3_NAME}';
  #endif
  #ifdef BIN4_NAME
  if idx = 4 then Result := '{#BIN4_NAME}';
  #endif
  #ifdef BIN5_NAME
  if idx = 5 then Result := '{#BIN5_NAME}';
  #endif
  #ifdef BIN6_NAME
  if idx = 6 then Result := '{#BIN6_NAME}';
  #endif
  #ifdef BIN7_NAME
  if idx = 7 then Result := '{#BIN7_NAME}';
  #endif
  #ifdef BIN8_NAME
  if idx = 8 then Result := '{#BIN8_NAME}';
  #endif
  #ifdef BIN9_NAME
  if idx = 9 then Result := '{#BIN9_NAME}';
  #endif
end;

function SliceSha(idx: Integer): String;
begin
  Result := '';
  #ifdef BIN1_SHA
  if idx = 1 then Result := '{#BIN1_SHA}';
  #endif
  #ifdef BIN2_SHA
  if idx = 2 then Result := '{#BIN2_SHA}';
  #endif
  #ifdef BIN3_SHA
  if idx = 3 then Result := '{#BIN3_SHA}';
  #endif
  #ifdef BIN4_SHA
  if idx = 4 then Result := '{#BIN4_SHA}';
  #endif
  #ifdef BIN5_SHA
  if idx = 5 then Result := '{#BIN5_SHA}';
  #endif
  #ifdef BIN6_SHA
  if idx = 6 then Result := '{#BIN6_SHA}';
  #endif
  #ifdef BIN7_SHA
  if idx = 7 then Result := '{#BIN7_SHA}';
  #endif
  #ifdef BIN8_SHA
  if idx = 8 then Result := '{#BIN8_SHA}';
  #endif
  #ifdef BIN9_SHA
  if idx = 9 then Result := '{#BIN9_SHA}';
  #endif
end;

function SliceBytes(idx: Integer): Int64;
begin
  Result := 0;
  #ifdef BIN1_BYTES
  if idx = 1 then Result := {#BIN1_BYTES};
  #endif
  #ifdef BIN2_BYTES
  if idx = 2 then Result := {#BIN2_BYTES};
  #endif
  #ifdef BIN3_BYTES
  if idx = 3 then Result := {#BIN3_BYTES};
  #endif
  #ifdef BIN4_BYTES
  if idx = 4 then Result := {#BIN4_BYTES};
  #endif
  #ifdef BIN5_BYTES
  if idx = 5 then Result := {#BIN5_BYTES};
  #endif
  #ifdef BIN6_BYTES
  if idx = 6 then Result := {#BIN6_BYTES};
  #endif
  #ifdef BIN7_BYTES
  if idx = 7 then Result := {#BIN7_BYTES};
  #endif
  #ifdef BIN8_BYTES
  if idx = 8 then Result := {#BIN8_BYTES};
  #endif
  #ifdef BIN9_BYTES
  if idx = 9 then Result := {#BIN9_BYTES};
  #endif
end;

function SliceOK(idx: Integer): Boolean;
var
  path: String;
  rec: TFindRec;
  sz: Int64;
begin
  Result := False;
  path := SliceName(idx);
  if path = '' then Exit;
  path := ExpandConstant('{src}\' + path);
  if not FileExists(path) then Exit;
  if FindFirst(path, rec) then begin
    sz := rec.SizeHigh;
    sz := sz shl 32;
    sz := sz + rec.SizeLow;
    FindClose(rec);
    Result := (sz = SliceBytes(idx));
  end;
end;

function AllSlicesPresent: Boolean;
var
  i: Integer;
begin
  Result := True;
  for i := 1 to {#BIN_COUNT} do
    if not SliceOK(i) then Result := False;
end;

function FmtMB(mb: Int64): String;
begin
  if mb >= 1024 then
    Result := IntToStr(mb div 1024) + '.' + IntToStr(((mb mod 1024) * 10) div 1024) + ' GB'
  else
    Result := IntToStr(mb) + ' MB';
end;

function FmtEta(sec: Int64): String;
begin
  if sec <= 0 then
    Result := 'estimating time remaining...'
  else if sec < 60 then
    Result := 'about ' + IntToStr(sec) + ' s remaining'
  else
    Result := 'about ' + IntToStr(sec div 60) + ' min ' + IntToStr(sec mod 60) + ' s remaining';
end;

function PctStr(done: Int64; tot: Int64): String;
var
  p: Int64;
begin
  if tot <= 0 then begin
    Result := '0';
    Exit;
  end;
  p := (done * 100) div tot;
  if p > 100 then p := 100;
  Result := IntToStr(p);
end;

procedure ShowLabel(txt: String; force: Boolean);
var
  now: Integer;
begin
  if ETALabel = nil then Exit;
  now := GetTickCount;
  if (not force) and (now - LabelTick < 250) then Exit;
  LabelTick := now;
  ETALabel.Caption := txt;
  ETALabel.Visible := True;
  ETALabel.BringToFront;
  Log(txt);
end;

function DlProgress(const Url, FileName: String; const Progress, ProgressMax: Int64): Boolean;
var
  base, done, sp, sec, mbDone, mbTot: Int64;
  i, now: Integer;
begin
  Result := True;
  base := 0;
  for i := 1 to {#BIN_COUNT} do begin
    if SliceName(i) = FileName then Break;
    base := base + SliceBytes(i);
  end;
  done := base + Progress;
  now := GetTickCount;
  if (now - DlLastTick) >= 300 then begin
    if done > DlLastBytes then begin
      sp := ((done - DlLastBytes) * 1000) div (now - DlLastTick);
      if DlSpeed > 0 then
        DlSpeed := (DlSpeed * 7 + sp * 3) div 10
      else
        DlSpeed := sp;
    end;
    DlLastTick := now;
    DlLastBytes := done;
  end;
  mbDone := done div 1048576;
  mbTot := DlTotBytes div 1048576;
  if DlSpeed > 0 then
    sec := (DlTotBytes - done) div DlSpeed
  else
    sec := 0;
  ShowLabel('Downloading  ' + FmtMB(mbDone) + ' of ' + FmtMB(mbTot) + '  (' + PctStr(done, DlTotBytes) + '%)  -  ' + FmtEta(sec), False);
end;

function MoveSlices: Boolean;
var
  i: Integer;
  nm, tmpPath, dstPath: String;
begin
  Result := True;
  for i := 1 to {#BIN_COUNT} do begin
    if SliceOK(i) then Continue;
    nm := SliceName(i);
    tmpPath := ExpandConstant('{tmp}\' + nm);
    dstPath := ExpandConstant('{src}\' + nm);
    if not FileExists(tmpPath) then begin
      Log('MoveSlices: missing in tmp: ' + nm);
      Result := False;
      Break;
    end;
    if not FileCopy(tmpPath, dstPath, False) then begin
      Log('MoveSlices: cannot write ' + dstPath);
      Result := False;
      Break;
    end;
    DeleteFile(tmpPath);
    if not SliceOK(i) then begin
      Log('MoveSlices: size mismatch after move: ' + nm);
      Result := False;
      Break;
    end;
  end;
  if Result then Log('All slices in place');
end;

procedure PlaceLabelOnWizard;
begin
  if ETALabel = nil then Exit;
  ETALabel.Parent := WizardForm;
  ETALabel.SetBounds(170, WizardForm.ClientHeight - 105, WizardForm.ClientWidth - 190, 55);
end;

function DoDownloadUI: Boolean;
var
  i, missing: Integer;
begin
  Result := True;
  if AllSlicesPresent then Exit;
  missing := 0;
  DlTotBytes := 0;
  for i := 1 to {#BIN_COUNT} do begin
    DlTotBytes := DlTotBytes + SliceBytes(i);
    if not SliceOK(i) then missing := missing + 1;
  end;
  Log('Download phase: ' + IntToStr(missing) + ' slice(s) missing');
  DlLastBytes := 0;
  DlSpeed := 0;
  DlLastTick := GetTickCount;
  DLPage.Clear;
  for i := 1 to {#BIN_COUNT} do
    if not SliceOK(i) then
      DLPage.Add('{#DL_BASE}' + '/' + SliceName(i), SliceName(i), SliceSha(i));
  PlaceLabelOnWizard;
  ShowLabel('Downloading  0 MB of ' + FmtMB(DlTotBytes div 1048576) + '...', True);
  DLPage.Show;
  try
    try
      DLPage.Download;
      Log('Download finished');
    except
      if DLPage.AbortedByUser then
        Log('Download aborted by user')
      else begin
        Log('Download error: ' + GetExceptionMessage);
        Result := False;
      end;
    end;
  finally
    DLPage.Hide;
    PlaceLabelOnWizard;
    if ETALabel <> nil then ETALabel.Visible := False;
  end;
  if Result then Result := MoveSlices;
  if not Result then
    SuppressibleMsgBox('Setup could not download the required files from:' #13#10 + '{#DL_BASE}' + #13#10 #13#10 + 'Check your internet connection and run Setup again, or place the .bin files in the same folder as the Setup program.', mbError, MB_OK, IDOK);
end;

function DoDownloadSilent: Boolean;
var
  i: Integer;
begin
  Result := True;
  DlTotBytes := 0;
  for i := 1 to {#BIN_COUNT} do
    DlTotBytes := DlTotBytes + SliceBytes(i);
  DlLastBytes := 0;
  DlSpeed := 0;
  DlLastTick := GetTickCount;
  try
    for i := 1 to {#BIN_COUNT} do
      if not SliceOK(i) then begin
        Log('Silent download: ' + SliceName(i));
        DownloadTemporaryFile('{#DL_BASE}' + '/' + SliceName(i), SliceName(i), SliceSha(i), @DlProgress);
      end;
  except
    Log('Silent download failed: ' + GetExceptionMessage);
    Result := False;
  end;
  if Result then Result := MoveSlices;
end;

function Partial(C: Integer; Files: Integer; MB: Integer): Integer;
begin
  if Files <= 0 then
    Result := 0
  else
    Result := (C * MB) div Files;
end;

procedure InstTick;
var
  doneMB, sp, sec, rem, now, deltaT: Integer;
begin
  if ETALabel = nil then Exit;
  doneMB := Partial(G1C, {#G1_FILES}, {#G1_MB}) + Partial(G2C, {#G2_FILES}, {#G2_MB}) + Partial(G3C, {#G3_FILES}, {#G3_MB}) + Partial(G4C, {#G4_FILES}, {#G4_MB}) + Partial(G5C, {#G5_FILES}, {#G5_MB});
  now := GetTickCount;
  deltaT := now - InstLastTick;
  if deltaT >= 500 then begin
    sp := ((doneMB - InstLastMB) * 1000) div deltaT;
    if sp > 0 then
      InstSpeed := (InstSpeed * 7 + sp * 3) div 10;
    InstLastTick := now;
    InstLastMB := doneMB;
  end;
  rem := {#TOT_MB} - doneMB;
  if InstSpeed > 0 then
    sec := rem div InstSpeed
  else
    sec := 0;
  ShowLabel('Installing  ' + FmtMB(doneMB) + ' of ' + FmtMB({#TOT_MB}) + '  (' + PctStr(doneMB, {#TOT_MB}) + '%)  -  ' + FmtEta(sec), False);
end;

procedure OnFileG1;
begin
  G1C := G1C + 1;
  InstTick;
end;

procedure OnFileG2;
begin
  G2C := G2C + 1;
  InstTick;
end;

procedure OnFileG3;
begin
  G3C := G3C + 1;
  InstTick;
end;

procedure OnFileG4;
begin
  G4C := G4C + 1;
  InstTick;
end;

procedure OnFileG5;
begin
  G5C := G5C + 1;
  InstTick;
end;

const
  WEBVIEW2_CLIENT_KEY = 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function WebView2PvOk(RootKey: Integer): Boolean;
var
  Pv: String;
begin
  Result := False;
  if RegQueryStringValue(RootKey, WEBVIEW2_CLIENT_KEY, 'pv', Pv) then
    Result := (Pv <> '') and (Pv <> '0.0.0.0');
end;

function WebView2Installed(): Boolean;
var
  FindRec: TFindRec;
  Dir: String;
begin
  Result := False;
  if WebView2PvOk(HKLM32) or WebView2PvOk(HKCU) then
  begin
    Result := True;
    exit;
  end;
  Dir := ExpandConstant('{pf32}\Microsoft\EdgeWebView\Application');
  if FindFirst(Dir + '\*', FindRec) then
  try
    repeat
      if ((FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0) and
        (FindRec.Name <> '.') and (FindRec.Name <> '..') then
      begin
        if FileExists(Dir + '\' + FindRec.Name + '\EBWebView\x64\EmbeddedBrowserWebView.dll') or
          FileExists(Dir + '\' + FindRec.Name + '\EmbeddedBrowserWebView.dll') then
        begin
          Result := True;
          break;
        end;
      end;
    until not FindNext(FindRec);
  finally
    FindClose(FindRec);
  end;
end;

procedure InstallWebView2IfNeeded();
var
  RC: Integer;
begin
  if WebView2Installed() then
    exit;
  if not Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'),
    '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, RC) then
    RC := -1;
  Sleep(5000);
  if not WebView2Installed() then
    MsgBox('The Microsoft WebView2 runtime could not be installed.' #13#10 #13#10 +
      'BlankSlate needs the WebView2 runtime and it could not be downloaded. ' +
      'An internet connection is required the first time this installer runs ' +
      'when WebView2 is missing. Please install "Microsoft Edge WebView2 Runtime" ' +
      'and run BlankSlate again.', mbInformation, MB_OK);
end;

procedure InitializeWizard;
begin
  DLPage := CreateDownloadPage(SetupMessage(msgWizardPreparing), SetupMessage(msgPreparingDesc), @DlProgress);
  DLPage.ShowBaseNameInsteadOfUrl := True;
  ETALabel := TNewStaticText.Create(WizardForm);
  ETALabel.Parent := WizardForm;
  ETALabel.SetBounds(170, WizardForm.ClientHeight - 105, WizardForm.ClientWidth - 190, 55);
  ETALabel.Caption := '';
  ETALabel.Visible := False;
  LabelTick := 0;
  InstLastMB := 0;
  InstSpeed := 0;
  Log('InitializeWizard ok');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  Log('NextButtonClick page=' + IntToStr(CurPageID));
  if CurPageID = wpReady then begin
    if not AllSlicesPresent then begin
      if DLPage = nil then
        Result := DoDownloadSilent
      else
        Result := DoDownloadUI;
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not AllSlicesPresent then begin
    Log('PrepareToInstall: slices still missing');
    if not DoDownloadSilent then
      Result := 'Setup could not download the required files from:' #13#10 + '{#DL_BASE}' + #13#10 #13#10 + 'Check your internet connection and run Setup again.';
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then begin
    InstLastTick := GetTickCount;
    InstLastMB := 0;
    InstSpeed := 0;
    PlaceLabelOnWizard;
    ShowLabel('Installing...', True);
  end;
  if CurStep = ssPostInstall then begin
    ShowLabel('Installing  100%', True);
    InstallWebView2IfNeeded();
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpFinished then
    if ETALabel <> nil then ETALabel.Visible := False;
end;

