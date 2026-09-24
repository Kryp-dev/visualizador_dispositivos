@echo off
REM Empacota o kiosk-webos em um .ipk usando o ares da webOS TV SDK.
REM Executar de dentro da pasta kiosk-webos:
REM   package.bat
where ares-package >nul 2>nul
if %errorlevel% neq 0 (
  echo [ERRO] ares-package nao encontrado. Instale o webOS TV SDK CLI.
  echo        Voce precisa ter o Node.js instalado e rodar no terminal:
  echo        npm install -g @webosose/ares-cli
  pause
  exit /b 1
)
if not exist dist mkdir dist
call ares-package . -o dist
if %errorlevel% equ 0 (
  echo.
  echo [SUCESSO] IPK gerado na pasta dist/!
) else (
  echo [ERRO] Falha ao empacotar.
)
pause