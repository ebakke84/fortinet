#!/bin/bash
# Bygger "FortiGate Setup.app" i dist/. Kjøres på Mac-en:  ./build_app.sh
# GitHub Actions bruker det samme scriptet (se .github/workflows/build.yml).
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python3 ]; then
    echo "▸ Lager Python-miljø (.venv) ..."
    python3 -m venv .venv
fi
.venv/bin/python3 --version
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

echo "▸ Selvtest av den bygde appen ..."
OUT="$(mktemp -t fgt-selftest)"
if ! QT_QPA_PLATFORM=offscreen FGT_SELFTEST_OUT="$OUT" "$APP/Contents/MacOS/FortiGate Setup" --selftest; then
    cat "$OUT"; echo "✖ Selvtesten feilet"; exit 1
fi
cat "$OUT"; echo

# Zip med ditto, så symlenker og signatur i .app-pakken blir med
ditto -c -k --keepParent "$APP" "dist/FortiGate-Setup-mac.zip"

echo
echo "✔ Ferdig: $APP  (og dist/FortiGate-Setup-mac.zip)"
echo "  Innstillinger og output ligger i ~/Library/Application Support/FortiGate Setup"
[ -n "${CI:-}" ] && exit 0

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
