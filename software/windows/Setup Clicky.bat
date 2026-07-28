@echo off
REM Clicky setup for Windows -- combined install/uninstall, mirroring
REM "Setup Clicky.command"'s macOS flow. Much simpler here on purpose:
REM Windows has no Gatekeeper-style quarantine flag to clear and no
REM per-app permission database (tccutil) to reset -- BLE/network
REM permission prompts, if Windows shows any, are handled by the OS
REM itself at first use, not something this script needs to touch.
REM Double-click this instead of using the command line: no CLI
REM knowledge needed either way.

setlocal enabledelayedexpansion

set "SRC=%~dp0Clicky"
set "DEST=%LOCALAPPDATA%\Clicky"
set "DATA_DIR=%USERPROFILE%\.clicky-pipeline"

if exist "%DEST%" (
    echo Clicky is already installed at %DEST%
    echo.
    echo What would you like to do?
    echo   1^) Reinstall ^(overwrite with this version^)
    echo   2^) Uninstall
    echo   3^) Cancel
    set /p choice="Choose 1, 2, or 3: "
    if "!choice!"=="1" (
        rd /s /q "%DEST%"
        call :install
    ) else if "!choice!"=="2" (
        call :uninstall
    ) else (
        echo Cancelled.
    )
) else (
    call :install
)

pause
exit /b

:install
echo Installing Clicky...
xcopy /e /i /q "%SRC%" "%DEST%" > nul
echo Done. Launching Clicky...
start "" "%DEST%\Clicky.exe"
exit /b

:uninstall
echo Stopping Clicky if it's running...
taskkill /f /im Clicky.exe > nul 2>&1
timeout /t 1 > nul

echo Removing %DEST%...
rd /s /q "%DEST%"

echo.
REM Deliberately NEVER touches %DATA_DIR% (recordings, settings.json --
REM including your API keys, Notion token, Google OAuth token). Wiping
REM that as part of "uninstall" is exactly the kind of surprise that
REM burns trust -- most software leaves your config behind so
REM reinstalling doesn't mean starting over from scratch. If you
REM genuinely want to delete it, do that yourself, deliberately:
REM   rd /s /q "%DATA_DIR%"
if exist "%DATA_DIR%" (
    echo Your recordings and settings ^(including saved API keys/tokens^) are kept at:
    echo   %DATA_DIR%
    echo Delete that folder yourself if you want a completely clean slate.
)
echo.
echo Clicky has been uninstalled.
exit /b
