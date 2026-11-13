@echo off
rem Builds dist\Stash.exe with the newest yt-dlp.
rem Run this again if a site changes something and downloads stop working.
rem Build files go in a short path because Windows limits path length by default,
rem which breaks Python installs inside deeply nested project folders.
cd /d "%~dp0"
set "WORK=%LOCALAPPDATA%\PinterestDownloaderBuild"
if not exist "%WORK%\venv\Scripts\pip.exe" python -m venv --clear "%WORK%\venv" || goto :failed
"%WORK%\venv\Scripts\python.exe" -m pip install --upgrade --disable-pip-version-check -r requirements.txt || goto :failed
"%WORK%\venv\Scripts\pyinstaller.exe" --noconfirm --onefile --windowed --name Stash --collect-all imageio_ffmpeg --add-data "%CD%\fonts;fonts" --add-data "%CD%\assets;assets" --icon "%CD%\assets\icon.ico" --workpath "%WORK%\work" --specpath "%WORK%" --distpath dist Stash.pyw || goto :failed
echo.
echo Done: dist\Stash.exe
pause
exit /b 0
:failed
echo.
echo Build failed: see the messages above.
pause
exit /b 1
