@echo off
REM Thin Windows shim that delegates to the cross-platform Python launcher.
REM .mcp.json invokes claude-dejavu-mcp.py directly via `python`; this file
REM exists for users who want to run the launcher by name from cmd/PowerShell.
python "%~dp0claude-dejavu-mcp.py" %*
