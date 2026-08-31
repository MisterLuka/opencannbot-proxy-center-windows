@echo off
setlocal
if /I "%~1"=="/?" (
  echo Usage:
  echo   install.cmd [-RepoDir "path"] [-InstallDir "path"] [-Overwrite]
  echo.
  echo All arguments are optional:
  echo   -RepoDir     Use an existing OpenCANNBot repository instead of downloading one.
  echo   -InstallDir  Installation directory. Default: %%LOCALAPPDATA%%\OpenCANNBotProxyCenter
  echo   -Overwrite   Stop the old installation and overwrite its program files.
  echo.
  echo Examples:
  echo   install.cmd
  echo   install.cmd -InstallDir "C:\Tools\OpenCANNBotProxyCenter"
  echo   install.cmd -RepoDir "C:\Projects\opencannbot" -Overwrite
  exit /b 0
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
exit /b %ERRORLEVEL%
