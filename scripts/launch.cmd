@echo off
setlocal
if not defined CODEX_ROUTER_RUNTIME set "CODEX_ROUTER_RUNTIME=%LOCALAPPDATA%\CodexRouter\runtimes\1.2.0"
if not exist "%CODEX_ROUTER_RUNTIME%\Scripts\python.exe" (
  echo Model Router runtime missing. Run: py -3.13 scripts\install.py 1>&2
  exit /b 1
)
"%CODEX_ROUTER_RUNTIME%\Scripts\python.exe" -X utf8 -u "%~dp0..\main.py" mcp
exit /b %ERRORLEVEL%
