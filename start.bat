@echo off
REM KirkChat - lancement du serveur local + ouverture du navigateur
setlocal
cd /d "%~dp0"

REM Trouve python (py launcher puis python puis chemin winget)
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY where python >nul 2>nul && set "PY=python"

if not defined PY (
  for /f "delims=" %%i in ('dir /b /s "%LOCALAPPDATA%\Programs\Python\Python3*\python.exe" 2^>nul') do set "PY=%%i"
)

if not defined PY (
  echo [KirkChat] Python est introuvable.
  echo Installez Python 3 depuis https://www.python.org/downloads/ puis relancez ce fichier.
  pause
  exit /b 1
)

echo [KirkChat] Demarrage du serveur avec : %PY%
start "" http://localhost:8000
"%PY%" server.py
endlocal
