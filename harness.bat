@echo off
rem One-key entry to the terminal UI. ASCII ONLY, CRLF ONLY: cmd reads .bat files in the OEM
rem codepage and seeks by byte offset, so Chinese comments or bare-LF line endings make it start
rem executing in the middle of a line (observed: 'the' is not recognized ...). All Chinese text
rem lives on the Python side (runs\tui-last.log, runs\tui-error.log, the UI itself).
rem
rem Two windows on purpose: this parent only spawns the UI window and exits immediately, so
rem closing the bat window is not the same action as quitting the UI. If the UI dies with a
rem non-zero code, its own window stays open and prints the two logs.
rem
rem Preferred host: Windows Terminal, because mouse and hover only work there -- textual's DECSET
rem (1000/1003/1006) is honoured by WT, while conhost never writes mouse bytes to the input
rem stream (see the known-limits section of README). wt -w new forces a NEW WT window instead of
rem opening a tab in an existing one. Measured on wt 1.24.12741.0: -N, -n and --new-window are
rem all REJECTED (wt starts, complains, and never runs the command -- the bat then looks like it
rem does nothing at all). -w new and -w -1 are accepted. If wt.exe cannot be found at all, fall
rem back to the old conhost window: same UI, keyboard only, and the status line says so.
setlocal
if not defined HARNESS_PY set "HARNESS_PY=C:\conda\envs\llm_learn\python.exe"

if /i "%~1"=="--child" goto child
cd /d "%~dp0"
where wt.exe >nul 2>nul
if errorlevel 1 goto conhost
start "" wt.exe -w new cmd /c ""%~f0" --child %*"
exit /b 0

:conhost
start "spark harness" cmd /c ""%~f0" --child %*"
exit /b 0

:child
chcp 65001 >nul
cd /d "%~dp0"
call "%HARNESS_PY%" main.py tui %2 %3 %4 %5 %6 %7 %8 %9
if errorlevel 1 (
  echo.
  echo [harness] UI exited with an error. Summary: runs\tui-last.log
  if exist "%~dp0runs\tui-last.log" type "%~dp0runs\tui-last.log"
  echo.
  echo [harness] Traceback: runs\tui-error.log
  if exist "%~dp0runs\tui-error.log" type "%~dp0runs\tui-error.log"
  echo.
  echo Window kept open. Press any key to close.
  pause >nul
)
endlocal
