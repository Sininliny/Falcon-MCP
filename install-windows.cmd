@echo off
rem One-click install or update of Falcon MCP, its agent skill and MCP configs for the agent apps already installed.
rem It never installs Claude Desktop, Claude Code or Codex themselves.
rem Arguments are passed through, for example: install-windows.cmd -DryRun
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-windows.ps1" %*
set "FALCON_INSTALL_EXIT=%ERRORLEVEL%"
rem Keep the window open when started by double-click (no arguments).
if "%~1"=="" pause
exit /b %FALCON_INSTALL_EXIT%
