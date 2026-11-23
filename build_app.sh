#!/bin/bash
# Bygger "FortiGate Setup.app" i dist/. Kjøres på Mac-en:  ./build_app.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python3 ]; then
    echo "▸ Lager Python-miljø (.venv) ..."
    python3 -m venv .venv
fi
echo "▸ Installerer pakker (første gang tar det et par minutter) ..."
.venv/bin/python3 -m pip install -q --upgrade pip
.venv/bin/python3 -m pip install -q -r requirements.txt -r requirements-app.txt

echo "▸ Bygger appen ..."
rm -rf build dist
.venv/bin/pyinstaller --noconfirm --clean --windowed \
    --name "FortiGate Setup" \
    --osx-bundle-identifier no.fortinet-setup.app \
    --add-data "templates:templates" \
    --add-data "site_defaults.example.yaml:." \
    --add-data "secrets.env.example:." \
    --add-data "tests/fixtures:tests/fixtures" \
    --collect-submodules netmiko \
    --collect-data netmiko \
    --collect-data ntc_templates \
    app.py

APP="dist/FortiGate Setup.app"
echo
echo "✔ Ferdig: $APP"
echo "  Innstillinger og output ligger i ~/Library/Application Support/FortiGate Setup"
echo
read -r -p "Kopiere appen til Programmer-mappa? [j/N] " svar
if [[ "$svar" =~ ^[jJyY] ]]; then
    rm -rf "/Applications/FortiGate Setup.app"
    cp -R "$APP" /Applications/
    echo "✔ Lagt i Programmer. Første gang: høyreklikk → Åpne."
    open -R "/Applications/FortiGate Setup.app"
else
    open -R "$APP"
fi
