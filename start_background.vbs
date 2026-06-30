' ============================================================
'  ФОНОВЫЙ ЗАПУСК мониторинга подарков (без окна).
'  Дважды кликните этот файл — скрипт запустится скрыто и
'  будет работать, даже если закрыть все окна.
'
'  ВАЖНО: сначала заполните данные в run_monitor.bat
'
'  Чтобы ОСТАНОВИТЬ: откройте Диспетчер задач (Ctrl+Shift+Esc),
'  найдите процесс "python.exe" и завершите его.
' ============================================================

Dim fso, shell, here
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = here

' 0 = скрытое окно, False = не ждать завершения
shell.Run "run_monitor.bat", 0, False

' Короткое уведомление, что запущено
MsgBox "Мониторинг подарков запущен в фоне." & vbCrLf & vbCrLf & _
       "Остановить: Диспетчер задач -> python.exe -> Снять задачу.", _
       64, "Gifts Monitor"
