#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FortiGate førstegangsoppsett for FortiOS 7.6 (40F, 60F, 50G, 70G, 120G).

Kobler til en fabrikknullstilt boks på 192.168.1.99, leser hva den har,
spør interaktivt om hostname / WAN / LAN, henter SNMPv3, trusted hosts,
timezone og admin-timeout fra site_defaults.yaml, og kjører konfigurasjonen
og lagrer underveis (vanlig cfg-save automatic).

LAN-IP byttes helt til slutt. Før det legger scriptet en midlertidig IP-alias
i det nye LAN-nettet på Mac-ens nettverkskort, slik at det kan koble seg på
igjen etter byttet, verifisere og lagre.

Bruk:
    python3 setup.py                                # live mot 192.168.1.99
    python3 setup.py --dry-run                      # les boksen, vis config, ikke endre
    python3 setup.py --offline tests/fixtures/60F   # uten boks (test av flyt og maler)
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import ipaddress
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path

try:
    import yaml
    from jinja2 import Environment, FileSystemLoader, StrictUndefined
except ImportError as e:  # pragma: no cover
    sys.exit(f"Mangler Python-pakke ({e.name}). Kjør: pip install -r requirements.txt")

BASE = Path(__file__).resolve().parent
SUPPORTED_MODELS = ("40F", "41F", "60F", "61F", "50G", "51G", "70G", "71G", "120G", "121G")
# interfaces som vises i "get system interface physical" men ikke er vanlige porter
NON_PORTS = {"modem"}
MIN_VERSION = (7, 6, 0)

READ_CMDS = {
    "status": "get system status",
    "global": "show system global",
    "interface": "show system interface",
    "physical": "get system interface physical",
    "vswitch": "show system virtual-switch",
    "dhcp": "show system dhcp server",
    "policy": "show firewall policy",
    "sdwan": "show system sdwan",
}

# Linjer i CLI-output som betyr at en kommando feilet
ERROR_RE = re.compile(
    r"command parse error|Command fail|value parse error|entry not found|"
    r"node_check_object fail|Unknown action|not found in datasource|"
    r"invalid (?:ip|netmask|value|input)|MUST be set",
    re.I,
)
READ_ERROR_RE = re.compile(r"command parse error|Unknown action", re.I)


# ───────────────────────────── hjelpere ─────────────────────────────

class Abort(Exception):
    """Stopp med en forklarende melding."""


class ApplyError(Exception):
    def __init__(self, step: str, line: str, output: str):
        super().__init__(f"Steg '{step}' feilet på linjen: {line}")
        self.step, self.line, self.output = step, line, output


# Farger og stil (slås av ved NO_COLOR eller når output ikke går til en terminal)
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


STEP_NAMES = {
    "system": "Hostname, tidssone og admin",
    "snmp": "SNMPv3",
    "wan": "WAN-porter og ruting",
    "policy": "Brannmur-policy LAN → Internett",
}


class Log:
    """Logg til fil, med maskering av passord."""

    def __init__(self) -> None:
        self.buf: list[str] = []
        self.path: Path | None = None
        self.secrets: set[str] = set()

    def mask(self, text: str) -> str:
        for s in sorted(self.secrets, key=len, reverse=True):
            if s:
                text = text.replace(s, "********")
        return text

    def write(self, text: str) -> None:
        text = self.mask(text)
        if self.path:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(text + "\n")
        else:
            self.buf.append(text)

    def attach(self, path: Path) -> None:
        self.path = path
        for t in self.buf:
            self.write(t)
        self.buf.clear()


LOG = Log()


# ───────────────────────── parsing av FortiOS-config ─────────────────────────

def parse_fos(text: str) -> dict:
    """Parser 'show'-output til nøstede dicts.

    config X  -> node["_config"]["X"]
    edit "n"  -> node["_edit"]["n"]
    set k v.. -> node["k"] = [v, ...]
    """
    root: dict = {}
    stack = [root]
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            tok = shlex.split(line)
        except ValueError:
            tok = line.split()
        if not tok:
            continue
        cmd = tok[0]
        if cmd == "config" and len(tok) > 1:
            node: dict = {}
            stack[-1].setdefault("_config", {})[" ".join(tok[1:])] = node
            stack.append(node)
        elif cmd == "edit" and len(tok) > 1:
            node = {}
            stack[-1].setdefault("_edit", {})[tok[1]] = node
            stack.append(node)
        elif cmd == "set" and len(tok) > 1:
            stack[-1][tok[1]] = tok[2:]
        elif cmd in ("next", "end") and len(stack) > 1:
            stack.pop()
    return root


def section(node: dict, path: str) -> dict:
    return node.get("_config", {}).get(path, {})


def edits(node: dict, path: str) -> dict:
    return section(node, path).get("_edit", {})


def first(node: dict, key: str, default=None):
    v = node.get(key)
    return v[0] if v else default


def iface_ip(node: dict):
    v = node.get("ip")
    if not v or v[0] == "0.0.0.0":
        return None
    try:
        if "/" in v[0]:
            return ipaddress.ip_interface(v[0])
        if len(v) > 1:
            return ipaddress.ip_interface(f"{v[0]}/{v[1]}")
    except ValueError:
        return None
    return None


def analyze(raw: dict, host: str) -> dict:
    st = raw["status"]
    m = re.search(r"Version:\s*FortiGate-(\S+)\s+v(\d+)\.(\d+)\.(\d+)", st)
    if not m:
        raise Abort("Fant ikke modell/versjon i 'get system status'.")

    def rx(pattern, default=""):
        mm = re.search(pattern, st)
        return mm.group(1) if mm else default

    info: dict = {
        "model": m.group(1),
        "version": tuple(int(x) for x in m.group(2, 3, 4)),
        "serial": rx(r"Serial-Number:\s*(\S+)", "ukjent"),
        "hostname": rx(r"Hostname:\s*(\S+)"),
        "vdom": rx(r"Virtual domain configuration:\s*(\S+)", "disable"),
    }
    glob = section(parse_fos(raw["global"]), "system global")
    info["cfg_save"] = first(glob, "cfg-save", "automatic")

    ifaces = edits(parse_fos(raw["interface"]), "system interface")
    info["ifaces"] = ifaces
    info["physical"] = re.findall(r"==\[([^\]]+)\]", raw["physical"])

    vsw = edits(parse_fos(raw["vswitch"]), "system virtual-switch")
    info["vswitch_of"] = {p: vs for vs, n in vsw.items() for p in edits(n, "port")}
    info["aggregate_of"] = {
        mem: name for name, n in ifaces.items() for mem in n.get("member", [])
    }
    info["dhcp"] = edits(parse_fos(raw["dhcp"]), "system dhcp server")
    info["policies"] = edits(parse_fos(raw["policy"]), "firewall policy")
    info["policy_names"] = {first(p, "name", "") for p in info["policies"].values()}
    sd = section(parse_fos(raw["sdwan"]), "system sdwan")
    info["sdwan_status"] = first(sd, "status", "disable")
    info["sdwan_members"] = edits(sd, "members")
    info["sdwan_healthchecks"] = list(edits(sd, "health-check"))

    host_ip = ipaddress.ip_address(host)
    info["mgmt_iface"] = None
    info["mgmt_net"] = ipaddress.ip_network(f"{host}/24", strict=False)
    for name, node in ifaces.items():
        ipi = iface_ip(node)
        if ipi and ipi.ip == host_ip:
            info["mgmt_iface"], info["mgmt_net"] = name, ipi.network
            break
    return info


# ───────────────────────────── spørsmål ─────────────────────────────

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


def v_alias(s: str) -> str:
    if s == "-":
        return ""
    if len(s) > 25 or '"' in s or "\\" in s:
        raise ValueError('Maks 25 tegn, uten " eller \\')
    return s


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


def v_hostname(s: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,34}", s):
        raise ValueError("Bruk bokstaver, tall, - _ . (maks 35 tegn)")
    return s


def v_iface_ip(s: str, label: str):
    try:
        ipi = ipaddress.ip_interface(s)
    except ValueError:
        raise ValueError(f"Ugyldig {label}, bruk f.eks. 10.20.30.1/24")
    if "/" not in s:
        raise ValueError("Ta med prefiks, f.eks. /24")
    if ipi.version != 4:
        raise ValueError("Kun IPv4 støttes")
    net = ipi.network
    if net.prefixlen > 30 or ipi.ip in (net.network_address, net.broadcast_address):
        raise ValueError("Adressen må være en vertsadresse i et nett på /30 eller større")
    return ipi


def v_ip(s: str) -> ipaddress.IPv4Address:
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        raise ValueError("Ugyldig IP-adresse")
    if ip.version != 4:
        raise ValueError("Kun IPv4 støttes")
    return ip


def v_choice(options: dict):
    def _v(s: str):
        if s not in options:
            raise ValueError(f"Velg en av: {', '.join(options)}")
        return options[s]
    return _v


def v_dns(s: str) -> list[str]:
    items = [x.strip() for x in s.split(",") if x.strip()]
    if not 1 <= len(items) <= 4:
        raise ValueError("Oppgi 1–4 DNS-servere")
    return [str(v_ip(x)) for x in items]


def default_range(net, gw, offsets):
    first_h = int(net.network_address) + 1
    last_h = int(net.broadcast_address) - 1
    start = int(net.network_address) + int(offsets[0])
    end = int(net.network_address) + int(offsets[1])
    if not (first_h <= start <= end <= last_h - 1):
        start, end = first_h, last_h - 1          # siste adresse holdes av til Mac-alias
    g = int(gw)
    if start <= g <= end:
        if g == start:
            start += 1
        elif g == end:
            end -= 1
        else:
            start = g + 1
    return ipaddress.ip_address(start), ipaddress.ip_address(end)


def pick_alias(net, gw, start, end):
    """Velg en ledig adresse i nytt LAN for Mac-ens midlertidige alias."""
    a = int(net.broadcast_address) - 1
    while a > int(net.network_address):
        ip = ipaddress.ip_address(a)
        in_range = start is not None and int(start) <= a <= int(end)
        if ip != gw and not in_range:
            return ip
        a -= 1
    raise Abort("Fant ingen ledig adresse til Mac-alias i LAN-nettet.")


# ───────────────────────────── site_defaults.yaml ─────────────────────────────

def expand_env(o):
    if isinstance(o, str):
        v = os.path.expandvars(o)
        if re.search(r"\$\{?[A-Za-z_]", v):
            raise Abort(f"Miljøvariabel ikke satt: {o}")
        return v
    if isinstance(o, list):
        return [expand_env(x) for x in o]
    if isinstance(o, dict):
        return {k: expand_env(v) for k, v in o.items()}
    return o


def _check_cli_string(s: str, what: str) -> str:
    if '"' in s or "\\" in s or "\n" in s:
        raise Abort(f'{what} kan ikke inneholde ", \\ eller linjeskift')
    return s


def load_defaults(path: Path) -> dict:
    if not path.exists():
        example = path.with_name("site_defaults.example.yaml")
        if example.exists():
            raise Abort(f"Finner ikke {path.name}. Kopier {example.name} til {path.name} og fyll inn "
                        "dine verdier (trusted hosts, SNMP osv.).")
        raise Abort(f"Finner ikke {path}")
    d = expand_env(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    sysd, admin = d.get("system", {}), d.get("admin", {})
    lan, wan = d.get("lan", {}), d.get("wan", {})
    snmp = d.get("snmp", {}) or {}

    https_port = int(admin.get("https_port", 443))
    if not 1 <= https_port <= 65535 or https_port in (22, 80, 541):
        raise Abort("admin.https_port må være 1–65535 og ikke 22, 80 eller 541")

    timeout = int(sysd.get("admin_timeout", 15))
    if not 1 <= timeout <= 480:
        raise Abort("system.admin_timeout må være 1–480 minutter")

    out = {
        "timezone": _check_cli_string(str(sysd.get("timezone", "Europe/Oslo")), "timezone"),
        "admin_timeout": timeout,
        "admin_user": str(admin.get("user", "admin")),
        "admin_https_port": https_port,
        "admin_tls": _tls_versions(admin.get("tls_versions", ["tlsv1-2", "tlsv1-3"])),
        "trusted_hosts": [ipaddress.ip_network(x, strict=False) for x in admin.get("trusted_hosts", [])],
        "trust_lan_subnet": bool(admin.get("include_lan_subnet", True)),
        "wan_allowaccess": str(wan.get("allowaccess", "ping")),
        "remove_default_hc": bool(wan.get("remove_default_health_checks", True)),
        "hc": {
            "name": str(wan.get("health_check", {}).get("name", "HC_Internet")),
            "servers": [str(v_ip(x)) for x in wan.get("health_check", {}).get("servers", ["1.1.1.1", "8.8.8.8"])],
            "protocol": str(wan.get("health_check", {}).get("protocol", "ping")),
            "latency": int(wan.get("health_check", {}).get("latency_ms", 250)),
            "jitter": int(wan.get("health_check", {}).get("jitter_ms", 50)),
            "loss": int(wan.get("health_check", {}).get("packetloss_pct", 5)),
        },
        "lan": {
            "dns": [str(v_ip(x)) for x in lan.get("dns_servers", ["1.1.1.1", "8.8.8.8"])],
            "dhcp_offsets": lan.get("dhcp_range_offsets", [100, 200]),
            "lease_time": _lease_time(lan.get("dhcp_lease_time", 28800)),
            "allowaccess": str(lan.get("allowaccess", "ping https ssh")),
        },
        "policy_name": _check_cli_string(str(d.get("policy", {}).get("name", "LAN-to-Internet")), "policy.name"),
        "snmp": normalize_snmp(snmp),
    }
    if len(out["trusted_hosts"]) > 8:
        raise Abort("Maks 8 trusted hosts i YAML (scriptet trenger 2 plasser under oppsettet)")
    if len(out["hc"]["servers"]) < 1:
        raise Abort("wan.health_check.servers må ha minst én adresse")
    return out


TLS_VERSIONS = {"tlsv1-1", "tlsv1-2", "tlsv1-3"}


def _tls_versions(v) -> str:
    items = [v] if isinstance(v, str) else list(v)
    bad = [x for x in items if x not in TLS_VERSIONS]
    if not items or bad:
        raise Abort(f"admin.tls_versions må være en eller flere av {sorted(TLS_VERSIONS)}")
    return " ".join(items)


def _lease_time(v) -> int:
    v = int(v)
    if not 300 <= v <= 8640000:
        raise Abort("lan.dhcp_lease_time må være 300–8640000 sekunder")
    return v


AUTH_PROTOS = {"md5", "sha", "sha224", "sha256", "sha384", "sha512"}
PRIV_PROTOS = {"aes", "des", "aes256", "aes256cisco"}
SEC_LEVELS = {"no-auth-no-priv", "auth-no-priv", "auth-priv"}


def normalize_snmp(s: dict) -> dict:
    sysinfo = s.get("sysinfo", {}) or {}
    for k in ("description", "contact", "location"):
        sysinfo[k] = _check_cli_string(str(sysinfo.get(k, "") or ""), f"snmp.sysinfo.{k}")
    users = []
    for u in s.get("users", []) or []:
        name = _check_cli_string(str(u["name"]), "snmp-brukernavn")
        lvl = u.get("security_level", "auth-priv")
        if lvl not in SEC_LEVELS:
            raise Abort(f"SNMP {name}: security_level må være en av {sorted(SEC_LEVELS)}")
        nu = {"name": name, "security_level": lvl,
              "auth_proto": u.get("auth_proto", "sha256"), "priv_proto": u.get("priv_proto", "aes256"),
              "auth_pwd": str(u.get("auth_pwd", "")), "priv_pwd": str(u.get("priv_pwd", "")),
              "notify_hosts": [str(v_ip(x)) for x in u.get("notify_hosts", []) or []]}
        if lvl != "no-auth-no-priv":
            if nu["auth_proto"] not in AUTH_PROTOS:
                raise Abort(f"SNMP {name}: auth_proto må være en av {sorted(AUTH_PROTOS)}")
            if not 8 <= len(nu["auth_pwd"]) <= 128:
                raise Abort(f"SNMP {name}: auth_pwd må være 8–128 tegn")
            _check_cli_string(nu["auth_pwd"], f"SNMP {name} auth_pwd")
            LOG.secrets.add(nu["auth_pwd"])
        if lvl == "auth-priv":
            if nu["priv_proto"] not in PRIV_PROTOS:
                raise Abort(f"SNMP {name}: priv_proto må være en av {sorted(PRIV_PROTOS)}")
            if not 8 <= len(nu["priv_pwd"]) <= 128:
                raise Abort(f"SNMP {name}: priv_pwd må være 8–128 tegn")
            _check_cli_string(nu["priv_pwd"], f"SNMP {name} priv_pwd")
            LOG.secrets.add(nu["priv_pwd"])
        users.append(nu)
    return {
        "enabled": bool(users) or any(sysinfo.values()),
        "sysinfo": sysinfo,
        "users": users,
        "allow_on_lan": bool(s.get("allow_on_lan", True)),
    }


# ───────────────────────────── enheter ─────────────────────────────

class LiveDevice:
    def __init__(self, host: str, user: str, password: str):
        try:
            from netmiko import ConnectHandler
        except ImportError:
            raise Abort("Netmiko mangler. Kjør: pip install -r requirements.txt")
        self.host = host
        self.conn = ConnectHandler(device_type="fortinet", host=host, username=user,
                                   password=password, conn_timeout=20, fast_cli=False)

    def read(self, cmd: str) -> str:
        out = self.conn.send_command(cmd, read_timeout=90)
        LOG.write(f"\n### {self.host}: {cmd}\n{out}")
        return "" if READ_ERROR_RE.search(out) else out

    def line(self, cmd: str) -> str:
        out = self.conn.send_command_timing(cmd, strip_prompt=False, strip_command=False,
                                            last_read=0.4, read_timeout=60)
        LOG.write(out)
        return out

    def wait_for(self, cmd: str, pattern, timeout: float) -> str:
        """Send en kommando og les til mønsteret dukker opp (eller timeout)."""
        LOG.write(cmd)
        self.conn.write_channel(cmd + "\n")
        buf, end = "", time.time() + timeout
        while time.time() < end:
            buf += self.conn.read_channel()
            if pattern.search(buf):
                time.sleep(0.5)
                buf += self.conn.read_channel()   # ta med prompten etterpå
                break
            time.sleep(0.5)
        LOG.write(buf)
        return buf

    def refresh_prompt(self) -> None:
        self.conn.set_base_prompt()

    def commit_and_drop(self, lines: list[str]) -> None:
        """Send de siste linjene rolig, uten å vente på prompt (sesjonen dør underveis), og koble fra."""
        for ln in lines:
            LOG.write(ln)
            try:
                self.conn.write_channel(ln + "\n")
                time.sleep(2.5)
                out = self.conn.read_channel()
            except Exception:
                break                      # sesjonen døde, som forventet
            LOG.write(out)
            # FortiOS 7.6: "N admin session(s) are currently connected on this interface.
            #              Are you sure you want to continue? (y/n)"
            if "(y/n)" in out:
                LOG.write("y")
                try:
                    self.conn.write_channel("y\n")
                    time.sleep(2.5)
                    out = self.conn.read_channel()
                except Exception:
                    break                  # IP-en er aktivert og sesjonen døde
                LOG.write(out)
            if ERROR_RE.search(out) or re.search(r"[Ee]rror|[Ff]ail|[Ii]nvalid|conflict|overlap", out):
                raise ApplyError("lan-ip (commit)", ln, out)
        self.drop()

    def alive(self) -> bool:
        try:
            return self.conn.is_alive()
        except Exception:
            return False

    def drop(self) -> None:
        for c in (getattr(self.conn, "remote_conn", None), getattr(self.conn, "remote_conn_pre", None)):
            try:
                c.close()
            except Exception:
                pass

    def close(self) -> None:
        try:
            self.conn.disconnect()
        except Exception:
            self.drop()


class OfflineDevice:
    """Leser lagret output fra en mappe (status.txt, interface.txt, ...)."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.by_cmd = {c: k for k, c in READ_CMDS.items()}

    def read(self, cmd: str) -> str:
        key = self.by_cmd.get(cmd)
        if key is None:
            return ""
        p = self.folder / f"{key}.txt"
        return p.read_text(encoding="utf-8") if p.exists() else ""


def first_login(host: str, user: str) -> str | None:
    """Logg inn med tomt passord og håndter tvunget passordbytte.

    Returnerer nytt passord, eller None hvis boksen allerede har passord.
    """
    import paramiko

    t = paramiko.Transport((host, 22))
    t.start_client(timeout=20)
    authed = False
    allowed = ["password", "keyboard-interactive"]
    try:
        t.auth_none(user)
        authed = t.is_authenticated()
    except paramiko.BadAuthenticationType as e:
        allowed = list(e.allowed_types)
    except paramiko.AuthenticationException:
        pass
    for meth in allowed:
        if authed:
            break
        try:
            if meth == "password":
                t.auth_password(user, "")
            elif meth == "keyboard-interactive":
                t.auth_interactive(user, lambda title, instr, prompts: [""] * len(prompts))
            authed = t.is_authenticated()
        except paramiko.AuthenticationException:
            pass
    if not authed:
        t.close()
        return None

    chan = t.open_session()
    chan.get_pty(width=200)
    chan.invoke_shell()
    buf = _read_until(chan, r"New Password:|#\s*$", 20)
    if "New Password" not in buf:
        t.close()
        raise Abort("Boksen godtok tomt passord uten å kreve bytte. Sett admin-passord "
                    "manuelt i GUI/CLI og kjør scriptet på nytt.")
    say()
    info_line("Fabrikknullstilt boks – velg nytt admin-passord for FortiGaten")
    while True:
        pw = getpass.getpass(f"  {CYAN('?')} {BOLD('Nytt FortiGate-passord')}: ")
        if pw and pw == getpass.getpass(f"  {CYAN('?')} {BOLD('Gjenta passord')}: "):
            break
        fail("Passordene var tomme eller like ikke")
    chan.send(pw + "\n")
    _read_until(chan, r"Confirm Password:", 15)
    chan.send(pw + "\n")
    buf = _read_until(chan, r"New Password:|#\s*$|[Ee]rror|[Ii]nvalid", 20)
    t.close()
    if "New Password" in buf or re.search(r"[Ee]rror|[Ii]nvalid", buf):
        raise Abort("FortiGaten avviste passordet (passordpolicy?). Svar fra boksen:\n" + buf[-400:])
    ok("Admin-passord satt")
    return pw


def _read_until(chan, pattern: str, timeout: float) -> str:
    buf, end = "", time.time() + timeout
    while time.time() < end:
        if chan.recv_ready():
            buf += chan.recv(65535).decode("utf-8", "replace")
            if re.search(pattern, buf):
                return buf
        else:
            time.sleep(0.2)
    return buf


def wait_port(host: str, port: int, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=3):
                return True
        except OSError:
            time.sleep(2)
    return False


# ───────────────────────────── Mac ─────────────────────────────

def mac_iface_for(host: str) -> str | None:
    out = subprocess.run(["route", "-n", "get", host], capture_output=True, text=True).stdout
    m = re.search(r"interface:\s*(\S+)", out)
    return m.group(1) if m else None


def sudo(*args: str) -> None:
    LOG.write("sudo " + " ".join(args))
    subprocess.run(["sudo", *args], check=True)


# ───────────────────────────── intervju ─────────────────────────────

def port_label(info: dict, p: str) -> str:
    node = info["ifaces"].get(p, {})
    tags = []
    if first(node, "role") == "wan":
        tags.append("wan")
    if first(node, "mode") == "dhcp":
        tags.append("dhcp")
    ipi = iface_ip(node)
    if ipi:
        tags.append(str(ipi))
    if p in info["vswitch_of"]:
        tags.append(f"i switch '{info['vswitch_of'][p]}', flyttes ut")
    if p in info["aggregate_of"]:
        tags.append(f"medlem av '{info['aggregate_of'][p]}', kan ikke brukes")
    return ", ".join(tags)


def wan_candidates(info: dict, lan_iface: str) -> list[str]:
    ports = [p for p in dict.fromkeys(info["physical"] + list(info["vswitch_of"])) if p not in NON_PORTS]
    excl = {lan_iface, info["mgmt_iface"]}
    return [p for p in ports if p not in excl]


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
                f"Pass på at Mac-en IKKE står i denne porten.")
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


# ───────────────────────────── kontekst og maler ─────────────────────────────

def th_fmt(net) -> str:
    return f"{net.network_address} {net.netmask}"


def build_ctx(info: dict, d: dict, a: dict) -> dict:
    lan_ip = a["lan_ip"]
    net = lan_ip.network

    final_th = list(dict.fromkeys(d["trusted_hosts"] + ([net] if d["trust_lan_subnet"] else [])))
    prov_th = list(dict.fromkeys(final_th + [net, info["mgmt_net"]]))
    if len(prov_th) > 10:
        raise Abort("For mange trusted hosts (maks 10 inkludert LAN og provisjoneringsnett)")

    existing = [k for k, n in info["dhcp"].items() if first(n, "interface") == a["lan_iface"]]
    ids = [int(k) for k in info["dhcp"] if k.isdigit()]
    dhcp_id = existing[0] if existing else str(max(ids + [0]) + 1)

    lan_node = info["ifaces"].get(a["lan_iface"], {})
    access = lan_node.get("allowaccess") or d["lan"]["allowaccess"].split()
    if d["snmp"]["enabled"] and d["snmp"]["allow_on_lan"] and d["snmp"]["users"]:
        access = list(access) + ["snmp"]
    access = list(dict.fromkeys(access))

    wans = []
    for w in a["wans"]:
        p = w["port"]
        wans.append({
            "port": p,
            "mode": w["mode"],
            "alias": w["alias"],
            "ip": str(w["ipi"].ip) if w["mode"] == "static" else None,
            "mask": str(w["ipi"].netmask) if w["mode"] == "static" else None,
            "gateway": str(w["gateway"]) if w["mode"] == "static" else None,
            "release_from": info["vswitch_of"].get(p),
            "dhcp_delete": [k for k, n in info["dhcp"].items() if first(n, "interface") == p],
        })

    # Default-policyen (internal -> wan1) blokkerer SD-WAN og blir duplikat av vår egen.
    # Policies fra LAN kun mot WAN-porter erstattes av den nye, navngitte policyen.
    wan_like = {w["port"] for w in wans} | {n for n, nd in info["ifaces"].items() if first(nd, "role") == "wan"}
    replace_ids = [pid for pid, pn in info["policies"].items()
                   if pn.get("srcintf") == [a["lan_iface"]] and pn.get("dstintf")
                   and set(pn["dstintf"]) <= wan_like and first(pn, "name", "") != d["policy_name"]]

    start = a.get("dhcp_start")
    end = a.get("dhcp_end")
    same_iface = a["lan_iface"] == info["mgmt_iface"]
    return {
        "fix_cfg_save": info["cfg_save"] != "automatic",
        "hostname": a["hostname"],
        "timezone": d["timezone"],
        "admin_timeout": d["admin_timeout"],
        "admin_user": d["admin_user"],
        "admin_https_port": d["admin_https_port"],
        "admin_tls": d["admin_tls"],
        "delete_hc": [h for h in info["sdwan_healthchecks"] if h.startswith("Default_")]
                     if d["remove_default_hc"] else [],
        "trusthosts": [th_fmt(n) for n in prov_th],
        "final_trusthosts": [th_fmt(n) for n in final_th],
        "snmp": d["snmp"],
        "wan_mode": a["wan_mode"],
        "wans": wans,
        "replace_policies": replace_ids,
        "replace_policies_desc": [
            f"{pid}: {' '.join(info['policies'][pid]['srcintf'])} -> {' '.join(info['policies'][pid]['dstintf'])}"
            for pid in replace_ids],
        "wan_allowaccess": d["wan_allowaccess"],
        "hc": d["hc"],
        "policy": {
            "name": d["policy_name"],
            "dstintf": "virtual-wan-link" if a["wan_mode"] == "sdwan" else wans[0]["port"],
            "exists": d["policy_name"] in info["policy_names"],
        },
        "lan": {
            "iface": a["lan_iface"],
            "ip": str(lan_ip.ip),
            "mask": str(lan_ip.netmask),
            "cidr": str(lan_ip),
            "allowaccess": " ".join(access),
            "dhcp_enabled": a["dhcp_enabled"],
            "lease_time": d["lan"]["lease_time"],
            "dhcp_id": dhcp_id,
            "dhcp_id_existing": existing[0] if existing else None,
            "dhcp_start": str(start) if start else None,
            "dhcp_end": str(end) if end else None,
            "dns": a.get("dns", []),
            "same_iface": same_iface,
            "alias_ip": str(pick_alias(net, lan_ip.ip, start, end)),
        },
    }


class Step:
    def __init__(self, name: str, template: str, text: str, final: bool = False):
        self.name, self.template, self.text, self.final = name, template, text, final

    @property
    def lines(self) -> list[str]:
        return [ln.strip() for ln in self.text.splitlines() if ln.strip()]


def make_env() -> Environment:
    return Environment(loader=FileSystemLoader(BASE / "templates"), undefined=StrictUndefined,
                       trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)


def render(env: Environment, template: str, ctx: dict, **extra) -> str:
    text = env.get_template(template).render(**ctx, **extra)
    return "\n".join(ln.rstrip() for ln in text.splitlines() if ln.strip()) + "\n"


def build_steps(env: Environment, ctx: dict) -> list[Step]:
    steps = [
        Step("system", "10_system.j2", render(env, "10_system.j2", ctx)),
    ]
    if ctx["snmp"]["enabled"]:
        steps.append(Step("snmp", "20_snmp.j2", render(env, "20_snmp.j2", ctx)))
    steps.append(Step("wan", "30_wan.j2", render(env, "30_wan.j2", ctx)))
    if not ctx["policy"]["exists"]:
        steps.append(Step("policy", "40_policy.j2", render(env, "40_policy.j2", ctx)))
    steps.append(Step("lan", "50_lan.j2", render(env, "50_lan.j2", ctx, part="all"), final=True))
    return steps


# ───────────────────────────── utførelse ─────────────────────────────

def run_block(dev, name: str, lines: list[str]) -> None:
    LOG.write(f"\n### steg: {name}")
    for ln in lines:
        out = dev.line(ln)
        if ERROR_RE.search(out):
            raise ApplyError(name, ln, out)


def check_refs(dev, ctx: dict) -> dict:
    """SD-WAN-medlemmer kan ikke være referert fra policy/rute o.l."""
    found = {}
    if ctx["wan_mode"] != "sdwan":
        return found
    handled = ("system.virtual-switch", "system.dhcp.server")
    replaced = set(ctx["replace_policies"])

    def is_handled(ln: str) -> bool:
        if any(h in ln for h in handled):
            return True
        m = re.search(r"firewall\.policy\D*(\d+)", ln)
        return bool(m and m.group(1) in replaced)

    for w in ctx["wans"]:
        out = dev.read(f"diagnose sys checkused system.interface.name {w['port']}")
        rest = [ln.strip() for ln in out.splitlines() if "used by" in ln and not is_handled(ln)]
        if rest:
            found[w["port"]] = rest
    return found


def verify(dev, ctx: dict) -> tuple[bool, bool]:
    lan = ctx["lan"]
    ifs = edits(parse_fos(dev.read("show system interface")), "system interface")
    got = iface_ip(ifs.get(lan["iface"], {}))
    ok_ip = bool(got) and str(got) == lan["cidr"]

    dh = edits(parse_fos(dev.read("show system dhcp server")), "system dhcp server")
    node = dh.get(lan["dhcp_id"])
    if lan["dhcp_enabled"]:
        rng = edits(node or {}, "ip-range").get("1", {})
        ok_dhcp = bool(node) and first(node, "default-gateway") == lan["ip"] \
            and first(rng, "start-ip") == lan["dhcp_start"] and first(rng, "end-ip") == lan["dhcp_end"]
    else:
        ok_dhcp = lan["dhcp_id_existing"] is None or lan["dhcp_id_existing"] not in dh
    return ok_ip, ok_dhcp


SAVED_RE = re.compile(r"config saved|no config to be saved", re.I)


def save_config(dev) -> None:
    """Med cfg-save automatic er alt lagret allerede. Sikre at modusen faktisk er automatic."""
    glob = section(parse_fos(dev.read("show system global")), "system global")
    if first(glob, "cfg-save", "automatic") != "automatic":
        run_block(dev, "cfg-save automatic", ["config system global", "set cfg-save automatic", "end"])
        dev.wait_for("execute cfg save", SAVED_RE, timeout=60)


def execute(dev, ctx: dict, steps: list[Step], env: Environment, args, pw: str) -> None:
    lan = ctx["lan"]
    mac = None
    if lan["same_iface"]:
        if args.no_mac_alias or sys.platform != "darwin":
            warn(f"Etter IP-byttet må PC-en nå {lan['ip']}. Gi den f.eks. {lan['alias_ip']}/"
                 f"{a_prefix(lan)} som ekstra IP før du fortsetter.")
            input("  Trykk Enter når klar ...")
        else:
            mif = mac_iface_for(args.host)
            if not mif:
                raise Abort(f"Fant ikke hvilket nettverkskort på Mac-en som når {args.host}")
            my_ip = mac_ip(mif)
            new_net = ipaddress.ip_network(f"{lan['ip']}/{lan['mask']}", strict=False)
            if my_ip and ipaddress.ip_address(my_ip) in new_net:
                ok(f"Mac-en ({my_ip}) står allerede i {new_net}")
            else:
                info_line(f"Mac-en får midlertidig IP {BOLD(lan['alias_ip'])} på {mif} for å nå nytt LAN")
                warn("macOS spør nå etter passordet til MAC-BRUKEREN din (ikke FortiGate)")
                subprocess.run(["sudo", "-v", "-p", "  Mac-passord for %u: "], check=True)
                sudo("ifconfig", mif, "alias", lan["alias_ip"], "netmask", lan["mask"])
                mac = (mif, lan["alias_ip"])
                ok("Midlertidig IP lagt til")
    say()

    try:
        for st in steps:
            if st.final and lan["same_iface"]:
                # Linje for linje med feilsjekk. Bare 'next'/'end' (som aktiverer IP-en) sendes blindt.
                iface_lines = Step("lan-ip", "50_lan.j2", render(env, "50_lan.j2", ctx, part="iface")).lines
                commit = iface_lines.index("next")
                with Progress(f"LAN-IP {lan['cidr']} på {lan['iface']}"):
                    run_block(dev, "lan-ip", iface_lines[:commit])
                    dev.commit_and_drop(iface_lines[commit:])
                info_line("SSH-forbindelsen brøt, som forventet ved IP-bytte")
                with Progress(f"Kobler til igjen på {lan['ip']} (inntil 3 min)"):
                    if not wait_port(lan["ip"], 22, 180):
                        say()
                        diagnose_old(args, pw, lan)
                        raise Abort(f"Fikk ikke kontakt med {lan['ip']} innen 3 minutter.")
                    time.sleep(3)
                    dev = LiveDevice(lan["ip"], args.user, pw)
                dhcp_lines = Step("dhcp", "50_lan.j2", render(env, "50_lan.j2", ctx, part="dhcp")).lines
                if dhcp_lines:
                    with Progress("DHCP-server for nytt LAN"):
                        run_block(dev, "dhcp", dhcp_lines)
            else:
                with Progress(STEP_NAMES.get(st.name, st.name)):
                    run_block(dev, st.name, st.lines)
                    if st.name == "system":
                        dev.refresh_prompt()

        with Progress("Kontrollerer LAN-IP og DHCP"):
            ok_ip, ok_dhcp = verify(dev, ctx)
            if not ok_ip:
                raise Abort("LAN-IP ble ikke som forventet.")
            if not ok_dhcp:
                raise Abort("DHCP-oppsettet ble ikke som forventet.")

        if ctx["final_trusthosts"] != ctx["trusthosts"]:
            with Progress("Endelige trusted hosts"):
                run_block(dev, "trusthosts", Step("th", "", render(
                    env, "_trusthosts.j2", ctx, hosts=ctx["final_trusthosts"])).lines)

        with Progress("Kontrollerer at alt er lagret"):
            save_config(dev)
        show_status(dev, ctx)
        dev.close()
        say()
        box([f"✔  Ferdig – {ctx['hostname']} er konfigurert og lagret",
             f"   GUI:  https://{lan['ip']}" + ("" if ctx["admin_https_port"] == 443 else f":{ctx['admin_https_port']}"),
             f"   SSH:  ssh {args.user}@{lan['ip']}"], GREEN)
    except (ApplyError, Abort, Exception) as e:
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
    finally:
        if mac:
            mif, alias = mac
            renew_mac_dhcp(mif, alias, lan)


def mac_ip(mif: str) -> str:
    return subprocess.run(["ipconfig", "getifaddr", mif], capture_output=True, text=True).stdout.strip()


def renew_mac_dhcp(mif: str, alias: str, lan: dict) -> None:
    """Fjern midlertidig IP og tving ny DHCP-lease (ny IP og ny gateway) på Mac-en."""
    try:
        sudo("ifconfig", mif, "-alias", alias)
        # 'ipconfig set DHCP' alene fornyer ikke når kortet allerede står på DHCP.
        # Bytt til BOOTP og tilbake, så slipper macOS den gamle leasen.
        sudo("ipconfig", "set", mif, "BOOTP")
        time.sleep(1)
        sudo("ipconfig", "set", mif, "DHCP")
    except subprocess.CalledProcessError as e:
        warn(f"Klarte ikke rydde nettverkskortet automatisk: {e}")
        return
    if not lan["dhcp_enabled"]:
        ok(f"Midlertidig IP {alias} fjernet")
        return
    net = ipaddress.ip_network(f"{lan['ip']}/{lan['mask']}", strict=False)
    with Progress("Mac-en henter ny DHCP-adresse"):
        end, got = time.time() + 45, ""
        while time.time() < end:
            got = mac_ip(mif)
            if got and ipaddress.ip_address(got) in net:
                break
            time.sleep(2)
    router = subprocess.run(["ipconfig", "getoption", mif, "router"],
                            capture_output=True, text=True).stdout.strip()
    if got and ipaddress.ip_address(got) in net:
        ok(f"Mac-en har nå {got}, gateway {router or '?'}")
    else:
        warn(f"Mac-en fikk ikke adresse i {net} ennå. Trekk ut og sett inn kabelen.")


def open_gui(ctx: dict) -> None:
    """Åpne FortiGate-GUI-en i standardnettleseren når den svarer."""
    port = ctx["admin_https_port"]
    url = f"https://{ctx['lan']['ip']}" + ("" if port == 443 else f":{port}")
    say()
    if not ask_yes(f"Åpne {url} i nettleseren?", True):
        return
    with Progress("Venter på at GUI-en svarer"):
        up = wait_port(ctx["lan"]["ip"], port, 60)
    if not up:
        warn(f"Får ikke svar på {url}. Har Mac-en fått adresse i nytt LAN?")
        return
    subprocess.run(["open", url])
    info_line("Nettleseren advarer om sertifikatet første gang. Det er normalt "
              "(selvsignert sertifikat) – velg å fortsette.")


def diagnose_old(args, pw: str, lan: dict) -> None:
    """Ny IP svarer ikke. Sjekk om boksen fortsatt er på gammel IP, og vis hva interfacet har."""
    if not wait_port(args.host, 22, 5):
        fail(f"Boksen svarer heller ikke på {args.host}.")
        return
    try:
        d = LiveDevice(args.host, args.user, pw)
        ifs = edits(parse_fos(d.read("show system interface")), "system interface")
        got = iface_ip(ifs.get(lan["iface"], {}))
        warn(f"Boksen svarer fortsatt på {args.host}. {lan['iface']} har IP {got}, "
             f"altså ble IP-byttet ikke utført.")
        d.close()
    except Exception as e:
        warn(f"Boksen svarer på port 22 på {args.host}, men innlogging feilet: {e}")


def a_prefix(lan: dict) -> int:
    return ipaddress.ip_network(f"0.0.0.0/{lan['mask']}").prefixlen


def show_status(dev, ctx: dict) -> None:
    routes = dev.read("get router info routing-table all")
    default = [ln for ln in routes.splitlines() if "0.0.0.0/0" in ln]
    header("Status")
    if default:
        ok("Default-rute finnes")
        for ln in default:
            say(DIM("      " + ln.strip()))
    else:
        warn("Ingen default-rute ennå – er WAN-kabelen koblet til?")
    if ctx["wan_mode"] == "sdwan":
        hc = dev.read("diagnose sys sdwan health-check")
        for w in ctx["wans"]:
            port = re.escape(w["port"])
            # 7.x: "Seq(1 wan1): state(alive), ..."  (eldre: "wan1(1): state(alive)")
            m = re.search(rf"(?:Seq\(\d+ {port}\)|{port}\(\d+\)): state\((\w+)\)", hc)
            if m and m.group(1) == "alive":
                ok(f"SD-WAN {w['port']}: oppe")
            elif m:
                warn(f"SD-WAN {w['port']}: {m.group(1)}")
            else:
                info_line(f"SD-WAN {w['port']}: ingen status ennå")


# ───────────────────────────── oversikt ─────────────────────────────

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


# ───────────────────────────── main ─────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="FortiGate førstegangsoppsett (FortiOS 7.6)")
    ap.add_argument("--host", default="192.168.1.99")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--defaults", default=str(BASE / "site_defaults.yaml"))
    ap.add_argument("--dry-run", action="store_true", help="les boksen og vis config, men ikke endre noe")
    ap.add_argument("--offline", metavar="MAPPE", help="bruk lagret output i stedet for en boks (impliserer --dry-run)")
    ap.add_argument("--no-mac-alias", action="store_true", help="ikke legg midlertidig IP på Mac-en automatisk")
    ap.add_argument("--quiet", action="store_true", help="ikke skriv generert config til skjermen")
    args = ap.parse_args()

    say()
    box(["FortiGate førstegangsoppsett",
         "40F · 60F · 50G · 70G · 120G  ·  FortiOS 7.6"])
    try:
        d = load_defaults(Path(args.defaults))
        env = make_env()
        pw = ""
        if args.offline:
            dev = OfflineDevice(Path(args.offline))
            args.dry_run = True
        else:
            with Progress(f"Kobler til {args.host}"):
                reachable = wait_port(args.host, 22, 10)
            if not reachable:
                raise Abort(f"Når ikke SSH på {args.host}. Står Mac-en på LAN-porten og har fått DHCP?")
            pw = first_login(args.host, args.user) or getpass.getpass(
                f"  {CYAN('?')} {BOLD(f'FortiGate-passord for {args.user}')}: ")
            LOG.secrets.add(pw)
            dev = LiveDevice(args.host, args.user, pw)

        with Progress("Leser oppsettet på boksen"):
            raw = {k: dev.read(c) for k, c in READ_CMDS.items()}
        info = analyze(raw, args.host)
        show_info(info)

        if info["model"] not in SUPPORTED_MODELS:
            warn(f"{info['model']} er ikke testet med disse malene.")
            if not ask_yes("Fortsette likevel?", False):
                raise Abort("Avbrutt.")
        if info["version"] < MIN_VERSION:
            raise Abort("Malene er skrevet for FortiOS 7.6. Oppgrader boksen først.")
        if info["vdom"] != "disable":
            raise Abort("Boksen har VDOM aktivert. Ikke støttet.")
        if info["sdwan_status"] == "enable" and info["sdwan_members"]:
            warn("SD-WAN er allerede konfigurert på boksen. Medlemmer kan bli overskrevet.")

        answers = interview(info, d)
        ctx = build_ctx(info, d, answers)

        refs = check_refs(dev, ctx)
        if refs:
            header("Referanser til WAN-portene")
            warn("SD-WAN-medlemmer kan ikke være i bruk andre steder. Fant:")
            for p, lines in refs.items():
                for ln in lines:
                    say(DIM(f"      {p}: {ln}"))
            if not ask_yes("Fortsette likevel? (steget vil sannsynligvis feile)", False):
                raise Abort("Avbrutt. Fjern referansene og kjør på nytt.")

        steps = build_steps(env, ctx)

        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
        out_dir = BASE / "output" / f"{ctx['hostname']}_{info['serial']}_{stamp}"
        (out_dir / "raw").mkdir(parents=True, exist_ok=True)
        for k, v in raw.items():
            (out_dir / "raw" / f"{k}.txt").write_text(v, encoding="utf-8")
        cfg_text = "".join(f"# ─── steg: {s.name} ───\n{s.text}\n" for s in steps)
        (out_dir / "config.txt").write_text(LOG.mask(cfg_text), encoding="utf-8")
        LOG.attach(out_dir / "run.log")

        show_summary(ctx)
        say()
        info_line(f"Generert CLI lagret i {DIM(str(out_dir.relative_to(BASE)) + '/config.txt')}")
        show_cli = args.dry_run and not args.quiet
        if not args.dry_run and not args.quiet:
            show_cli = ask_yes("Vise generert CLI før kjøring?", False)
        if show_cli:
            header("Generert CLI (passord maskert)")
            say(DIM(LOG.mask(cfg_text)))

        if args.dry_run:
            say()
            ok("Dry-run: ingenting er sendt til boksen.")
            return

        say()
        if input(f"  {CYAN('?')} {BOLD('Skriv ja for å konfigurere boksen')}: ").strip().lower() != "ja":
            raise Abort("Avbrutt. Ingenting er endret.")
        header("Konfigurerer")
        execute(dev, ctx, steps, env, args, pw)
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
