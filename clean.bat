@echo off
rem ============================================================================
rem  netclip cleanup -- double-click this file to delete the staging directory.
rem
rem  This file is deliberately ENGLISH/ASCII-ONLY.
rem
rem  Why: cmd.exe reads a .bat file byte-by-byte using the system ANSI code page
rem  (GBK/936 on Chinese Windows). A UTF-8 saved Chinese comment gets mangled --
rem  and worse, the mangled bytes can contain characters that cmd treats as
rem  command separators, so random fragments of the comment end up being
rem  executed as commands. Saving as GBK instead just moves the problem to
rem  another locale. ASCII has no such ambiguity.
rem
rem  All real logic and all Chinese messages live in clean.ps1, which PowerShell
rem  reads as UTF-8 (the file carries a BOM).
rem
rem  When double-clicked the window would vanish on error, so keep it open.
rem ============================================================================

setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%clean.ps1"
set "CODE=%ERRORLEVEL%"

if not "%CODE%"=="0" (
    echo.
    echo [netclip cleanup exited with code %CODE%]
)

echo.
pause

endlocal
exit /b %CODE%
