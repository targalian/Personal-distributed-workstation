!macro NSIS_HOOK_POSTINSTALL
  nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="QuickLAN Discovery UDP 45454" program="$INSTDIR\quicklan.exe"'
  nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="QuickLAN Transfer TCP 45455" program="$INSTDIR\quicklan.exe"'
  nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="QuickLAN Manifest HTTP 45457" program="$INSTDIR\quicklan.exe"'
  nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="QuickLAN Transfer TCP 45455-45474" program="$INSTDIR\quicklan.exe"'
  nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="QuickLAN Manifest HTTP 45457-45476" program="$INSTDIR\quicklan.exe"'
  nsExec::ExecToLog 'netsh advfirewall firewall add rule name="QuickLAN Discovery UDP 45454" dir=in action=allow program="$INSTDIR\quicklan.exe" enable=yes protocol=UDP localport=45454 profile=any'
  nsExec::ExecToLog 'netsh advfirewall firewall add rule name="QuickLAN Transfer TCP 45455-45474" dir=in action=allow program="$INSTDIR\quicklan.exe" enable=yes protocol=TCP localport=45455-45474 profile=any'
  nsExec::ExecToLog 'netsh advfirewall firewall add rule name="QuickLAN Manifest HTTP 45457-45476" dir=in action=allow program="$INSTDIR\quicklan.exe" enable=yes protocol=TCP localport=45457-45476 profile=any'
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="QuickLAN Discovery UDP 45454" program="$INSTDIR\quicklan.exe"'
  nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="QuickLAN Transfer TCP 45455" program="$INSTDIR\quicklan.exe"'
  nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="QuickLAN Manifest HTTP 45457" program="$INSTDIR\quicklan.exe"'
  nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="QuickLAN Transfer TCP 45455-45474" program="$INSTDIR\quicklan.exe"'
  nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="QuickLAN Manifest HTTP 45457-45476" program="$INSTDIR\quicklan.exe"'
!macroend
