@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 update_app.py
goto finished
:use_python
python update_app.py
:finished
pause
endlocal
