#!/bin/bash
# Dobbeltklikk i Finder for å starte. Første gang: høyreklikk → Åpne.
cd "$(dirname "$0")" || exit 1

B=$'\033[1m'; C=$'\033[1;36m'; G=$'\033[1;32m'; R=$'\033[1;31m'; D=$'\033[2m'; N=$'\033[0m'

pause() { echo; read -n 1 -s -r -p "${D}Trykk en tast for å lukke vinduet ...${N}"; echo; }

# Første gang: lag Python-miljø og installer pakker
if [ ! -x .venv/bin/python3 ]; then
    echo "${C}▸${N} Første gangs oppsett: installerer Python-pakker (tar et minutt) ..."
    if ! python3 -m venv .venv || ! .venv/bin/python3 -m pip install -q --upgrade pip \
       || ! .venv/bin/python3 -m pip install -q -r requirements.txt; then
        echo "${R}✖ Installasjonen feilet.${N} Har Mac-en Python 3? Prøv: xcode-select --install"
        rm -rf .venv; pause; exit 1
    fi
    echo "${G}✔${N} Ferdig installert"
fi
PY=.venv/bin/python3

# Første gang: lag lokale innstillinger fra eksempelfilene (de ligger ikke i Git)
if [ ! -f site_defaults.yaml ]; then
    cp site_defaults.example.yaml site_defaults.yaml
    echo "${C}▸${N} Laget site_defaults.yaml fra eksempelet. Fyll inn dine verdier."
    open -t site_defaults.yaml
fi
if [ ! -f secrets.env ]; then
    cp secrets.env.example secrets.env; chmod 600 secrets.env
    echo "${C}▸${N} Laget secrets.env. Fyll inn SNMP-passordene."
    open -t secrets.env
fi

# Hemmeligheter (SNMP-passord osv.) fra secrets.env, hvis den finnes
if [ -f secrets.env ]; then
    set -a; source secrets.env; set +a
fi

clear
cat <<EOF

${C}╭──────────────────────────────────────────────╮${N}
${C}│${N}  ${B}FortiGate-verktøy${N}                           ${C}│${N}
${C}╰──────────────────────────────────────────────╯${N}

  ${C}1)${N} ${B}Sett opp ny FortiGate${N}
  ${C}2)${N} ${B}Prøvekjøring${N}         ${D}les boksen og vis config, endrer ingenting${N}
  ${C}3)${N} ${B}Hent info fra boks${N}   ${D}lagrer show-output til collected/${N}
  ${C}4)${N} ${B}Åpne output-mappa${N}
  ${C}5)${N} ${B}Rediger standardverdier${N} ${D}site_defaults.yaml${N}
  ${C}q)${N} Avslutt

EOF
read -r -p "  ${C}?${N} ${B}Velg${N} ${D}[1]${N}: " choice
choice=${choice:-1}

case "$choice" in
    1) $PY setup.py ;;
    2) $PY setup.py --dry-run ;;
    3) $PY collect.py ;;
    4) mkdir -p output; open output; exit 0 ;;
    5) open -t site_defaults.yaml; exit 0 ;;
    q|Q) exit 0 ;;
    *) echo "${R}✖ Ukjent valg${N}" ;;
esac
pause
