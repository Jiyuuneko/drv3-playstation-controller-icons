@echo off
setlocal EnableExtensions
title DRV3 PlayStation Controller Icons Manager

pushd "%~dp0" >nul 2>&1
if errorlevel 1 (
    echo ERROR: Could not open the mod directory.
    pause
    exit /b 1
)

python "%~dp0tools\manager.py" %*
set "LAST_RESULT=%ERRORLEVEL%"

if not "%LAST_RESULT%"=="0" (
    echo.
    echo Operation failed with exit code %LAST_RESULT%.
    echo Install Python 3.10 or newer and ensure it is available as python on PATH.
    if "%~1"=="" pause
)

popd
exit /b %LAST_RESULT%
