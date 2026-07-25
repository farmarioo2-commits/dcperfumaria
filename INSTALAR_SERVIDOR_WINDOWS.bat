@echo off
title Instalar Servidor Gestão Fácil
cd /d %~dp0

where py >nul 2>nul
if %errorlevel% neq 0 (
  echo Instale Python 3.12 marcando Add Python to PATH.
  pause
  exit /b 1
)

if not exist .venv (
  py -3.12 -m venv .venv 2>nul
  if errorlevel 1 py -m venv .venv
)

call .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt

if not exist .env copy .env.example .env

netsh advfirewall firewall add rule name="Gestao Facil API 9000" dir=in action=allow protocol=TCP localport=9000 >nul 2>nul

echo Instalacao concluida.
pause
