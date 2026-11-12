@echo off
setlocal enabledelayedexpansion

rem ---------------------------------------------------------------------
rem  Image Cleaner - build.bat
rem  Builds dist\ImageCleaner.exe via PyInstaller (see ImageCleaner.spec).
rem  Safe to double-click from Explorer or run from any shell/directory.
rem ---------------------------------------------------------------------

cd /d "%~dp0"

echo ============================================
echo  Image Cleaner - Build
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH. Install Python 3.11+ and try again.
    goto :fail
)

echo Checking for PyInstaller...
python -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo PyInstaller not found - installing it now...
    python -m pip install --quiet pyinstaller
    if errorlevel 1 (
        echo [ERROR] Failed to install PyInstaller. Try manually: pip install pyinstaller
        goto :fail
    )
)

if not exist "icon.ico" (
    echo [ERROR] icon.ico not found in %cd%. The build embeds this as the app icon.
    goto :fail
)

echo Cleaning previous build artifacts...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"

echo.
echo Building ImageCleaner.exe ...
echo.
python -m PyInstaller ImageCleaner.spec
if errorlevel 1 (
    echo.
    echo [ERROR] Build failed - see the PyInstaller output above for details.
    goto :fail
)

echo.
echo ============================================
echo  Build succeeded: dist\ImageCleaner.exe
echo ============================================
pause
exit /b 0

:fail
echo.
echo Build did not complete.
pause
exit /b 1
