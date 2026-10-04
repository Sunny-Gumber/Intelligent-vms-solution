Unicode True
RequestExecutionLevel admin
SetCompressor /SOLID lzma
CRCCheck on
XPStyle on

!include "MUI2.nsh"
!include "nsDialogs.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"

!ifndef StageRoot
  !error "StageRoot is required"
!endif
!ifndef OutputRoot
  !error "OutputRoot is required"
!endif
!ifndef ProductVersion
  !define ProductVersion "0.0.0"
!endif
!ifndef InstallerVersion
  !define InstallerVersion "0.0.0.0"
!endif
!ifndef BuildCommit
  !define BuildCommit "unknown"
!endif

Name "Intelligent VMS Field-Test Setup ${ProductVersion}"
OutFile "${OutputRoot}\IntelligentVMS-FieldTest-Setup-x64-${ProductVersion}.exe"
InstallDir "$PROGRAMFILES64\Intelligent VMS"
InstallDirRegKey HKLM "Software\IntelligentVMS" "InstallDir"
BrandingText "Intelligent VMS · Field-Test Installer · unsigned"
VIProductVersion "${InstallerVersion}"
VIAddVersionKey "ProductName" "Intelligent VMS Field-Test Setup"
VIAddVersionKey "ProductVersion" "${ProductVersion}"
VIAddVersionKey "FileVersion" "${InstallerVersion}"
VIAddVersionKey "Comments" "Release Candidate / External Qualification Pending"

Var Mode
Var RecordingRoot
Var Action
Var RadioServer
Var RadioClient
Var RadioBoth
Var RecordingEdit

!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
Page custom ModePage ModePageLeave
Page custom RecordingPage RecordingPageLeave
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  StrCpy $Mode "Both"
  StrCpy $Action "Install"
  StrCpy $RecordingRoot "$SYSDRIVE\IntelligentVMS-Recordings"
  ${GetParameters} $0
  ClearErrors
  ${GetOptions} $0 "/MODE=" $1
  ${IfNot} ${Errors}
    ${If} $1 == "server"
      StrCpy $Mode "Server"
    ${ElseIf} $1 == "client"
      StrCpy $Mode "Client"
    ${ElseIf} $1 == "both"
      StrCpy $Mode "Both"
    ${EndIf}
  ${EndIf}
  ClearErrors
  ${GetOptions} $0 "/ACTION=" $1
  ${IfNot} ${Errors}
    ${If} $1 == "upgrade"
      StrCpy $Action "Upgrade"
    ${ElseIf} $1 == "repair"
      StrCpy $Action "Repair"
    ${ElseIf} $1 == "install"
      StrCpy $Action "Install"
    ${EndIf}
  ${EndIf}
  ClearErrors
  ${GetOptions} $0 "/RECORDINGROOT=" $1
  ${IfNot} ${Errors}
    StrCpy $RecordingRoot $1
  ${EndIf}
FunctionEnd

Function ModePage
  nsDialogs::Create 1018
  Pop $0
  ${If} $0 == error
    Abort
  ${EndIf}
  ${NSD_CreateLabel} 0 0 100% 24u "Choose what this field-test setup installs. Server and client remain separate components."
  Pop $0
  ${NSD_CreateRadioButton} 0 34u 100% 12u "Server"
  Pop $RadioServer
  ${NSD_CreateRadioButton} 0 56u 100% 12u "Client"
  Pop $RadioClient
  ${NSD_CreateRadioButton} 0 78u 100% 12u "Both (Server first, then Client)"
  Pop $RadioBoth
  ${If} $Mode == "Server"
    ${NSD_Check} $RadioServer
  ${ElseIf} $Mode == "Client"
    ${NSD_Check} $RadioClient
  ${Else}
    ${NSD_Check} $RadioBoth
  ${EndIf}
  ${NSD_CreateLabel} 0 108u 100% 42u "Unsigned field-test software. Windows 10/11 and hardware/device qualification are not claimed."
  Pop $0
  nsDialogs::Show
FunctionEnd

Function ModePageLeave
  ${NSD_GetState} $RadioServer $0
  ${If} $0 == ${BST_CHECKED}
    StrCpy $Mode "Server"
    Return
  ${EndIf}
  ${NSD_GetState} $RadioClient $0
  ${If} $0 == ${BST_CHECKED}
    StrCpy $Mode "Client"
    Return
  ${EndIf}
  StrCpy $Mode "Both"
FunctionEnd

Function RecordingPage
  ${If} $Mode == "Client"
    Abort
  ${EndIf}
  nsDialogs::Create 1018
  Pop $0
  ${NSD_CreateLabel} 0 0 100% 24u "Recording storage (local path only; ordinary uninstall/upgrade preserves this data):"
  Pop $0
  ${NSD_CreateText} 0 34u 100% 14u "$RecordingRoot"
  Pop $RecordingEdit
  ${NSD_CreateLabel} 0 62u 100% 48u "Preflight validates x64 Windows, elevation, path safety, PostgreSQL 14+, Python 3.12 x64, required ports and service state before server changes."
  Pop $0
  nsDialogs::Show
FunctionEnd

Function RecordingPageLeave
  ${NSD_GetText} $RecordingEdit $RecordingRoot
  ${If} $RecordingRoot == ""
    MessageBox MB_ICONSTOP "Choose a recording path."
    Abort
  ${EndIf}
FunctionEnd

Section "Install"
  SetShellVarContext all

  ; Preflight from NSIS private extraction before Program Files/registry mutation.
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File "${StageRoot}\Invoke-Setup.ps1"
  nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "$PLUGINSDIR\Invoke-Setup.ps1" -Action "Preflight" -Components "$Mode" -RecordingRoot "$RecordingRoot" -PayloadRoot "$PLUGINSDIR" -Version "${InstallerVersion}" -Commit "${BuildCommit}" -Quiet'
  Pop $0
  ${If} $0 != 0
    DetailPrint "Preflight failed with exit code $0."
    Abort
  ${EndIf}

  SetOutPath "$INSTDIR\Setup"
  File "${StageRoot}\Invoke-Setup.ps1"
  File "${StageRoot}\product-version.json"
  SetOutPath "$INSTDIR\Setup\payload"
  File /r "${StageRoot}\payload\*.*"

  WriteRegStr HKLM "Software\IntelligentVMS" "InstallDir" "$INSTDIR"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\IntelligentVMS" "DisplayName" "Intelligent VMS Field-Test Setup"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\IntelligentVMS" "DisplayVersion" "${ProductVersion}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\IntelligentVMS" "Publisher" "Intelligent VMS"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\IntelligentVMS" "UninstallString" "$\"$INSTDIR\Uninstall.exe$\""
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  DetailPrint "Running bounded preflight and $Action for $Mode..."
  nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\Setup\Invoke-Setup.ps1" -Action "$Action" -Components "$Mode" -RecordingRoot "$RecordingRoot" -PayloadRoot "$INSTDIR\Setup\payload" -Version "${InstallerVersion}" -Commit "${BuildCommit}" -Quiet'
  Pop $0
  ${If} $0 != 0
    DetailPrint "Setup orchestration failed with exit code $0."
    IfFileExists "$PROGRAMDATA\IntelligentVMS\installer-state.json" managed_state_present
      RMDir /r "$INSTDIR\Client"
      RMDir /r "$INSTDIR\Setup"
      Delete "$INSTDIR\Uninstall.exe"
      DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\IntelligentVMS"
      DeleteRegKey HKLM "Software\IntelligentVMS"
      RMDir "$INSTDIR"
    managed_state_present:
    Abort
  ${EndIf}
SectionEnd

Section "Uninstall"
  SetShellVarContext all
  ${If} ${FileExists} "$INSTDIR\Setup\Invoke-Setup.ps1"
    nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\Setup\Invoke-Setup.ps1" -Action "Uninstall" -Components "Both" -PayloadRoot "$INSTDIR\Setup\payload" -Version "${InstallerVersion}" -Commit "${BuildCommit}" -Quiet'
    Pop $0
  ${EndIf}
  RMDir /r "$INSTDIR\Client"
  RMDir /r "$INSTDIR\Setup"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\IntelligentVMS"
  DeleteRegKey HKLM "Software\IntelligentVMS"
SectionEnd
