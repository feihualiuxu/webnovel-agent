@echo off
setlocal
pushd "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
)
".venv\Scripts\python.exe" -m pip install -U pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
set CODE=%ERRORLEVEL%
popd
exit /b %CODE%
