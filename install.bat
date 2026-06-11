@echo off
:: Instalador do multi-ssh para Windows
setlocal EnableDelayedExpansion

set "INSTALL_DIR=%APPDATA%\multi-ssh"
set "VENV_DIR=%INSTALL_DIR%\venv"
set "SCRIPTS_DIR=%VENV_DIR%\Scripts"

echo.
echo  [multi-ssh] Instalador para Windows
echo  =====================================
echo.

:: Verifica Python
python --version >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo  [ERRO] Python nao encontrado.
    echo  Baixe em: https://www.python.org/downloads/
    echo  Marque "Add Python to PATH" durante a instalacao.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo  Python %PY_VER% encontrado.

:: Cria diretórios
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

:: Copia o script
echo  Copiando arquivos...
copy /Y "%~dp0multissh.py" "%INSTALL_DIR%\multissh.py" >nul

:: Cria ambiente virtual
echo  Criando ambiente virtual...
python -m venv "%VENV_DIR%"

:: Instala dependências
echo  Instalando dependencias (paramiko, questionary, rich)...
"%SCRIPTS_DIR%\pip.exe" install --quiet --upgrade pip
"%SCRIPTS_DIR%\pip.exe" install --quiet paramiko questionary rich

:: Cria arquivo .bat launcher
echo @echo off > "%INSTALL_DIR%\multissh.bat"
echo "%SCRIPTS_DIR%\python.exe" "%INSTALL_DIR%\multissh.py" %%* >> "%INSTALL_DIR%\multissh.bat"

:: Adiciona ao PATH do usuário
echo  Adicionando ao PATH do usuario...
for /f "skip=2 tokens=3*" %%a in ('reg query HKCU\Environment /v PATH 2^>nul') do set "CURRENT_PATH=%%a %%b"
echo !CURRENT_PATH! | find /i "%INSTALL_DIR%" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    setx PATH "%INSTALL_DIR%;!CURRENT_PATH!" >nul
    echo  PATH atualizado.
)

echo.
echo  Instalacao concluida!
echo.
echo  Comando: %INSTALL_DIR%\multissh.bat
echo  Config:  %%APPDATA%%\multi-ssh\
echo.
echo  IMPORTANTE: Abra um novo terminal (cmd ou PowerShell) e execute:
echo.
echo      multissh
echo.
pause
