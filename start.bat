@echo off
rem ============================================================================
rem  netclip launcher -- just double-click this file.
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
rem  All real logic and all Chinese messages live in start.ps1, which PowerShell
rem  reads as UTF-8 (the file carries a BOM).
rem
rem  The final "pause" only happens when there is a real console to read from.
rem  Otherwise a piped/redirected run would hang forever waiting for a key.
rem ============================================================================

setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%start.ps1"
set "CODE=%ERRORLEVEL%"

if not "%CODE%"=="0" (
    echo.
    echo [netclip launcher exited with code %CODE%]
)

rem Ask PowerShell whether stdin is an interactive console.
for /f %%I in ('powershell.exe -NoProfile -Command "if ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) { 'yes' } else { 'no' }"') do set "INTERACTIVE=%%I"

if /i "%INTERACTIVE%"=="yes" (
    echo.
    pause
)

endlocal
exit /b %CODE%
