@echo off
REM Launches watchdog.py as a single process, same approach as run.bat.
REM Run this in its OWN terminal window, alongside run.bat -- it's a
REM separate process specifically so it keeps running (and can alert)
REM even if the scanner in the other window crashes or hangs.
set PYTHONPATH=%~dp0venv\Lib\site-packages
set PYTHONUNBUFFERED=1
"C:\Users\My PC\AppData\Local\Programs\Python\Python310\python.exe" "%~dp0watchdog.py"
