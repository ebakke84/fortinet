# FortiGate førstegangsoppsett (FortiOS 7.6)

Interaktivt script for fabrikknullstilte FortiGate 40F, 60F, 50G, 70G og 120G.
Kjøres fra Mac, kobler til `192.168.1.99`, og setter opp hostname, WAN (én eller SD-WAN),
LAN med DHCP, SNMPv3, trusted hosts, timezone og admin-timeout.

## Enklest: dobbeltklikk

1. Hent prosjektet med Git (se under) til en mappe utenfor iCloud, f.eks. `~/Projects/fortinet`.
2. Dobbeltklikk **FortiGate Setup.command**. Første gang: høyreklikk → Åpne.
   Første oppstart installerer Python-pakkene og lager `site_defaults.yaml` og
   `secrets.env` fra eksempelfilene. Fyll inn dine verdier i dem.

Menyen har: sett opp ny boks, prøvekjøring, hent info, åpne output-mappa
og rediger standardverdier.

Større tekst i Terminal: **Cmd +** (tilbake: **Cmd 0**).

## Hente og oppdatere med Git

```bash
git clone https://github.com/ebakke84/fortinet.git ~/Projects/fortinet   # første gang
cd ~/Projects/fortinet && git pull                                       # oppdatere
```

Eller bruk GitHub Desktop: *File → Clone repository*, og *Fetch/Pull* for å oppdatere.

`site_defaults.yaml`, `secrets.env` og `output/` ligger aldri i Git (se `.gitignore`).
De blir liggende urørt når du oppdaterer.

## Manuell installasjon (alternativ)

```bash
cd fortigate-setup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Før hver kjøring

1. Rediger `site_defaults.yaml` (trusted hosts, SNMP osv.). Lages fra
   `site_defaults.example.yaml` første gang.
2. SNMP-passordene leses fra `secrets.env` (via menyen), eller sett dem selv:
   ```bash
   export FGT_SNMP_AUTH='...'
   export FGT_SNMP_PRIV='...'
   ```
3. Koble Mac-en til en LAN-port på FortiGaten og vent på DHCP-adresse i 192.168.1.x.

## Kjøring

```bash
python3 setup.py --dry-run     # les boksen og vis generert config, endrer ingenting
python3 setup.py               # kjør
```

Uten boks (for å teste maler og spørsmål):

```bash
python3 setup.py --offline tests/fixtures/61F
```

## Hva som skjer

1. Logger inn som `admin` med tomt passord og tar det tvungne passordbyttet.
2. Leser modell, versjon, porter, switch-medlemskap, DHCP-servere og policies.
3. Spør om hostname, LAN, DHCP, DNS og WAN. Portlista viser faktiske porter på modellen.
   Velger du en port som sitter i LAN-switchen (f.eks. `lan3` på 40F), flyttes den ut.
4. Viser oppsummering og generert CLI. Alt lagres i `output/<hostname>_<serienr>_<tid>/`
   (`config.txt`, `run.log`, `raw/`). Passord er maskert.
5. Etter at du skriver `ja`:
   - kjører system, SNMP, WAN/SD-WAN og policy linje for linje, og stopper på første feil
   - legger midlertidig IP i nytt LAN på Mac-ens nettverkskort (spør om sudo-passord)
   - sender LAN-IP + DHCP på én gang. SSH faller ut, det er forventet
   - kobler til på ny IP, verifiserer, kjører DHCP på nytt om den ikke kom med
   - setter endelige trusted hosts (fjerner 192.168.1.0/24)
   - fjerner Mac-aliaset og tvinger ny DHCP-lease, så Mac-en får ny IP og gateway

## Hvis noe går galt

Hvert steg lagres med en gang. Stopper scriptet, er endringene frem til feilen lagret.
Rett feilen og kjør på nytt, eller nullstill boksen med `execute factoryreset`.
Feilmeldingen fra FortiGaten vises på skjermen og ligger i `run.log`.

## Ikke testet mot ekte boks

Scriptet er testet mot eksempeldata og en simulert FortiGate, ikke mot ekte hardware.
Kjør det første gang mot en lab-boks og sjekk spesielt:

- [ ] Tvunget passordbytte ved første SSH-innlogging oppfører seg som forventet
- [ ] `get system interface physical` lister portene riktig på 50G, 70G og 120G
- [ ] Porter kan flyttes ut av `virtual-switch` på din modell
- [ ] DHCP-blokken etter IP-byttet (kommer den med, eller kjøres den på nytt?)
- [ ] SNMPv3: polling og traps fungerer fra NMS

## Filer

| Fil | Innhold |
|---|---|
| `setup.py` | selve scriptet |
| `site_defaults.example.yaml` | mal for faste innstillinger (kopieres til `site_defaults.yaml`) |
| `secrets.env.example` | mal for SNMP-passord (kopieres til `secrets.env`) |
| `FortiGate Setup.command` | meny, start med dobbeltklikk |
| `templates/*.j2` | CLI-maler per steg |
| `tests/fixtures/61F` | ekte default-output fra en 61F (FortiOS 7.6.6) |
| `tests/fixtures/40F` | håndlaget eksempel for 40F |
| `collect.py` | henter samme info fra en boks, til nye fixtures |
