; Role Radar's Windows installer, built by scripts/package_windows.sh (makensis) from the app it stages.
;
; It installs for the signed-in user only, so it never asks for an administrator: the app goes in
; %LOCALAPPDATA%\Programs\<name>, with a Start menu shortcut (and one on the desktop, the first time),
; and in Settings > Apps, to remove it. Then it opens the app.
; An update (WinSparkle runs it silently, with /S) asks the running app and its checker to quit first,
; replaces the app whole, and opens the new version. Settings, job history and alert settings live
; elsewhere (%LOCALAPPDATA%\<name>, Credential Manager), so an update keeps them; removing the app asks
; whether they go too.
;
; From package_windows.sh: NAME ("Role Radar", or "Role Radar Dev": its files' folder and Credential
; Manager service too), VERSION, KEY (its Settings > Apps entry), SOURCE (the staged app), ICON, OUTFILE.

Unicode true
ManifestDPIAware true
!include "MUI2.nsh"
!include "FileFunc.nsh"

!define EXE "RoleRadar.exe"
!define UNINSTALL "Software\Microsoft\Windows\CurrentVersion\Uninstall\${KEY}"
!define RUN "Software\Microsoft\Windows\CurrentVersion\Run"
!define CREDENTIALS "EMAIL_TO EMAIL_FROM SMTP_HOST SMTP_PORT SMTP_SECURITY SMTP_USERNAME SMTP_PASSWORD DISCORD_WEBHOOK_URL"

Name "${NAME}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\${NAME}"
RequestExecutionLevel user
SetCompressor /SOLID lzma
BrandingText "${NAME} ${VERSION}"
VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "${NAME}"
VIAddVersionKey "FileDescription" "${NAME} Setup"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "CompanyName" "Role Radar"
VIAddVersionKey "LegalCopyright" "MIT License"

!define MUI_ICON "${ICON}"
!define MUI_UNICON "${ICON}"
!define MUI_FINISHPAGE_RUN "$INSTDIR\${EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "Open ${NAME}"
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Var Updating  ; an earlier version is installed

; Ask the running app to quit, and its checker to finish the companies in flight (up to 90 seconds);
; then end any program still running from this app's folder (the app, its checker), and only those.
!macro StopRunning
  IfFileExists "$INSTDIR\${EXE}" 0 +2
    nsExec::Exec '"$INSTDIR\${EXE}" --quit'
  System::Call 'Kernel32::SetEnvironmentVariable(t "RR_FOLDER", t "$INSTDIR\")'
  nsExec::Exec `powershell -NoProfile -NonInteractive -Command "Get-Process | Where-Object { $$_.Path -and $$_.Path.StartsWith($$env:RR_FOLDER, 'OrdinalIgnoreCase') } | Stop-Process -Force"`
  Sleep 500
!macroend

Section "Install"
  StrCpy $Updating 0
  IfFileExists "$INSTDIR\${EXE}" 0 +2
    StrCpy $Updating 1
  !insertmacro StopRunning
  RMDir /r "$INSTDIR"  ; the last version goes whole, so none of its files is left behind
  SetOutPath "$INSTDIR"
  File /r "${SOURCE}\*.*"
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  CreateShortcut "$SMPROGRAMS\${NAME}.lnk" "$INSTDIR\${EXE}"
  StrCmp $Updating 1 +2
    CreateShortcut "$DESKTOP\${NAME}.lnk" "$INSTDIR\${EXE}"

  WriteRegStr HKCU "${UNINSTALL}" "DisplayName" "${NAME}"
  WriteRegStr HKCU "${UNINSTALL}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTALL}" "Publisher" "Role Radar"
  WriteRegStr HKCU "${UNINSTALL}" "DisplayIcon" "$INSTDIR\${EXE}"
  WriteRegStr HKCU "${UNINSTALL}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL}" "URLInfoAbout" "https://github.com/anupreetsingh/Role-Radar"
  WriteRegStr HKCU "${UNINSTALL}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINSTALL}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINSTALL}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  WriteRegDWORD HKCU "${UNINSTALL}" "EstimatedSize" $0

  ; An update runs silently (/S), with no finish page to open it from: open the new version now.
  IfSilent 0 +2
    Exec '"$INSTDIR\${EXE}"'
SectionEnd

Section "Uninstall"
  !insertmacro StopRunning
  DeleteRegValue HKCU "${RUN}" "${NAME}"
  Delete "$SMPROGRAMS\${NAME}.lnk"
  Delete "$DESKTOP\${NAME}.lnk"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "${UNINSTALL}"
  DeleteRegKey HKCU "Software\Role Radar\${NAME}"  ; WinSparkle's settings

  IfSilent done
  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 \
    "Also delete your ${NAME} settings, job history and alert settings?$\n$\nKeep them to pick up where you left off if you install it again." \
    IDNO done
  RMDir /r "$LOCALAPPDATA\${NAME}"
  ; Its alert settings in Credential Manager ("<name>/EMAIL_TO" and the rest).
  StrCpy $0 "${CREDENTIALS} "
  next:
    StrCmp $0 "" done
    StrCpy $1 0
    find_space:
      StrCpy $2 $0 1 $1
      StrCmp $2 " " found
      IntOp $1 $1 + 1
      Goto find_space
    found:
      StrCpy $2 $0 $1
      IntOp $1 $1 + 1
      StrCpy $0 $0 "" $1
      nsExec::Exec 'cmdkey /delete:"${NAME}/$2"'
      Goto next
  done:
SectionEnd
