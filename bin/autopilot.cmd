@echo off
rem Launcher for Windows (cmd and PowerShell): runs the autopilot package of this checkout with the
rem Python launcher (py -3) when there is one, else python. The command runs once either way.
set "PYTHONPATH=%~dp0..;%PYTHONPATH%"
where py >NUL 2>NUL
if %errorlevel%==0 (py -3 -m autopilot %*) else (python -m autopilot %*)
exit /b %errorlevel%
