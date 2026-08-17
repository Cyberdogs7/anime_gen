@echo off
rem ============================================================
rem  Anime Studio Dashboard launcher
rem  Starts the approval/review dashboard on http://127.0.0.1:8125
rem  (keeps its own console open so you can watch logs)
rem ============================================================
setlocal
cd /d "%~dp0"

rem Python to use. Defaults to anaconda (where studio.py runs best);
rem override with the STUDIO_PY env var if you use a different interpreter.
if "%STUDIO_PY%"=="" set "STUDIO_PY=C:\Users\Chad\anaconda3\python.exe"

echo Starting Anime Studio dashboard...
echo   Python : %STUDIO_PY%
echo   URL    : http://127.0.0.1:8125
echo Press Ctrl+C to stop.
echo.

"%STUDIO_PY%" -B studio.py dashboard --port 8125

endlocal
