@echo off
setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "PROJECT_ROOT=%%~fI"
set "VENV_DIR=%PROJECT_ROOT%\.venv"

echo [BOOT] Preparing LeisureLLM from "%PROJECT_ROOT%"

REM Check if venv exists
if not exist "%VENV_DIR%\Scripts\python.exe" (
	echo [ERROR] Virtual environment not found at "%VENV_DIR%"
	echo        Create it with: python -m venv .venv
	echo        Then install requirements: .venv\Scripts\pip install -r LeisureLLM\requirements.txt
	goto :fail
)

call "%VENV_DIR%\Scripts\activate.bat" || goto :fail

echo [BOOT] Checking dependencies...
python -m pip install -r "%SCRIPT_DIR%requirements.txt" -q || goto :fail

REM Load environment variables from LeisureLLM\.env if present
REM Skip lines starting with # and handle = in values properly
if exist "%SCRIPT_DIR%.env" (
	echo [BOOT] Loading environment from .env...
	for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%SCRIPT_DIR%.env") do (
		if not "%%A"=="" set "%%A=%%B"
	)
)

if "%TAVILY_API_KEY%"=="" (
	echo [WARN] TAVILY_API_KEY is not set for this session.
	echo        Set it in PowerShell with: ^$env:TAVILY_API_KEY="tvly-your-key"
	echo        Continuing without Tavily-powered web search.
)

if not exist "%PROJECT_ROOT%\config.py" (
	echo [ERROR] config.py not found in "%PROJECT_ROOT%".
	echo        This file is required. Check your repository is intact.
	goto :fail
)

set "PYTHONPATH=%PROJECT_ROOT%"

REM Default database location: keep it alongside LeisureLLM scripts/migrations.
REM This avoids accidentally creating an empty DB in the repo root.
if "%DATABASE_PATH%"=="" set "DATABASE_PATH=%SCRIPT_DIR%assistant.db"
pushd "%PROJECT_ROOT%" >nul || goto :fail
echo [BOOT] Launching LeisureLLM bot...
python LeisureLLM\leisureLLM.py
set EXITCODE=%ERRORLEVEL%
popd >nul
exit /b %EXITCODE%

:fail
echo [ERROR] StartBot encountered a problem. See messages above.
exit /b 1
