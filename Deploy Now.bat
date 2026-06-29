@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   BookPoint - Deploy to Fly.io
echo   App: bookpoint-rac
echo ============================================
echo.

where flyctl >nul 2>nul
if errorlevel 1 (
    echo ERROR: flyctl is not installed or not on your PATH.
    echo Install it first - see DEPLOY.md, "Prerequisites" section.
    echo.
    pause
    exit /b 1
)

where git >nul 2>nul
if %ERRORLEVEL%==0 (
    if exist ".git" (
        echo Saving a local commit of your current changes...
        git add -A
        git commit -m "Deploy via Deploy Now.bat" >nul 2>nul
        echo.
    )
)

echo Uploading current folder and deploying to Fly.io...
echo This may take 2-4 minutes. Please wait...
echo.

flyctl deploy --app bookpoint-rac

if errorlevel 1 (
    echo.
    echo ============================================
    echo   DEPLOY FAILED - see the error above.
    echo ============================================
) else (
    echo.
    echo ============================================
    echo   DEPLOY SUCCEEDED - your changes are now live.
    echo ============================================
)

echo.
pause
