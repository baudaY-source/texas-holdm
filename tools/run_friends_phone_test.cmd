@echo off
setlocal
set "TAVERN_ROOT=%~dp0.."
set "TAVERN_PYTHON=%TAVERN_ROOT%\.venv\Scripts\python.exe"
set "TAVERN_CLOUDFLARED=%TAVERN_ROOT%\local_backups\tools\cloudflared-2026.7.3-windows-amd64.exe"

if not exist "%TAVERN_PYTHON%" (
  echo [ERROR] Project venv Python was not found.
  echo Expected: "%TAVERN_PYTHON%"
  pause
  exit /b 2
)

if exist "%TAVERN_CLOUDFLARED%" (
  "%TAVERN_PYTHON%" "%~dp0run_friends_alpha.py" --public-quick-tunnel --phone-verifies-public --auto-create-room --cloudflared "%TAVERN_CLOUDFLARED%" %*
) else (
  "%TAVERN_PYTHON%" "%~dp0run_friends_alpha.py" --public-quick-tunnel --phone-verifies-public --auto-create-room %*
)

set "TAVERN_EXIT=%ERRORLEVEL%"
if not "%TAVERN_EXIT%"=="0" pause
exit /b %TAVERN_EXIT%
