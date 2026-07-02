' ============================================================
'  BACKGROUND START - runs the monitor hidden (no window).
'  Double-click to start. It keeps running after you close
'  all windows.
'
'  IMPORTANT: run setup.bat once before using this.
'
'  To STOP: open Task Manager (Ctrl+Shift+Esc), find
'  "python.exe" and end the task (or run stop_monitor.bat).
' ============================================================

Dim fso, shell, here
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = here

' 0 = hidden window, False = do not wait
shell.Run "run_monitor.bat", 0, False

MsgBox "Gifts monitor started in background." & vbCrLf & vbCrLf & _
       "To stop: Task Manager -> python.exe -> End task.", _
       64, "Gifts Monitor"
