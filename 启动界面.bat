@echo off
rem ==========================================================================
rem  qqdigest - desktop GUI launcher
rem
rem  !! KEEP THIS FILE PURE ASCII - DO NOT ADD CHINESE TEXT !!
rem
rem  Why: cmd.exe mis-reads a batch file that contains non-ASCII bytes.
rem  Together with a console code page switch it desynchronises its file
rem  offset and starts executing torn-apart lines, which makes the script
rem  fall into the error branch and silently hang on "pause". That bug is
rem  exactly what made this launcher appear to "do nothing" when double
rem  clicked. Verified on this machine: GBK-encoded and UTF-8 Chinese
rem  comments both break it, pure ASCII works. All user-facing Chinese
rem  text lives in gui.py instead.
rem ==========================================================================

setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"

set "PY="
set "PYW="

rem --- locate Python (explicit paths first, then PATH) ----------------------
if exist "D:\Tools\Python311\python.exe"  set "PY=D:\Tools\Python311\python.exe"
if exist "D:\Tools\Python311\pythonw.exe" set "PYW=D:\Tools\Python311\pythonw.exe"
if not defined PY  if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"  set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"
if not defined PY  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"  set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
if not defined PY  if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"  set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
if not defined PY  for /f "delims=" %%P in ('where python 2^>nul')  do if not defined PY  set "PY=%%P"
if not defined PYW for /f "delims=" %%P in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%P"

if not defined PY (
    echo.
    echo  [ERROR] Python was not found.
    echo.
    echo  Install Python 3.11 or newer from https://www.python.org/downloads/
    echo  or edit the paths near the top of this file.
    echo.
    pause
    exit /b 1
)
if not defined PYW set "PYW=%PY%"

rem --- preflight: silent when everything is fine, verbose when broken -------
"%PY%" "%ROOT%gui.py" --preflight >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [ERROR] Preflight check failed. Details below:
    echo.
    "%PY%" "%ROOT%gui.py" --preflight
    echo.
    echo  A full traceback is also written to logs\gui-error.log
    echo.
    pause
    exit /b 1
)

start "" "%PYW%" "%ROOT%gui.py"
exit /b 0
