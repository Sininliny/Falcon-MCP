@echo off
rem One-click install or update of Falcon MCP, Codex CLI, Claude Code, the agent skill and MCP configs.
rem Arguments are passed through, for example: install-windows.cmd -DryRun
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-windows.ps1" %*
set "FALCON_INSTALL_EXIT=%ERRORLEVEL%"
rem Keep the window open when started by double-click (no arguments).
if "%~1"=="" pause
exit /b %FALCON_INSTALL_EXIT%
