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

## Mac-appen

Samme oppsett i et vanlig Mac-vindu: koble til → fyll ut skjema → se oppsummering → konfigurer.
Portene velges fra lister med det boksen faktisk har, og feil markeres i rødt mens du fyller ut.

- **Prøve fra kildekoden:** menyvalg **6** (installerer PySide6 første gang), eller `python3 app.py`.
- **Bygge `FortiGate Setup.app`:** menyvalg **7**, eller `./build_app.sh`. Appen havner i `dist/`,
  og scriptet tilbyr å kopiere den til Programmer. Første gang: høyreklikk → Åpne.
- **Innstillinger:** *Fil → Innstillinger* (⌘,) redigerer `site_defaults.yaml` og SNMP-passordene,
  og sjekker at fila er gyldig før den lagres.
- **Datamappe:** fra kildekoden brukes prosjektmappa (som Terminal-versjonen). Den bygde appen bruker
  `~/Library/Application Support/FortiGate Setup`. *Fil → Velg datamappe* peker den et annet sted,
  f.eks. til prosjektmappa, så app og Terminal deler innstillinger og output.
- **Prøvekjøring uten boks:** *Åpne lagret boks-data …* og velg f.eks. `tests/fixtures/61F`
  eller `raw/`-mappa fra en tidligere kjøring.
- Når Mac-en trenger midlertidig IP i nytt LAN, viser macOS sin vanlige passorddialog.

## Windows-appen

Den samme appen finnes for Windows. Den bygges automatisk av GitHub Actions ved hver push.

- **Laste ned:** GitHub → **Actions** → *Windows-app* → siste grønne kjøring → **Artifacts** →
  *FortiGate-Setup-windows*. Pakk ut zip-fila et fast sted (f.eks. `C:\Verktøy\FortiGate Setup`)
  og dobbeltklikk **FortiGate Setup.exe**. Lag gjerne en snarvei på skrivebordet.
  En tag som `v1.0` (`git tag v1.0 && git push --tags`) lager en *Release* med zip-fila,
  så slipper du å lete i Actions.
- **Første gang:** Windows SmartScreen sier «Windows beskyttet PC-en» fordi appen ikke er signert.
  Trykk *Mer info* → *Kjør likevel*.
- **Datamappe:** `%APPDATA%\FortiGate Setup` (innstillinger, `secrets.env` og `output/`).
  *Fil → Velg datamappe* fungerer som på Mac. Innstillinger: **Ctrl+,**.
- **Midlertidig IP i nytt LAN:** Windows viser sin vanlige dialog for administratortilgang to ganger,
  når IP-en legges til og når den fjernes til slutt. Appen bruker `netsh`, og etterpå
  `ipconfig /release` og `/renew`, så PC-en får ny adresse og gateway. Klarer den ikke å legge til
  IP-en (for eksempel fordi du trykker Nei), ber den deg legge den inn manuelt og venter.
- **Bygge selv på en Windows-PC:** installer Python 3 fra python.org (huk av for *Add python.exe to PATH*)
  og dobbeltklikk `build_app.bat`. Appen havner i `dist\FortiGate Setup\`.
- **Fra kildekoden:** `py -m pip install -r requirements.txt -r requirements-app.txt` og `py app.py`.

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
   - legger midlertidig IP i nytt LAN på maskinens nettverkskort (Mac: spør om sudo-passord,
     Windows: spør om administratortilgang)
   - sender LAN-IP + DHCP på én gang. SSH faller ut, det er forventet
   - kobler til på ny IP, verifiserer, kjører DHCP på nytt om den ikke kom med
   - setter endelige trusted hosts (fjerner 192.168.1.0/24)
   - fjerner den midlertidige IP-en og tvinger ny DHCP-lease, så maskinen får ny IP og gateway

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
- [ ] Windows: midlertidig IP (`netsh`) og ny DHCP-lease til slutt på en ekte PC

## Filer

| Fil | Innhold |
|---|---|
| `setup.py` | Terminal-versjonen |
| `app.py` | appen for Mac og Windows (PySide6) |
| `fgt/core.py` | felles logikk for begge: lesing, validering, maler, utførelse |
| `build_app.sh` | bygger `FortiGate Setup.app` med PyInstaller (Mac) |
| `build_app.bat` | bygger `FortiGate Setup.exe` med PyInstaller (Windows) |
| `.github/workflows/windows.yml` | GitHub Actions: tester og bygger Windows-appen |
| `tests/test_core.py` | tester (`python3 -m pytest -q`) |
| `site_defaults.example.yaml` | mal for faste innstillinger (kopieres til `site_defaults.yaml`) |
| `secrets.env.example` | mal for SNMP-passord (kopieres til `secrets.env`) |
| `FortiGate Setup.command` | meny, start med dobbeltklikk |
| `templates/*.j2` | CLI-maler per steg |
| `tests/fixtures/61F` | ekte default-output fra en 61F (FortiOS 7.6.6) |
| `tests/fixtures/40F` | håndlaget eksempel for 40F |
| `collect.py` | henter samme info fra en boks, til nye fixtures |
