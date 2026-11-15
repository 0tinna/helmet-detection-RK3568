@echo off
setlocal
rem ---------------------------------------------------------------
rem  RK3568 serial entry point. Base command for the Agent.
rem
rem  Usage:
rem    rk.cmd "uname -a"
rem    rk.cmd "ls -l /dev/video*"
rem
rem  Env overrides:
rem    RK_PORT     default COM6
rem    RK_BAUD     default 1500000
rem    RK_TIMEOUT  default 20   (seconds)
rem
rem  Use single quotes inside the command to avoid quoting issues.
rem  Exit code = board command exit code, 3 = timeout, 2 = port busy.
rem ---------------------------------------------------------------
if "%~1"=="" (
  echo usage: rk.cmd "command"
  echo   e.g. rk.cmd "uname -a"
  exit /b 1
)
if not defined RK_PORT set RK_PORT=COM6
if not defined RK_BAUD set RK_BAUD=1500000
if not defined RK_TIMEOUT set RK_TIMEOUT=20

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0serial_console.ps1" -Port %RK_PORT% -Baud %RK_BAUD% -Cmd "%~1" -Timeout %RK_TIMEOUT%
exit /b %ERRORLEVEL%
