' ============================================================================
'  netclip -- windowless launcher, meant to be started by Task Scheduler.
'
'  Why a .vbs instead of a .bat or a .ps1:
'    * start.bat pops a console window for a moment, and
'      `powershell.exe -WindowStyle Hidden` still flashes one. Only
'      wscript.exe can launch a child with window style 0 and show nothing.
'
'  Why this file is ASCII-ONLY:
'    A BOM-less .vbs is read by wscript.exe using the system ANSI code page
'    (GBK/936 on Chinese Windows), so non-ASCII here would be mangled -- the
'    same trap the .ps1 files avoid with a UTF-8 BOM. A .vbs has no such
'    convention, so English it is. All Chinese text lives in start.ps1.
'
'  ---------------------------------------------------------------------------
'  Task Scheduler setup
'  ---------------------------------------------------------------------------
'    General   : Run only when user is logged on
'                Run with highest privileges            <- needed for UIPI
'    Triggers  : At log on   (a 30 second delay is a good idea)
'    Actions   : Start a program
'                  Program   : wscript.exe
'                  Arguments : "C:\path\to\netclip\netclip_task.vbs"
'                  Start in  : C:\path\to\netclip
'
'    Do NOT use "Run whether user is logged on or not". netclip needs an
'    interactive desktop session: the low-level hooks, SendInput and the
'    clipboard all fail silently in session 0.
'
'  What it does: start.ps1 -Run stops any previous instance (it also finds
'  instances running as administrator, which a non-elevated console cannot
'  even see), then launches `pythonw.exe -m netclip` in the background.
'  Nothing is printed, nothing is asked.
' ============================================================================

Option Explicit

Dim fso, sh, root, cmd, rc

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = root

' Second argument 0 = hidden window; third argument True = wait for it to exit.
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & root & "\start.ps1"" -Run"
rc = sh.Run(cmd, 0, True)

If rc <> 0 Then
    ' Only reached when the launcher actually failed -- a plain successful start
    ' is completely silent. Say something, otherwise the machine just sits there
    ' with no mouse sharing and no explanation.
    MsgBox "netclip failed to start (exit code " & rc & ")." & vbCrLf & vbCrLf & _
           "Run start.bat in the folder below and pick option 2 to see the real" & vbCrLf & _
           "error. Log file:" & vbCrLf & vbCrLf & _
           root & "\netclip.log", 16, "netclip"
End If
