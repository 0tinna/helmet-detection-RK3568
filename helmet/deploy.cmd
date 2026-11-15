@echo off
rem Checked PowerShell deployment; optional board and isolated remote root.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1" %*
exit /b %ERRORLEVEL%
