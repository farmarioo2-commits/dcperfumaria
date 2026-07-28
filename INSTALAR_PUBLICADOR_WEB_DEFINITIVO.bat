@echo off
setlocal
chcp 65001 >nul

title Instalar publicador Web definitivo
set "DEST=C:\Projetos\app_flutter"
set "SRC=%~dp0arquivos\PUBLICAR_SITE_ERP_CORRETO.bat"

if not exist "%DEST%\pubspec.yaml" (
  echo ERRO: projeto não encontrado em %DEST%.
  pause
  exit /b 1
)

copy /y "%SRC%" "%DEST%\PUBLICAR_SITE_ERP_CORRETO.bat" >nul
if errorlevel 1 (
  echo ERRO ao instalar o publicador.
  pause
  exit /b 1
)

echo Publicador instalado com sucesso em:
echo %DEST%\PUBLICAR_SITE_ERP_CORRETO.bat
echo.
echo Execute esse arquivo sempre que quiser atualizar o site.
pause
