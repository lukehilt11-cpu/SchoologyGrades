@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem  build.bat            the programs next to "source" AND an installer in ..\releases
rem  build.bat force      rebuild even if nothing has changed
rem  build.bat dev        the programs only (skips the installer)
rem  build.bat run        when done, open the installer (or, with dev, the program) to try it

set "FORCE=0"
set "DEV=0"
set "RUN=0"
:args
if "%~1"=="" goto args_done
if /i "%~1"=="force" set "FORCE=1"
if /i "%~1"=="dev" set "DEV=1"
if /i "%~1"=="run" set "RUN=1"
shift
goto args
:args_done

set "VERSION=1.0"
if exist VERSION set /p VERSION=<VERSION
set "CACHE=%LOCALAPPDATA%\SchoologyGradesBuild"
set "WORK=%CACHE%\work"
set "LOG=%CACHE%\build.log"
for %%I in ("..") do set "PARENT=%%~fI"
for %%I in ("..\releases") do set "OUTDIR=%%~fI"
set "TARGET=%OUTDIR%\SchoologyGrades-%VERSION%-Setup.exe"
set "RESULT="
set "NOTE="
if not exist "%CACHE%" mkdir "%CACHE%"
type nul > "%LOG%"

rem ---- nothing changed since the last build?
if "%FORCE%"=="1" goto build
if "%DEV%"=="1" goto build
if not exist "%TARGET%" goto build
if not exist "%PARENT%\SchoologyGrades.exe" goto build
if not exist "%PARENT%\SchoologySetup.exe" goto build
powershell -NoProfile -Command "$t=(Get-Item -LiteralPath $env:TARGET).LastWriteTime; $n=Get-ChildItem -Recurse -File | Where-Object { $_.FullName -notmatch '__pycache__' -and $_.LastWriteTime -gt $t } | Select-Object -First 1; if ($n) { exit 1 } else { exit 0 }"
if errorlevel 1 goto build
echo Nothing has changed since the last build:
echo   %TARGET%
echo Use "build.bat force" to rebuild anyway.
goto launch

:build
echo Schoology Grade Checker build %VERSION%
where python >nul 2>&1
if errorlevel 1 goto nopython

rem ---- 1/6 build tools
python -c "import PyInstaller, selenium, PIL, numpy" >nul 2>&1
if errorlevel 1 goto install_tools
echo [1/6] Build tools ready
goto tools_done
:install_tools
echo [1/6] Installing build tools (first time only)...
python -m pip install --quiet --disable-pip-version-check pyinstaller "selenium>=4.6" pillow numpy >> "%LOG%" 2>&1
if errorlevel 1 goto fail
:tools_done

rem ---- 2/6 icons (only redrawn when missing or older than make_icons.py)
set "ICONS_OK=1"
for %%F in (grades.ico grades.icns grades_256.png grades_64.png setup.ico setup.icns setup_256.png wizard_large.bmp wizard_small.bmp dmg_background.png) do if not exist "icons\%%F" set "ICONS_OK=0"
if "%FORCE%"=="1" set "ICONS_OK=0"
if "%ICONS_OK%"=="0" goto draw_icons
powershell -NoProfile -Command "$s=(Get-Item 'make_icons.py').LastWriteTime; $o=Get-ChildItem 'icons' -File | Where-Object { $_.LastWriteTime -lt $s } | Select-Object -First 1; if ($o) { exit 1 } else { exit 0 }"
if errorlevel 1 goto draw_icons
echo [2/6] Icons up to date
goto icons_done
:draw_icons
echo [2/6] Drawing icons...
python make_icons.py >> "%LOG%" 2>&1
if errorlevel 1 goto fail
:icons_done
set "MISSING="
for %%F in (grades.ico grades_256.png grades_64.png setup.ico setup_256.png) do if not exist "icons\%%F" set "MISSING=%%F"
if defined MISSING goto icons_missing

rem ---- 3/6 build the programs
echo [3/6] Building programs (a minute or two)...
set "CLEAN="
if "%FORCE%"=="1" set "CLEAN=--clean"
python -m PyInstaller --noconfirm %CLEAN% --log-level WARN --distpath "%WORK%\dist" --workpath "%WORK%\build" grades.spec >> "%LOG%" 2>&1
if errorlevel 1 goto fail
if not exist "%WORK%\dist\SchoologyGrades\SchoologyGrades.exe" goto fail
if not exist "%WORK%\dist\SchoologyGrades\SchoologySetup.exe" goto fail
if not exist "%WORK%\dist\SchoologyGrades-portable.exe" goto fail
if not exist "%WORK%\dist\SchoologySetup-portable.exe" goto fail

rem ---- 4/6 the single-file programs, next to "source" (a data folder appears beside them when they run)
echo [4/6] Updating SchoologyGrades.exe and SchoologySetup.exe...
copy /y "%WORK%\dist\SchoologyGrades-portable.exe" "%PARENT%\SchoologyGrades.exe" >> "%LOG%" 2>&1
if errorlevel 1 goto locked
copy /y "%WORK%\dist\SchoologySetup-portable.exe" "%PARENT%\SchoologySetup.exe" >> "%LOG%" 2>&1
if errorlevel 1 goto locked

rem ---- 5/6 the installer
if "%DEV%"=="1" goto skip_installer
set "ISCC="
for %%P in ("%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe") do if exist %%P set "ISCC=%%~P"
if not defined ISCC for /f "delims=" %%P in ('where ISCC.exe 2^>nul') do set "ISCC=%%P"
if not exist "%OUTDIR%" mkdir "%OUTDIR%"
if defined ISCC goto make_installer
echo [5/6] Inno Setup not found, making a portable zip instead...
powershell -NoProfile -Command "Compress-Archive -Path (Join-Path $env:WORK 'dist\SchoologyGrades\*') -DestinationPath (Join-Path $env:OUTDIR ('SchoologyGrades-' + $env:VERSION + '-portable.zip')) -Force" >> "%LOG%" 2>&1
if errorlevel 1 goto fail
set "RESULT=%OUTDIR%\SchoologyGrades-%VERSION%-portable.zip"
set "NOTE=For a real installer, run: winget install JRSoftware.InnoSetup   (then build again)"
goto cleanup
:make_installer
echo [5/6] Creating the installer...
"%ISCC%" /Qp "/DMyAppVersion=%VERSION%" "/DAppSource=%WORK%\dist\SchoologyGrades" "/O%OUTDIR%" installer.iss >> "%LOG%" 2>&1
if errorlevel 1 goto fail
set "RESULT=%TARGET%"
goto cleanup
:skip_installer
echo [5/6] Skipping the installer (dev build)

rem ---- 6/6 tidy up (the build cache stays so the next build is faster)
:cleanup
echo [6/6] Cleaning up...
if exist "%WORK%\dist" rmdir /s /q "%WORK%\dist"
if exist "__pycache__" rmdir /s /q "__pycache__"
echo.
echo Done.
echo   Programs:   %PARENT%\SchoologyGrades.exe and SchoologySetup.exe
if defined RESULT echo   Installer:  %RESULT%
if defined NOTE echo   %NOTE%
goto launch

:launch
if "%RUN%"=="0" goto finish
if "%DEV%"=="1" (
  if exist "%PARENT%\SchoologyGrades.exe" start "" "%PARENT%\SchoologyGrades.exe"
) else (
  if exist "%TARGET%" start "" "%TARGET%"
)
goto finish

:icons_missing
echo The icon file icons\%MISSING% is missing, and drawing the icons didn't create it.
echo Is make_icons.py in the same folder as build.bat?
goto fail

:nopython
echo Python was not found. Install it from python.org and tick "Add python.exe to PATH".
goto finish_fail

:locked
echo.
echo Couldn't replace SchoologyGrades.exe or SchoologySetup.exe in:
echo   %PARENT%
echo If either program is running, close it and build again.
goto finish_fail

:fail
echo.
echo Build failed. Last lines of the log:
echo ------------------------------------------------------------
powershell -NoProfile -Command "Get-Content -LiteralPath $env:LOG -Tail 25"
echo ------------------------------------------------------------
echo Full log: %LOG%
:finish_fail
if defined CI exit /b 1
echo %cmdcmdline% | find /i "/c" >nul && pause
exit /b 1

:finish
if defined CI exit /b 0
echo %cmdcmdline% | find /i "/c" >nul && pause
exit /b 0
