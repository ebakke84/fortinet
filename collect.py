#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Henter info fra en FortiGate via SSH og lagrer hvert svar i egen fil.
Endrer ingenting på boksen (bortsett fra tvunget passordbytte ved første innlogging).

Bruk:
    python3 collect.py                    # 192.168.1.99, bruker admin
    python3 collect.py --host 10.0.0.1    # annen IP
    python3 collect.py --no-full          # dropp full config (raskere)

Resultat: collected/<modell>_<serienr>/  +  en zip-fil av mappa
"""
from __future__ import annotations

import argparse
import getpass
import re
import shutil
import sys
import time
from pathlib import Path

try:
    import paramiko
    from netmiko import ConnectHandler
except ImportError:
    sys.exit("Mangler pakker. Kjør: pip install netmiko")

# filnavn -> kommando (samme navn som setup.py og tests/fixtures bruker)
COMMANDS = {
    "status": "get system status",
    "global": "show system global",
    "interface": "show system interface",
    "physical": "get system interface physical",
    "vswitch": "show system virtual-switch",
    "dhcp": "show system dhcp server",
    "policy": "show firewall policy",
    "sdwan": "show system sdwan",
    # ekstra, nyttig for å forstå modellen
    "hwswitch": "show system switch-interface",
    "nic": "get hardware nic",
    "route": "show router static",
    "admin": "show system admin",
}


def read_until(chan, pattern: str, timeout: float) -> str:
    buf, end = "", time.time() + timeout
    while time.time() < end:
        if chan.recv_ready():
            buf += chan.recv(65535).decode("utf-8", "replace")
            if re.search(pattern, buf):
                break
        else:
            time.sleep(0.2)
    return buf


def try_first_login(host: str, user: str) -> str | None:
    """Fabrikknullstilt boks: tomt passord + tvunget bytte. Returnerer nytt passord eller None."""
    t = paramiko.Transport((host, 22))
    t.start_client(timeout=20)
    ok = False
    for attempt in (
        lambda: t.auth_none(user),
        lambda: t.auth_password(user, ""),
        lambda: t.auth_interactive(user, lambda *a: [""] * len(a[2])),
    ):
        try:
            attempt()
            ok = t.is_authenticated()
        except paramiko.SSHException:
            pass
        if ok:
            break
    if not ok:
        t.close()
        return None

    chan = t.open_session()
    chan.get_pty(width=200)
    chan.invoke_shell()
    buf = read_until(chan, r"New Password:|#\s*$", 20)
    if "New Password" not in buf:
        t.close()
        return ""  # innlogget med tomt passord, ikke krav om bytte
    print("FortiGaten krever nytt admin-passord ved første innlogging.")
    while True:
        pw = getpass.getpass("  Nytt admin-passord: ")
        if pw and pw == getpass.getpass("  Gjenta: "):
            break
        print("  Tomme eller ulike passord, prøv igjen.")
    chan.send(pw + "\n")
    read_until(chan, r"Confirm Password:", 15)
    chan.send(pw + "\n")
    buf = read_until(chan, r"New Password:|#\s*$|[Ee]rror|[Ii]nvalid", 20)
    t.close()
    if "New Password" in buf or re.search(r"[Ee]rror|[Ii]nvalid", buf):
        sys.exit("FortiGaten avviste passordet (passordpolicy?):\n" + buf[-300:])
    print("  Passord satt. Husk det, setup.py trenger det.")
    return pw


def main() -> None:
    ap = argparse.ArgumentParser(description="Hent FortiGate-info via SSH")
    ap.add_argument("--host", default="192.168.1.99")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--out", default="collected")
    ap.add_argument("--no-full", action="store_true", help="ikke hent 'show full-configuration'")
    args = ap.parse_args()

    print(f"Kobler til {args.host} ...")
    try:
        pw = try_first_login(args.host, args.user)
    except (OSError, paramiko.SSHException) as e:
        sys.exit(f"Når ikke {args.host} på SSH: {e}\nStår Mac-en på en LAN-port med 192.168.1.x-adresse?")
    if pw is None:
        pw = getpass.getpass(f"Passord for {args.user}: ")

    conn = ConnectHandler(device_type="fortinet", host=args.host, username=args.user,
                          password=pw, conn_timeout=20, fast_cli=False)

    results: dict[str, str] = {}
    for name, cmd in COMMANDS.items():
        print(f"  {cmd}")
        results[name] = conn.send_command(cmd, read_timeout=90)
    if not args.no_full:
        print("  show full-configuration (kan ta et halvt minutt)")
        results["full-configuration"] = conn.send_command("show full-configuration", read_timeout=300)
    conn.disconnect()

    st = results["status"]
    model = re.search(r"FortiGate-(\S+)", st)
    serial = re.search(r"Serial-Number:\s*(\S+)", st)
    folder = Path(args.out) / f"{model.group(1) if model else 'ukjent'}_{serial.group(1) if serial else 'ukjent'}"
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in results.items():
        (folder / f"{name}.txt").write_text(text.strip() + "\n", encoding="utf-8")

    zip_path = shutil.make_archive(str(folder), "zip", folder.parent, folder.name)
    print(f"\nLagret i {folder}/ og {zip_path}")
    print("NB: full-configuration og admin.txt kan inneholde passord-hasher. Se over før du deler.")


if __name__ == "__main__":
    main()
