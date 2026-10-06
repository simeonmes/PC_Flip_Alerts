@echo off
REM Launches main.py as a single process.
REM
REM venv\Scripts\python.exe is only a launcher stub -- Windows has no true
REM "replace this process" syscall, so running it always spawns a second,
REM real interpreter process as a child. That's normal for any Windows venv,
REM not a bug, but it clutters the process list / terminal. This script
REM skips the stub by invoking the base interpreter directly and pointing
REM it at the venv's site-packages via PYTHONPATH, so exactly one process
REM shows up for the whole run.
set PYTHONPATH=%~dp0venv\Lib\site-packages
set PYTHONUNBUFFERED=1
"C:\Users\My PC\AppData\Local\Programs\Python\Python310\python.exe" "%~dp0main.py"
