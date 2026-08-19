@echo off
rem Lanzador de SeaLock. Doble clic aqui para abrir la aplicacion.
cd /d "%~dp0"

set PY=
where python >nul 2>&1 && set PY=python
if "%PY%"=="" if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"

if "%PY%"=="" (
  echo.
  echo   No se encuentra Python. Instalalo con:
  echo       winget install -e --id Python.Python.3.12
  echo.
  pause
  exit /b 1
)

%PY% -u app\servidor.py
pause
