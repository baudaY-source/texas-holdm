@echo off
setlocal
set "TAVERN_ROOT=%~dp0.."
set "TAVERN_PYTHON=%TAVERN_ROOT%\.venv\Scripts\python.exe"

if not exist "%TAVERN_PYTHON%" (
  echo [ERROR] Project venv Python was not found.
  echo Expected: "%TAVERN_PYTHON%"
  pause
  exit /b 2
)

"%TAVERN_PYTHON%" "%~dp0run_friends_alpha.py" --public-quick-tunnel %*
set "TAVERN_EXIT=%ERRORLEVEL%"
if not "%TAVERN_EXIT%"=="0" pause
exit /b %TAVERN_EXIT%
