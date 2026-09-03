@echo off
rem Double-click me in Explorer.
rem
rem THE WINDOW MUST NOT VANISH. A double-clicked script that fails and closes
rem instantly is worse than no launcher at all - there is nothing to read and
rem no way to tell a crash from a clean exit. Every failure below jumps to
rem :hold, which pauses.
rem
rem Written with plain `if errorlevel` and `goto` rather than `||` blocks: cmd
rem parses a whole parenthesised block before running it, so errorlevel inside
rem one reads its value from BEFORE the block. That is a classic way for a
rem batch file to silently ignore its own failures.

cd /d "%~dp0"
if errorlevel 1 (
    echo Could not find the bot's folder. Move this file back beside engine\.
    goto hold
)

echo NS Bot
echo   folder: %CD%
echo.

rem `py` is the launcher shipped with python.org installs and is the reliable
rem one. Bare `python` on a fresh Windows opens the Microsoft Store instead of
rem running anything, which is a baffling failure to diagnose over a message.
set "PY="
where py >nul 2>&1
if not errorlevel 1 set "PY=py"
if not defined PY (
    where python >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo Python 3 is not installed.
    echo.
    echo Install it from https://www.python.org/downloads/ and TICK
    echo "Add python.exe to PATH" in the installer, then double-click this
    echo file again.
    goto hold
)

rem FIRST RUN ONLY. Skipped once done, so a normal start is not slowed by
rem re-checking things that cannot have changed.
if not exist ".venv\Scripts\python.exe" (
    echo First run: creating the virtual environment ^(a few seconds^)...
    %PY% -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
    echo Could not create .venv - see any message above.
    goto hold
)

".venv\Scripts\python.exe" -c "import cv2" >nul 2>&1
if errorlevel 1 (
    echo First run: installing the one dependency ^(opencv^)...
    ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
)
".venv\Scripts\python.exe" -c "import cv2" >nul 2>&1
if errorlevel 1 (
    echo Could not install the dependency. Are you online?
    goto hold
)

if not exist "run" mkdir run

rem No --attach. browser.launch^(reuse=True^) already attaches to a browser that
rem is serving CDP and starts one only when nothing is, so the launcher never
rem has to guess which case it is in - and guessing wrong is how an operator
rem ends up staring at "no page target after 40s".
echo Starting. Close this window ^(or press Quit in the panel^) to stop.
echo.

rem No `tee` on Windows. PowerShell's Tee-Object does the same job; without it
rem the run still works and the log is still written, only the on-screen copy
rem is lost. The log is the part that matters - it is the record of a session.
where powershell >nul 2>&1
if errorlevel 1 goto plain

".venv\Scripts\python.exe" engine\app.py 2>&1 | powershell -NoProfile -Command "$input | Tee-Object -FilePath run\app.log -Append"
goto done

:plain
".venv\Scripts\python.exe" engine\app.py >> run\app.log 2>&1
echo ^(output went to run\app.log^)

:done
echo.
echo If it said another window is already running, that instance still has the
echo game - use its panel, or close it first. Anything else: run\app.log has
echo the whole session.

:hold
echo.
echo ------------------------------------------
pause
