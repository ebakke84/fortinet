@echo off
rem Bygger "FortiGate Setup.exe" i dist\FortiGate Setup\. Kjøres på en Windows-PC: dobbeltklikk eller build_app.bat
rem GitHub Actions bruker det samme scriptet (se .github\workflows\windows.yml).
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo - Lager Python-miljo i .venv ...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 goto :nopython
)
set "PY=.venv\Scripts\python.exe"

echo - Installerer pakker, forste gang tar det et par minutter ...
"%PY%" -m pip install -q --upgrade pip || goto :fail
"%PY%" -m pip install -q -r requirements.txt -r requirements-app.txt || goto :fail

echo - Bygger appen ...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
"%PY%" -m PyInstaller --noconfirm --clean --windowed ^
    --name "FortiGate Setup" ^
    --add-data "templates;templates" ^
    --add-data "site_defaults.example.yaml;." ^
    --add-data "secrets.env.example;." ^
    --add-data "tests\fixtures;tests\fixtures" ^
    --collect-submodules netmiko ^
    --collect-data netmiko ^
    --collect-data ntc_templates ^
    app.py || goto :fail

echo - Selvtest av den bygde appen ...
set "FGT_SELFTEST_OUT=%TEMP%\fgt-selftest.txt"
if exist "%FGT_SELFTEST_OUT%" del "%FGT_SELFTEST_OUT%"
start "" /wait "dist\FortiGate Setup\FortiGate Setup.exe" --selftest
set "RC=%errorlevel%"
if exist "%FGT_SELFTEST_OUT%" type "%FGT_SELFTEST_OUT%"
echo.
if not "%RC%"=="0" goto :fail

echo.
echo OK: dist\FortiGate Setup\FortiGate Setup.exe
echo    Innstillinger og output ligger i %%APPDATA%%\FortiGate Setup
if "%CI%"=="" (
    explorer "dist\FortiGate Setup"
    pause
)
exit /b 0

:nopython
echo FEIL: Fant ikke Python 3. Installer fra https://www.python.org/downloads/ eller Microsoft Store,
echo og huk av for "Add python.exe to PATH".
if "%CI%"=="" pause
exit /b 1

:fail
echo FEIL: Byggingen feilet, se meldingene over.
if "%CI%"=="" pause
exit /b 1
