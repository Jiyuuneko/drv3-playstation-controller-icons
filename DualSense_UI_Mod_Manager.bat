@echo off
setlocal EnableExtensions
title DRV3 PlayStation Controller Icons Manager

pushd "%~dp0" >nul 2>&1
if errorlevel 1 (
    echo ERROR: Could not open the mod directory.
    pause
    exit /b 1
)

set "INTERACTIVE=1"
set "LAST_RESULT=0"

if /i "%~1"=="dualsense" set "INTERACTIVE=0"& goto install_dualsense
if /i "%~1"=="dualshock4" set "INTERACTIVE=0"& goto install_dualshock4
if /i "%~1"=="verify-installed" set "INTERACTIVE=0"& goto verify_installed
if /i "%~1"=="uninstall" set "INTERACTIVE=0"& goto uninstall
if /i "%~1"=="verify-original" set "INTERACTIVE=0"& goto verify_original
if not "%~1"=="" goto usage_error

:menu
call :detect_language
cls
echo ============================================================
echo          DRV3 PlayStation Controller Icons Manager
echo ============================================================
echo.
echo Active game language: %ACTIVE_LANGUAGE%
echo.
echo   1. Install DualSense icons
echo      Icon-only Create and Options buttons
echo.
echo   2. Install DualShock 4 icons
echo      SHARE and OPTIONS buttons
echo.
echo   3. Verify the installed mod
echo   4. Uninstall and restore original files
echo   5. Verify original game files
echo   Q. Quit
echo.
echo The game must be closed before installing or uninstalling.
echo Only compact rollback entries are saved - no full CPK backup.
echo.
choice /C 12345Q /N /M "Choose an option: "
if errorlevel 6 goto finish
if errorlevel 5 goto verify_original
if errorlevel 4 goto uninstall
if errorlevel 3 goto verify_installed
if errorlevel 2 goto install_dualshock4
if errorlevel 1 goto install_dualsense
goto menu

:install_dualsense
call :run_script "install.ps1" "Installing the DualSense icon variant" -Variant DualSense
goto action_done

:install_dualshock4
call :run_script "install.ps1" "Installing the DualShock 4 icon variant" -Variant DualShock4
goto action_done

:verify_installed
call :run_script "verify.ps1" "Verifying the installed mod" -Installed
goto action_done

:uninstall
call :run_script "uninstall.ps1" "Restoring the original game files"
goto action_done

:verify_original
call :run_script "verify.ps1" "Verifying the original game files"
goto action_done

:run_script
call :detect_language
cls
echo ============================================================
echo %~2
echo ============================================================
echo.
echo Active game language: %ACTIVE_LANGUAGE%
echo.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0%~1" %~3 %~4
set "LAST_RESULT=%ERRORLEVEL%"
echo.
if "%LAST_RESULT%"=="0" (
    echo Operation completed successfully.
) else (
    echo Operation failed with exit code %LAST_RESULT%.
    echo Read the error above; no unsupported file should be patched.
)
exit /b %LAST_RESULT%

:detect_language
set "ACTIVE_LANGUAGE=unknown"
if exist "%~dp0..\language.txt" set /p "ACTIVE_LANGUAGE="<"%~dp0..\language.txt"
exit /b 0

:action_done
if "%INTERACTIVE%"=="0" goto finish
echo.
pause
goto menu

:usage_error
echo Usage:
echo   %~nx0
echo   %~nx0 dualsense
echo   %~nx0 dualshock4
echo   %~nx0 verify-installed
echo   %~nx0 uninstall
echo   %~nx0 verify-original
set "LAST_RESULT=2"

:finish
popd
exit /b %LAST_RESULT%
