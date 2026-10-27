@echo off
setlocal
cd /d "%~dp0"
echo Pattern Lab AI Team League 2.0
echo First setup requires Python 3.12+ and an internet connection.
echo Keep this window open while using Pattern Lab.
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 bootstrap.py
goto finished
:use_python
python bootstrap.py
:finished
if errorlevel 1 pause
endlocal
