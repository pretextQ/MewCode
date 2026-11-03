@echo off
powershell.exe -NoLogo -NoProfile -File "%~dp0scripts\run_tui.ps1" %*
exit /b %errorlevel%
