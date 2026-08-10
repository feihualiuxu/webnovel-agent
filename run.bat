@echo off
setlocal
pushd "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m novel_agent %*
) else (
  python -m novel_agent %*
)
set CODE=%ERRORLEVEL%
popd
exit /b %CODE%
