#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FortiGate førstegangsoppsett for FortiOS 7.6 – Terminal-versjonen.

Kobler til en fabrikknullstilt boks på 192.168.1.99, leser hva den har,
spør interaktivt om hostname / WAN / LAN, henter SNMPv3, trusted hosts,
timezone og admin-innstillinger fra site_defaults.yaml, og kjører konfigurasjonen.
Logikken ligger i fgt/core.py og deles med appen (app.py).

Bruk:
    python3 setup.py                                # live mot 192.168.1.99
    python3 setup.py --dry-run                      # les boksen, vis config, ikke endre
    python3 setup.py --offline tests/fixtures/61F   # uten boks (test av flyt og maler)
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

try:
    import yaml  # noqa: F401
    import jinja2  # noqa: F401
except ImportError as e:  # pragma: no cover
    sys.exit(f"Mangler Python-pakke ({e.name}). Kjør: pip install -r requirements.txt")

from fgt.core import (  # noqa: E402
    LOG, SUPPORTED_MODELS, Abort, ApplyError, Events, OfflineDevice, RunOptions, admin_runner, this_pc,
    a_prefix, connect, default_data_dir, default_range, execute, gui_url, load_defaults,
    load_env_file, make_plan, open_url, port_label, preflight, read_box, v_alias, v_choice,
    v_dns, v_hostname, v_iface_ip, v_ip, wait_port, wan_candidates,
)


_COLOR = (sys.stdout.isatty() or bool(os.environ.get("FORCE_COLOR"))) and not os.environ.get("NO_COLOR")


def _st(code: str):
    return (lambda t: f"\033[{code}m{t}\033[0m") if _COLOR else (lambda t: str(t))


BOLD, DIM = _st("1"), _st("2")


RED, GREEN, YELLOW, CYAN = _st("1;31"), _st("1;32"), _st("1;33"), _st("1;36")


WIDTH = 64


def say(msg: str = "") -> None:
    print(msg, flush=True)


def header(title: str) -> None:
    say()
    say(CYAN(f"━━ {title.upper()} " + "━" * max(0, WIDTH - len(title) - 4)))
    say()


def ok(msg: str) -> None:
    say(f"  {GREEN('✔')} {msg}")


def warn(msg: str) -> None:
    say(f"  {YELLOW('!')} {YELLOW(msg)}")


def fail(msg: str) -> None:
    say(f"  {RED('✖')} {RED(msg)}")


def info_line(msg: str) -> None:
    say(f"  {DIM('·')} {msg}")


def kv(label: str, value, width: int = 16) -> None:
    say(f"  {DIM(label.ljust(width))}{BOLD(value)}")


def box(lines: list[str], color=CYAN) -> None:
    w = max(len(x) for x in lines) + 4
    say(color("╭" + "─" * w + "╮"))
    for x in lines:
        say(color("│") + "  " + BOLD(x.ljust(w - 2)) + color("│"))
    say(color("╰" + "─" * w + "╯"))


class Progress:
    """Ett steg på én linje:  ▸ Navn ...  ✔"""

    def __init__(self, name: str):
        self.name = name

    def __enter__(self):
        print(f"  {CYAN('▸')} {self.name} ...", end="", flush=True)
        return self

    def __exit__(self, typ, exc, tb):
        print(f"  {GREEN('✔') if typ is None else RED('✖')}", flush=True)
        return False


def ask(prompt: str, default=None, validator=None):
    hint = " " + DIM(f"[{default}]") if default not in (None, "") else ""
    while True:
        try:
            s = input(f"  {CYAN('?')} {BOLD(prompt)}{hint}: ").strip()
        except EOFError:
            raise Abort("Avbrutt (ingen input).")
        if not s and default is not None:
            s = str(default)
        if not s:
            fail("Må fylles ut")
            continue
        if validator is None:
            return s
        try:
            return validator(s)
        except ValueError as e:
            fail(str(e))


def ask_yes(prompt: str, default: bool = True) -> bool:
    d = "J/n" if default else "j/N"
    while True:
        try:
            s = input(f"  {CYAN('?')} {BOLD(prompt)} {DIM(f'[{d}]')}: ").strip().lower()
        except EOFError:
            raise Abort("Avbrutt (ingen input).")
        if not s:
            return default
        if s in ("j", "ja", "y", "yes"):
            return True
        if s in ("n", "nei", "no"):
            return False


def interview(info: dict, d: dict) -> dict:
    a: dict = {}
    header("Grunnoppsett")
    a["hostname"] = ask("Hostname", None, v_hostname)

    header("LAN")
    def v_lan_iface(s):
        if s not in info["ifaces"]:
            raise ValueError(f"Finner ikke interface '{s}'")
        return s
    a["lan_iface"] = ask("LAN-interface", info["mgmt_iface"] or "lan", v_lan_iface)
    lan_ip = ask("LAN IP/prefiks (f.eks. 10.20.30.1/24)", None, lambda s: v_iface_ip(s, "LAN-adresse"))
    a["lan_ip"] = lan_ip
    net = lan_ip.network

    a["dhcp_enabled"] = ask_yes("DHCP-server på LAN?", True)
    if a["dhcp_enabled"]:
        ds, de = default_range(net, lan_ip.ip, d["lan"]["dhcp_offsets"])

        def v_start(s):
            ip = v_ip(s)
            if ip not in net or ip in (net.network_address, net.broadcast_address, lan_ip.ip):
                raise ValueError(f"Må være en ledig vertsadresse i {net}")
            return ip
        a["dhcp_start"] = ask("DHCP fra", ds, v_start)

        def v_end(s):
            ip = v_start(s)
            if ip < a["dhcp_start"]:
                raise ValueError("Slutt må være etter start")
            if a["dhcp_start"] <= lan_ip.ip <= ip:
                raise ValueError("LAN-IP-en kan ikke ligge inne i DHCP-rangen")
            return ip
        a["dhcp_end"] = ask("DHCP til", de, v_end)
        a["dns"] = ask("DNS-servere (kommaseparert)", ", ".join(d["lan"]["dns"]), v_dns)

    header("WAN")
    cands = wan_candidates(info, a["lan_iface"])
    usable = [p for p in cands if p not in info["aggregate_of"]]
    say(f"  {BOLD('Porter på boksen:')}")
    for i, p in enumerate(cands, 1):
        lbl = port_label(info, p)
        line = f"   {CYAN(f'{i:>2})')} {BOLD(p.ljust(12))} {DIM(lbl)}"
        say(DIM(f"   {i:>2}) {p.ljust(12)} {lbl}") if p in info["aggregate_of"] else line)
    say()
    pref = [p for p in ("wan1", "wan2", "wan") if p in usable]
    two_ok = len(usable) >= 2
    mode = ask("WAN-oppsett: 1) én WAN  2) to WAN med SD-WAN", "2" if len(pref) >= 2 else "1",
               v_choice({"1": "single", "2": "sdwan"}))
    if mode == "sdwan" and not two_ok:
        raise Abort("Fant ikke to ledige porter for SD-WAN.")
    a["wan_mode"] = mode

    wans: list[dict] = []
    for n in range(1, 3 if mode == "sdwan" else 2):
        taken = {w["port"] for w in wans}

        def v_port(s, taken=taken):
            if s.isdigit() and 1 <= int(s) <= len(cands):
                s = cands[int(s) - 1]
            if s not in cands:
                raise ValueError("Velg et nummer eller portnavn fra listen")
            if s in info["aggregate_of"]:
                raise ValueError(f"{s} er medlem av '{info['aggregate_of'][s]}' og kan ikke brukes")
            if s in taken:
                raise ValueError(f"{s} er allerede valgt")
            return s
        default = next((p for p in pref if p not in taken), None)
        port = ask(f"WAN{n} port", default, v_port)
        if port in info["vswitch_of"]:
            warn(f"{port} flyttes ut av switchen '{info['vswitch_of'][port]}'. "
                f"Pass på at {this_pc()} IKKE står i denne porten.")
        m = ask(f"WAN{n} adressering: 1) DHCP  2) statisk", "1", v_choice({"1": "dhcp", "2": "static"}))
        w = {"port": port, "mode": m,
             "alias": ask(f"WAN{n} navn, f.eks. Telenor (- for ingen)", "-", v_alias)}
        if m == "static":
            def v_wan(s):
                ipi = v_iface_ip(s, "WAN-adresse")
                if ipi.network.overlaps(net):
                    raise ValueError(f"Overlapper med LAN-nettet {net}")
                return ipi
            ipi = ask(f"WAN{n} IP/prefiks", None, v_wan)

            def v_gw(s, ipi=ipi):
                gw = v_ip(s)
                if gw not in ipi.network or gw == ipi.ip:
                    raise ValueError(f"Gateway må ligge i {ipi.network} og være forskjellig fra WAN-IP")
                return gw
            w["ipi"] = ipi
            w["gateway"] = ask(f"WAN{n} gateway", None, v_gw)
        wans.append(w)
    a["wans"] = wans
    return a


def show_info(info: dict) -> None:
    v = ".".join(map(str, info["version"]))
    header("Boksen")
    kv("Modell", f"FortiGate-{info['model']}")
    kv("FortiOS", v)
    kv("Serienummer", info["serial"])
    kv("Koblet via", f"{info['mgmt_iface'] or 'ukjent'}  ({info['mgmt_net']})")
    if info["cfg_save"] != "automatic":
        say()
        warn(f"cfg-save står i '{info['cfg_save']}' fra en tidligere kjøring – settes tilbake til automatic")


def show_summary(ctx: dict) -> None:
    lan = ctx["lan"]
    header("Oppsummering")
    kv("Hostname", ctx["hostname"])
    kv("Tidssone", ctx["timezone"])
    kv("Admin-timeout", f"{ctx['admin_timeout']} min")
    kv("Admin HTTPS", f"port {ctx['admin_https_port']}  ({', '.join('TLS ' + v.replace('tlsv', '').replace('-', '.') for v in ctx['admin_tls'].split())})")
    say()
    kv("LAN", f"{lan['iface']}  {lan['cidr']}")
    if lan["dhcp_enabled"]:
        kv("DHCP", f"{lan['dhcp_start']} – {lan['dhcp_end']}  (lease {lan['lease_time'] // 3600} t)")
        kv("DNS", ", ".join(lan["dns"]))
    else:
        kv("DHCP", "av")
    say()
    kv("WAN-modus", "SD-WAN" if ctx["wan_mode"] == "sdwan" else "én WAN")
    for i, w in enumerate(ctx["wans"], 1):
        addr = f"{w['ip']}/{a_prefix(w)}  gw {w['gateway']}" if w["mode"] == "static" else "DHCP"
        name = f"  «{w['alias']}»" if w["alias"] else ""
        kv(f"WAN{i}", f"{w['port']}{name}  {addr}")
    say()
    th = [f"{t.split()[0]}/{a_prefix({'mask': t.split()[1]})}" for t in ctx["final_trusthosts"]]
    kv("Trusted hosts", ", ".join(th) or "(ingen)")
    kv("SNMPv3", ", ".join(u["name"] for u in ctx["snmp"]["users"]) or "(ingen)")
    if ctx["replace_policies_desc"] or ctx["policy"]["exists"] or ctx["delete_hc"]:
        say()
    if ctx["delete_hc"]:
        info_line(f"Fjerner {len(ctx['delete_hc'])} default SD-WAN-helsesjekker (Default_*)")
    for desc in ctx["replace_policies_desc"]:
        info_line(f"Default-policy {desc} erstattes av «{ctx['policy']['name']}»")
    if ctx["policy"]["exists"]:
        info_line(f"Policy «{ctx['policy']['name']}» finnes fra før og beholdes")


# ───────────────────────────── hendelser i Terminal ─────────────────────────────

class CLIEvents(Events):
    def step(self, name: str):
        return Progress(name)

    def info(self, msg: str) -> None:
        info_line(msg)

    def ok(self, msg: str) -> None:
        ok(msg)

    def warn(self, msg: str) -> None:
        warn(msg)

    def confirm(self, msg: str) -> None:
        warn(msg)
        input("  Trykk Enter når klar ...")

    def new_password(self) -> str:
        while True:
            pw = getpass.getpass(f"  {CYAN('?')} {BOLD('Nytt FortiGate-passord')}: ")
            if pw and pw == getpass.getpass(f"  {CYAN('?')} {BOLD('Gjenta passord')}: "):
                return pw
            fail("Passordene var tomme eller like ikke")

    def password(self, prompt: str) -> str:
        return getpass.getpass(f"  {CYAN('?')} {BOLD(prompt)}: ")


def show_status(st) -> None:
    header("Status")
    if st.default_routes:
        ok("Default-rute finnes")
        for ln in st.default_routes:
            say(DIM("      " + ln))
    else:
        warn("Ingen default-rute ennå – er WAN-kabelen koblet til?")
    for port, state in st.sdwan:
        if state == "alive":
            ok(f"SD-WAN {port}: oppe")
        elif state:
            warn(f"SD-WAN {port}: {state}")
        else:
            info_line(f"SD-WAN {port}: ingen status ennå")


def open_gui(ctx: dict) -> None:
    """Åpne FortiGate-GUI-en i standardnettleseren når den svarer."""
    url = gui_url(ctx)
    say()
    if not ask_yes(f"Åpne {url} i nettleseren?", True):
        return
    with Progress("Venter på at GUI-en svarer"):
        up = wait_port(ctx["lan"]["ip"], ctx["admin_https_port"], 60)
    if not up:
        warn(f"Får ikke svar på {url}. Har {this_pc()} fått adresse i nytt LAN?")
        return
    open_url(url)
    info_line("Nettleseren advarer om sertifikatet første gang. Det er normalt "
              "(selvsignert sertifikat) – velg å fortsette.")


# ───────────────────────────── main ─────────────────────────────

def main() -> None:
    data_dir = default_data_dir()
    ap = argparse.ArgumentParser(description="FortiGate førstegangsoppsett (FortiOS 7.6)")
    ap.add_argument("--host", default="192.168.1.99")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--defaults", default=str(data_dir / "site_defaults.yaml"))
    ap.add_argument("--dry-run", action="store_true", help="les boksen og vis config, men ikke endre noe")
    ap.add_argument("--offline", metavar="MAPPE", help="bruk lagret output i stedet for en boks (impliserer --dry-run)")
    ap.add_argument("--no-mac-alias", action="store_true", help="ikke legg midlertidig IP på maskinen automatisk")
    ap.add_argument("--quiet", action="store_true", help="ikke skriv generert config til skjermen")
    args = ap.parse_args()

    ev = CLIEvents()
    say()
    box(["FortiGate førstegangsoppsett",
         "40F · 60F · 50G · 70G · 120G  ·  FortiOS 7.6"])
    try:
        load_env_file(data_dir / "secrets.env")
        d = load_defaults(Path(args.defaults))
        pw = ""
        if args.offline:
            dev = OfflineDevice(Path(args.offline))
            args.dry_run = True
        else:
            dev, pw = connect(args.host, args.user, ev)

        raw, info = read_box(dev, args.host, ev)
        show_info(info)
        errors, warns = preflight(info)
        if errors:
            raise Abort(errors[0])
        if warns:
            say()
        for w in warns:
            warn(w)
        if info["model"] not in SUPPORTED_MODELS:
            if not ask_yes("Fortsette likevel?", False):
                raise Abort("Avbrutt.")

        answers = interview(info, d)
        plan = make_plan(dev, info, raw, d, answers, data_dir)
        ctx = plan.ctx

        if plan.refs:
            header("Referanser til WAN-portene")
            warn("SD-WAN-medlemmer kan ikke være i bruk andre steder. Fant:")
            for p, lines in plan.refs.items():
                for ln in lines:
                    say(DIM(f"      {p}: {ln}"))
            if not ask_yes("Fortsette likevel? (steget vil sannsynligvis feile)", False):
                raise Abort("Avbrutt. Fjern referansene og kjør på nytt.")

        show_summary(ctx)
        say()
        try:
            shown = plan.out_dir.relative_to(data_dir)
        except ValueError:
            shown = plan.out_dir
        info_line(f"Generert CLI lagret i {DIM(str(shown) + '/config.txt')}")
        show_cli = args.dry_run and not args.quiet
        if not args.dry_run and not args.quiet:
            show_cli = ask_yes("Vise generert CLI før kjøring?", False)
        if show_cli:
            header("Generert CLI (passord maskert)")
            say(DIM(plan.cfg_text))

        if args.dry_run:
            say()
            ok("Dry-run: ingenting er sendt til boksen.")
            return

        say()
        if input(f"  {CYAN('?')} {BOLD('Skriv ja for å konfigurere boksen')}: ").strip().lower() != "ja":
            raise Abort("Avbrutt. Ingenting er endret.")
        header("Konfigurerer")
        opts = RunOptions(host=args.host, user=args.user, mac_alias=not args.no_mac_alias)
        try:
            status = execute(dev, plan, opts, pw, ev, admin_runner(gui=False))
        except Exception as e:
            say()
            fail(str(e))
            if isinstance(e, ApplyError):
                say(DIM("    Svar fra FortiGaten:"))
                say("    " + LOG.mask(e.output.strip()).replace("\n", "\n    "))
            say()
            box(["Endringene frem til feilen er lagret på boksen.",
                 "Rett feilen og kjør scriptet på nytt, eller start helt",
                 "på nytt med 'execute factoryreset' på boksen."], YELLOW)
            raise SystemExit(1)
        show_status(status)
        say()
        box([f"✔  Ferdig – {ctx['hostname']} er konfigurert og lagret",
             f"   GUI:  {gui_url(ctx)}",
             f"   SSH:  ssh {args.user}@{ctx['lan']['ip']}"], GREEN)
        open_gui(ctx)
    except Abort as e:
        say()
        fail(str(e))
        sys.exit(1)
    except KeyboardInterrupt:
        say()
        fail("Avbrutt.")
        sys.exit(130)


if __name__ == "__main__":
    main()
