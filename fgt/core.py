# -*- coding: utf-8 -*-
"""
Kjernen i FortiGate-oppsettet: lese boksen, validere svar, lage konfig og kjøre den.

Ingen input() eller print() her. All kontakt med brukeren går gjennom et
Events-objekt, slik at både Terminal-versjonen (setup.py) og appen (app.py)
kan bruke den samme logikken.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import ipaddress
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

APP_NAME = "FortiGate Setup"


def _resource_dir() -> Path:
    """Der malene ligger: i .app-pakken når den er bygget, ellers prosjektmappa."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


RESOURCE_DIR = _resource_dir()
PROJECT_DIR = Path(__file__).resolve().parent.parent


def default_data_dir() -> Path:
    """Der site_defaults.yaml, secrets.env og output/ ligger.

    Fra kildekoden: prosjektmappa (som før). I bygd app: ~/Library/Application Support/FortiGate Setup
    på Mac og %APPDATA%\\FortiGate Setup på Windows, med mindre FGT_DATA_DIR er satt.
    """
    if os.environ.get("FGT_DATA_DIR"):
        return Path(os.environ["FGT_DATA_DIR"]).expanduser()
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_NAME
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / APP_NAME
        return Path.home() / ".local" / "share" / APP_NAME
    return PROJECT_DIR


SUPPORTED_MODELS = ("40F", "41F", "60F", "61F", "50G", "51G", "70G", "71G", "120G", "121G")


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


ERROR_RE = re.compile(
    r"command parse error|Command fail|value parse error|entry not found|"
    r"node_check_object fail|Unknown action|not found in datasource|"
    r"invalid (?:ip|netmask|value|input)|MUST be set",
    re.I,
)


READ_ERROR_RE = re.compile(r"command parse error|Unknown action", re.I)


STEP_NAMES = {
    "system": "Hostname, tidssone og admin",
    "snmp": "SNMPv3",
    "wan": "WAN-porter og ruting",
    "policy": "Brannmur-policy LAN → Internett",
}


class Abort(Exception):
    """Stopp med en forklarende melding."""


class ApplyError(Exception):
    def __init__(self, step: str, line: str, output: str):
        super().__init__(f"Steg '{step}' feilet på linjen: {line}")
        self.step, self.line, self.output = step, line, output


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


def v_alias(s: str) -> str:
    if s == "-":
        return ""
    if len(s) > 25 or '"' in s or "\\" in s:
        raise ValueError('Maks 25 tegn, uten " eller \\')
    return s


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
    raise Abort("Fant ingen ledig adresse til midlertidig IP i LAN-nettet.")


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
        # https (admin-GUI) er alltid med på WAN, uansett hva som står i fila.
        # Tilgangen begrenses av trusted hosts.
        "wan_allowaccess": " ".join(dict.fromkeys(str(wan.get("allowaccess", "ping")).split() + ["https"])),
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


# ───────────────────────── nettverkskortet på denne maskinen ─────────────────────────
# Mac: route/ipconfig/ifconfig.  Windows: PowerShell (NetTCPIP) og netsh.

def is_windows() -> bool:
    return sys.platform == "win32"


def local_net_supported() -> bool:
    """Kan vi lese og endre nettverkskortet automatisk på denne maskinen?"""
    return sys.platform in ("darwin", "win32")


def this_pc() -> str:
    return "Mac-en" if sys.platform == "darwin" else "PC-en"


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """subprocess.run uten konsollvindu som blinker opp på Windows."""
    if sys.platform == "win32":
        kw.setdefault("creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _ps_str(s: str) -> str:
    """PowerShell-streng i enkle anførselstegn."""
    return "'" + s.replace("'", "''") + "'"


def _ps(script: str) -> str:
    try:
        r = _run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                  "-Command", script], timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip()


def local_iface_for(host: str) -> str | None:
    """Navnet på nettverkskortet som når host (en5 / «Ethernet 2»)."""
    if is_windows():
        out = _ps(f"(Find-NetRoute -RemoteIPAddress {_ps_str(host)} -ErrorAction SilentlyContinue "
                  "| Select-Object -First 1).InterfaceAlias")
        return out.splitlines()[0].strip() if out else None
    out = _run(["route", "-n", "get", host]).stdout
    m = re.search(r"interface:\s*(\S+)", out)
    return m.group(1) if m else None


def local_ip(iface: str) -> str:
    """IPv4-adressen kortet har fått (DHCP foretrekkes foran manuelt lagt til)."""
    if is_windows():
        out = _ps(f"Get-NetIPAddress -InterfaceAlias {_ps_str(iface)} -AddressFamily IPv4 "
                  "-ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike '169.254.*' } "
                  "| Sort-Object { $_.PrefixOrigin -ne 'Dhcp' } | Select-Object -First 1 "
                  "-ExpandProperty IPAddress")
        return out.splitlines()[0].strip() if out else ""
    return _run(["ipconfig", "getifaddr", iface]).stdout.strip()


def local_router(iface: str) -> str:
    if is_windows():
        out = _ps(f"(Get-NetIPConfiguration -InterfaceAlias {_ps_str(iface)} -ErrorAction "
                  "SilentlyContinue).IPv4DefaultGateway | Select-Object -First 1 -ExpandProperty NextHop")
        return out.splitlines()[0].strip() if out else ""
    return _run(["ipconfig", "getoption", iface, "router"]).stdout.strip()


def route_src_ip(host: str) -> str:
    """Hvilken egen adresse operativsystemet ville brukt mot host (uten å sende noe)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 9))
            return s.getsockname()[0]
    except OSError:
        return ""


# Kommandoer som starter med OPTIONAL får feile uten at resten stopper (brukes på Windows).
OPTIONAL = "?"


def alias_cmds(iface: str, ip: str, mask: str) -> list[list[str]]:
    """Legg midlertidig ekstra IP på kortet."""
    if is_windows():
        # Windows 10 2004+/11: lar DHCP-adressen og en statisk adresse leve side om side.
        return [[OPTIONAL, "netsh", "interface", "ipv4", "set", "interface", f"interface={iface}",
                 "dhcpstaticipcoexistence=enabled"],
                ["netsh", "interface", "ipv4", "add", "address", f"name={iface}",
                 f"address={ip}", f"mask={mask}"]]
    return [["ifconfig", iface, "alias", ip, "netmask", mask]]


def cleanup_cmds(iface: str, ip: str, renew: bool) -> list[list[str]]:
    """Fjern midlertidig IP og tving ny DHCP-lease (ny IP og ny gateway)."""
    if is_windows():
        cmds = [[OPTIONAL, "netsh", "interface", "ipv4", "delete", "address", f"name={iface}",
                 f"address={ip}"],
                [OPTIONAL, "netsh", "interface", "ipv4", "set", "interface", f"interface={iface}",
                 "dhcpstaticipcoexistence=disabled"],
                [OPTIONAL, "netsh", "interface", "ipv4", "set", "address", f"name={iface}", "source=dhcp"],
                [OPTIONAL, "ipconfig", "/release", iface]]
        if renew:
            cmds.append([OPTIONAL, "ipconfig", "/renew", iface])
        return cmds
    # 'ipconfig set DHCP' alene fornyer ikke når kortet allerede står på DHCP.
    # Bytt til BOOTP og tilbake, så slipper macOS den gamle leasen.
    return [["ifconfig", iface, "-alias", ip],
            ["ipconfig", "set", iface, "BOOTP"],
            ["sleep", "1"],
            ["ipconfig", "set", iface, "DHCP"]]


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
    return Environment(loader=FileSystemLoader(str(RESOURCE_DIR / "templates")), undefined=StrictUndefined,
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


def a_prefix(lan: dict) -> int:
    return ipaddress.ip_network(f"0.0.0.0/{lan['mask']}").prefixlen


# ───────────────────────────── filer og innstillinger ─────────────────────────────

def ensure_data_dir(data_dir: Path) -> list[str]:
    """Lag datamappa og kopier eksempelfiler første gang. Returnerer filene som ble laget."""
    data_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for real, example in (("site_defaults.yaml", "site_defaults.example.yaml"),
                          ("secrets.env", "secrets.env.example")):
        dst, src = data_dir / real, RESOURCE_DIR / example
        if not dst.exists() and src.exists():
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            if real == "secrets.env":
                dst.chmod(0o600)
            made.append(real)
    return made


def load_env_file(path: Path) -> None:
    """Les KEY='verdi'-linjer (som secrets.env) inn i miljøet."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        if key:
            os.environ[key] = val


# ───────────────────────────── hendelser ─────────────────────────────

class Events:
    """Det kjernen trenger fra brukergrensesnittet. Standard: gjør ingenting."""

    @contextlib.contextmanager
    def step(self, name: str):
        """Ett synlig steg (vises som ▸ navn ... ✔/✖)."""
        yield

    def info(self, msg: str) -> None: ...
    def ok(self, msg: str) -> None: ...
    def warn(self, msg: str) -> None: ...

    def confirm(self, msg: str) -> None:
        """Vent til brukeren har gjort noe manuelt (f.eks. satt IP selv)."""

    def new_password(self) -> str:
        """Fabrikknullstilt boks: be om nytt admin-passord (to ganger, like)."""
        raise Abort("Boksen krever nytt admin-passord.")

    def password(self, prompt: str) -> str:
        """Be om eksisterende admin-passord."""
        raise Abort("Boksen krever passord.")


class AdminRunner:
    """Kjører kommandoer som krever administrator på maskinen (ifconfig/ipconfig/netsh)."""

    def prepare(self) -> None:
        """Be om tilgang på forhånd, slik at senere kall ikke stopper midt i."""

    def run(self, cmds: list[list[str]]) -> None:
        raise NotImplementedError


class SudoRunner(AdminRunner):
    """Terminal: sudo, passord skrives i Terminal."""

    def prepare(self) -> None:
        subprocess.run(["sudo", "-v", "-p", "  Mac-passord for %u: "], check=True)

    def run(self, cmds: list[list[str]]) -> None:
        for c in cmds:
            c = [x for x in c if x != OPTIONAL]
            LOG.write("sudo " + " ".join(c))
            subprocess.run(["sudo", *c], check=True)


class OsaRunner(AdminRunner):
    """App: macOS sin vanlige passorddialog (osascript ... with administrator privileges)."""

    def run(self, cmds: list[list[str]]) -> None:
        script = " && ".join(" ".join(shlex.quote(x) for x in c if x != OPTIONAL) for c in cmds)
        LOG.write("admin: " + script)
        osa = f'do shell script {_applescript_str(script)} with administrator privileges'
        r = subprocess.run(["osascript", "-e", osa], capture_output=True, text=True)
        if r.returncode != 0:
            raise subprocess.CalledProcessError(r.returncode, "osascript", r.stdout, r.stderr)


def _applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _cmd_line(c: list[str]) -> str:
    """Én linje i en .cmd-fil. netsh-argumenter som navn=«Ethernet 2» får anførselstegn rundt verdien."""
    out = []
    for x in c:
        k, eq, v = x.partition("=")
        if eq and " " in v and not k.startswith("/"):
            out.append(f'{k}="{v}"')
        elif " " in x or not x:
            out.append(f'"{x}"')
        else:
            out.append(x)
    return " ".join(out)


def uac_script(cmds: list[list[str]], log: str) -> str:
    """Innholdet i .cmd-fila som kjøres som administrator."""
    lines = ["@echo off", "chcp 65001 >nul"]
    for c in cmds:
        optional = bool(c) and c[0] == OPTIONAL
        line = _cmd_line([x for x in c if x != OPTIONAL])
        lines.append(f'echo ^> {line.replace("%", "%%")} >> "{log}"')
        if optional:
            lines.append(f'{line} >> "{log}" 2>&1')
        else:
            lines.append(f'{line} >> "{log}" 2>&1 || exit /b 1')
    lines.append("exit /b 0")
    return "\r\n".join(lines) + "\r\n"


class UacRunner(AdminRunner):
    """Windows: kjører kommandoene som administrator, Windows spør via sin vanlige UAC-dialog."""

    def run(self, cmds: list[list[str]]) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="fgt-"))
        bat, log = tmp / "admin.cmd", tmp / "admin.log"
        bat.write_text(uac_script(cmds, str(log)), encoding="utf-8")
        LOG.write("admin (UAC): " + " && ".join(_cmd_line([x for x in c if x != OPTIONAL]) for c in cmds))
        args = _ps_str(f'/c "{bat}"')
        ps = ("try { $p = Start-Process -FilePath $env:ComSpec "
              f"-ArgumentList {args} "
              "-Verb RunAs -WindowStyle Hidden -Wait -PassThru -ErrorAction Stop; exit $p.ExitCode } "
              "catch { exit 1223 }")
        r = _run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps])
        try:
            out = log.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            out = ""
        if out:
            LOG.write(out)
        for f in (bat, log):
            with contextlib.suppress(OSError):
                f.unlink()
        with contextlib.suppress(OSError):
            tmp.rmdir()
        if r.returncode != 0:
            msg = "avbrutt i Windows-dialogen" if r.returncode == 1223 else (out.splitlines()[-1] if out else "")
            raise subprocess.CalledProcessError(r.returncode, "admin", msg, msg)


def admin_runner(gui: bool) -> AdminRunner:
    """Riktig måte å få administratortilgang på for denne maskinen."""
    if is_windows():
        return UacRunner()
    return OsaRunner() if gui else SudoRunner()


# ───────────────────────────── innlogging og lesing ─────────────────────────────

def _empty_login(host: str, user: str):
    """SSH-innlogging med tomt passord. Returnerer (transport, shell-kanal, tekst så langt) eller None."""
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
    return t, chan, _read_until(chan, r"New Password:|#\s*$", 20)


def first_login(host: str, user: str, ev: Events) -> str | None:
    """Logg inn med tomt passord og håndter tvunget passordbytte.

    Returnerer nytt passord, eller None hvis boksen allerede har passord.

    Passordet spørres om mens INGEN SSH-økt står åpen: FortiGaten kaster deg ut av
    «New Password»-spørsmålet etter en stund, og en dialog i appen kan ta tid.
    Derfor: sjekk først, lukk, spør, og logg så inn på nytt og send passordet med en gang.
    """
    first = _empty_login(host, user)
    if first is None:
        return None
    t, _chan, buf = first
    t.close()
    if "New Password" not in buf:
        raise Abort("Boksen godtok tomt passord uten å kreve bytte. Sett admin-passord "
                    "manuelt i GUI/CLI og kjør på nytt.")
    ev.info("Fabrikknullstilt boks – velg nytt admin-passord for FortiGaten")
    pw = ev.new_password()

    again = _empty_login(host, user)
    if again is None:
        raise Abort("Boksen godtok ikke tomt passord andre gang. Har noen allerede satt passord? "
                    "Prøv å koble til på nytt og bruk det passordet.")
    t, chan, buf = again
    try:
        if "New Password" not in buf:
            raise Abort("Boksen ba ikke om nytt passord andre gang. Prøv å koble til på nytt.")
        chan.send(pw + "\n")
        _read_until(chan, r"Confirm Password:", 15)
        chan.send(pw + "\n")
        buf = _read_until(chan, r"New Password:|#\s*$|[Ee]rror|[Ii]nvalid", 20)
    finally:
        t.close()
    if "New Password" in buf or re.search(r"[Ee]rror|[Ii]nvalid", buf):
        raise Abort("FortiGaten avviste passordet (passordpolicy?). Svar fra boksen:\n" + buf[-400:])
    ev.ok("Admin-passord satt")
    return pw


def connect(host: str, user: str, ev: Events) -> tuple["LiveDevice", str]:
    """Koble til boksen: håndter førstegangsinnlogging eller be om passord."""
    with ev.step(f"Kobler til {host}"):
        reachable = wait_port(host, 22, 10)
    if not reachable:
        raise Abort(f"Når ikke SSH på {host}. Står {this_pc()} på LAN-porten og har fått DHCP?")
    try:
        pw = first_login(host, user, ev)
        if pw is None:
            pw = ev.password(f"FortiGate-passord for {user}")
        LOG.secrets.add(pw)
        with ev.step("Logger inn"):
            dev = LiveDevice(host, user, pw)
    except Abort:
        raise
    except Exception as e:  # noqa: BLE001 – gjør SSH-/nettverksfeil forståelige
        raise Abort(f"SSH-feil mot {host}: {type(e).__name__}: {e or 'forbindelsen ble brutt'}. "
                    "Prøv å koble til på nytt.") from e
    return dev, pw


def read_box(dev, host: str, ev: Events) -> tuple[dict, dict]:
    with ev.step("Leser oppsettet på boksen"):
        raw = {k: dev.read(c) for k, c in READ_CMDS.items()}
        info = analyze(raw, host)
    return raw, info


def preflight(info: dict) -> tuple[list[str], list[str]]:
    """(feil som stopper, advarsler) for boksen."""
    errors, warns = [], []
    if info["version"] < MIN_VERSION:
        errors.append("Malene er skrevet for FortiOS 7.6. Oppgrader boksen først.")
    if info["vdom"] != "disable":
        errors.append("Boksen har VDOM aktivert. Ikke støttet.")
    if info["model"] not in SUPPORTED_MODELS:
        warns.append(f"{info['model']} er ikke testet med disse malene.")
    if info["sdwan_status"] == "enable" and info["sdwan_members"]:
        warns.append("SD-WAN er allerede konfigurert på boksen. Medlemmer kan bli overskrevet.")
    if info["cfg_save"] != "automatic":
        warns.append(f"cfg-save står i '{info['cfg_save']}' fra en tidligere kjøring – settes tilbake til automatic")
    return errors, warns


# ───────────────────────────── skjema (brukes av appen) ─────────────────────────────

@dataclass
class PortChoice:
    name: str
    label: str
    usable: bool


def port_choices(info: dict, lan_iface: str) -> list[PortChoice]:
    return [PortChoice(p, port_label(info, p), p not in info["aggregate_of"])
            for p in wan_candidates(info, lan_iface)]


def form_defaults(info: dict, d: dict) -> dict:
    """Forslag til skjemaet, likt det Terminal-versjonen foreslår."""
    usable = [c.name for c in port_choices(info, info["mgmt_iface"] or "lan") if c.usable]
    pref = [p for p in ("wan1", "wan2", "wan") if p in usable] or usable
    return {
        "lan_iface": info["mgmt_iface"] or "lan",
        "wan_mode": "sdwan" if len([p for p in ("wan1", "wan2", "wan") if p in usable]) >= 2 else "single",
        "wan1_port": pref[0] if pref else "",
        "wan2_port": pref[1] if len(pref) > 1 else "",
        "dns": ", ".join(d["lan"]["dns"]),
    }


def suggest_dhcp(lan_ip_text: str, d: dict) -> tuple[str, str] | None:
    try:
        ipi = v_iface_ip(lan_ip_text.strip(), "LAN-adresse")
    except ValueError:
        return None
    s, e = default_range(ipi.network, ipi.ip, d["lan"]["dhcp_offsets"])
    return str(s), str(e)


def validate_form(info: dict, d: dict, f: dict) -> tuple[dict | None, dict[str, str]]:
    """Sjekk et utfylt skjema. Returnerer (svar, {}) eller (None, {felt: feilmelding}).

    Svarene har samme form som Terminal-versjonens interview() lager.
    """
    err: dict[str, str] = {}
    a: dict = {}

    def chk(field, fn):
        try:
            return fn()
        except ValueError as e:
            err[field] = str(e)
            return None

    a["hostname"] = chk("hostname", lambda: v_hostname(f.get("hostname", "").strip()))
    lan_iface = f.get("lan_iface", "").strip()
    if lan_iface not in info["ifaces"]:
        err["lan_iface"] = f"Finner ikke interface '{lan_iface}'"
    a["lan_iface"] = lan_iface
    lan_ip = chk("lan_ip", lambda: v_iface_ip(f.get("lan_ip", "").strip(), "LAN-adresse"))
    a["lan_ip"] = lan_ip
    net = lan_ip.network if lan_ip else None

    a["dhcp_enabled"] = bool(f.get("dhcp_enabled", True))
    if a["dhcp_enabled"] and net:
        def v_in(s):
            ip = v_ip(s.strip())
            if ip not in net or ip in (net.network_address, net.broadcast_address, lan_ip.ip):
                raise ValueError(f"Må være en ledig vertsadresse i {net}")
            return ip
        start = chk("dhcp_start", lambda: v_in(f.get("dhcp_start", "")))
        end = chk("dhcp_end", lambda: v_in(f.get("dhcp_end", "")))
        if start and end:
            if end < start:
                err["dhcp_end"] = "Slutt må være etter start"
            elif start <= lan_ip.ip <= end:
                err["dhcp_end"] = "LAN-IP-en kan ikke ligge inne i DHCP-rangen"
        a["dhcp_start"], a["dhcp_end"] = start, end
        a["dns"] = chk("dns", lambda: v_dns(f.get("dns", "")))

    mode = f.get("wan_mode", "single")
    a["wan_mode"] = mode
    choices = {c.name: c for c in port_choices(info, lan_iface)}
    wans, taken = [], set()
    for n in (1, 2) if mode == "sdwan" else (1,):
        k = f"wan{n}_"
        port = f.get(k + "port", "")
        if port not in choices:
            err[k + "port"] = "Velg en port"
        elif not choices[port].usable:
            err[k + "port"] = f"{port} er medlem av '{info['aggregate_of'][port]}' og kan ikke brukes"
        elif port in taken:
            err[k + "port"] = f"{port} er allerede valgt"
        taken.add(port)
        w = {"port": port, "mode": f.get(k + "mode", "dhcp"),
             "alias": chk(k + "alias", lambda: v_alias(f.get(k + "alias", "").strip() or "-")) or ""}
        if w["mode"] == "static":
            def v_wan():
                ipi = v_iface_ip(f.get(k + "ip", "").strip(), "WAN-adresse")
                if net and ipi.network.overlaps(net):
                    raise ValueError(f"Overlapper med LAN-nettet {net}")
                return ipi
            ipi = chk(k + "ip", v_wan)

            def v_gw():
                gw = v_ip(f.get(k + "gateway", "").strip())
                if ipi and (gw not in ipi.network or gw == ipi.ip):
                    raise ValueError(f"Gateway må ligge i {ipi.network} og være forskjellig fra WAN-IP")
                return gw
            w["ipi"], w["gateway"] = ipi, chk(k + "gateway", v_gw)
        wans.append(w)
    a["wans"] = wans
    return (None, err) if err else (a, {})


# ───────────────────────────── plan og filer ─────────────────────────────

@dataclass
class Plan:
    ctx: dict
    steps: list
    env: Environment
    out_dir: Path
    cfg_text: str
    refs: dict


def make_plan(dev, info: dict, raw: dict, d: dict, answers: dict, data_dir: Path) -> Plan:
    env = make_env()
    ctx = build_ctx(info, d, answers)
    refs = check_refs(dev, ctx)
    steps = build_steps(env, ctx)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = data_dir / "output" / f"{ctx['hostname']}_{info['serial']}_{stamp}"
    (out_dir / "raw").mkdir(parents=True, exist_ok=True)
    for k, v in raw.items():
        (out_dir / "raw" / f"{k}.txt").write_text(v, encoding="utf-8")
    cfg_text = "".join(f"# ─── steg: {s.name} ───\n{s.text}\n" for s in steps)
    (out_dir / "config.txt").write_text(LOG.mask(cfg_text), encoding="utf-8")
    LOG.attach(out_dir / "run.log")
    return Plan(ctx, steps, env, out_dir, LOG.mask(cfg_text), refs)


def gui_url(ctx: dict) -> str:
    port = ctx["admin_https_port"]
    return f"https://{ctx['lan']['ip']}" + ("" if port == 443 else f":{port}")


# ───────────────────────────── utførelse ─────────────────────────────

@dataclass
class RunOptions:
    host: str = "192.168.1.99"
    user: str = "admin"
    mac_alias: bool = True          # legg midlertidig IP på Mac-en automatisk


@dataclass
class Status:
    default_routes: list
    sdwan: list                      # [(port, state | None)]


def execute(dev, plan: Plan, opts: RunOptions, pw: str, ev: Events, admin: AdminRunner) -> Status:
    """Kjør alle stegene. Kaster Abort/ApplyError ved feil (endringene frem dit er lagret)."""
    ctx, env = plan.ctx, plan.env
    lan = ctx["lan"]
    mac = None
    manual = (f"Etter IP-byttet må {this_pc()} nå {lan['ip']}. Gi den f.eks. {lan['alias_ip']}/"
              f"{a_prefix(lan)} som ekstra IP før du fortsetter.")
    if lan["same_iface"]:
        if not opts.mac_alias or not local_net_supported():
            ev.confirm(manual)
        else:
            mif = local_iface_for(opts.host)
            if not mif:
                raise Abort(f"Fant ikke hvilket nettverkskort på {this_pc()} som når {opts.host}")
            my_ip = local_ip(mif)
            new_net = ipaddress.ip_network(f"{lan['ip']}/{lan['mask']}", strict=False)
            if my_ip and ipaddress.ip_address(my_ip) in new_net:
                ev.ok(f"{this_pc()} ({my_ip}) står allerede i {new_net}")
            else:
                ev.info(f"{this_pc()} får midlertidig IP {lan['alias_ip']} på {mif} for å nå nytt LAN")
                if is_windows():
                    ev.warn("Windows spør nå om å la appen gjøre endringer (administrator på PC-en)")
                else:
                    ev.warn("macOS spør nå etter passordet til MAC-BRUKEREN din (ikke FortiGate)")
                try:
                    admin.prepare()
                    admin.run(alias_cmds(mif, lan["alias_ip"], lan["mask"]))
                except subprocess.CalledProcessError as e:
                    ev.warn(f"Klarte ikke legge til midlertidig IP automatisk: {e.stderr or e}")
                    ev.confirm(manual)
                else:
                    mac = (mif, lan["alias_ip"])
                    ev.ok("Midlertidig IP lagt til")

    try:
        for st in plan.steps:
            if st.final and lan["same_iface"]:
                # Linje for linje med feilsjekk. Bare 'next'/'end' (som aktiverer IP-en) sendes blindt.
                iface_lines = Step("lan-ip", "50_lan.j2", render(env, "50_lan.j2", ctx, part="iface")).lines
                commit = iface_lines.index("next")
                with ev.step(f"LAN-IP {lan['cidr']} på {lan['iface']}"):
                    run_block(dev, "lan-ip", iface_lines[:commit])
                    dev.commit_and_drop(iface_lines[commit:])
                ev.info("SSH-forbindelsen brøt, som forventet ved IP-bytte")
                with ev.step(f"Kobler til igjen på {lan['ip']} (inntil 3 min)"):
                    if not wait_port(lan["ip"], 22, 180):
                        diagnose_old(opts, pw, lan, ev)
                        raise Abort(f"Fikk ikke kontakt med {lan['ip']} innen 3 minutter.")
                    time.sleep(3)
                    dev = LiveDevice(lan["ip"], opts.user, pw)
                dhcp_lines = Step("dhcp", "50_lan.j2", render(env, "50_lan.j2", ctx, part="dhcp")).lines
                if dhcp_lines:
                    with ev.step("DHCP-server for nytt LAN"):
                        run_block(dev, "dhcp", dhcp_lines)
            else:
                with ev.step(STEP_NAMES.get(st.name, st.name)):
                    run_block(dev, st.name, st.lines)
                    if st.name == "system":
                        dev.refresh_prompt()

        with ev.step("Kontrollerer LAN-IP og DHCP"):
            ok_ip, ok_dhcp = verify(dev, ctx)
            if not ok_ip:
                raise Abort("LAN-IP ble ikke som forventet.")
            if not ok_dhcp:
                raise Abort("DHCP-oppsettet ble ikke som forventet.")

        if ctx["final_trusthosts"] != ctx["trusthosts"]:
            with ev.step("Endelige trusted hosts"):
                run_block(dev, "trusthosts", Step("th", "", render(
                    env, "_trusthosts.j2", ctx, hosts=ctx["final_trusthosts"])).lines)

        with ev.step("Kontrollerer at alt er lagret"):
            save_config(dev)
        status = collect_status(dev, ctx)
        dev.close()
        return status
    finally:
        if mac:
            renew_local_dhcp(mac[0], mac[1], lan, ev, admin)


def collect_status(dev, ctx: dict) -> Status:
    routes = dev.read("get router info routing-table all")
    default = [ln.strip() for ln in routes.splitlines() if "0.0.0.0/0" in ln]
    sd = []
    if ctx["wan_mode"] == "sdwan":
        hc = dev.read("diagnose sys sdwan health-check")
        for w in ctx["wans"]:
            port = re.escape(w["port"])
            # 7.x: "Seq(1 wan1): state(alive), ..."  (eldre: "wan1(1): state(alive)")
            m = re.search(rf"(?:Seq\(\d+ {port}\)|{port}\(\d+\)): state\((\w+)\)", hc)
            sd.append((w["port"], m.group(1) if m else None))
    return Status(default, sd)


def renew_local_dhcp(mif: str, alias: str, lan: dict, ev: Events, admin: AdminRunner) -> None:
    """Fjern midlertidig IP og tving ny DHCP-lease (ny IP og ny gateway) på maskinen."""
    try:
        admin.run(cleanup_cmds(mif, alias, renew=bool(lan["dhcp_enabled"])))
    except subprocess.CalledProcessError as e:
        ev.warn(f"Klarte ikke rydde nettverkskortet automatisk: {e}")
        return
    if not lan["dhcp_enabled"]:
        ev.ok(f"Midlertidig IP {alias} fjernet")
        return
    net = ipaddress.ip_network(f"{lan['ip']}/{lan['mask']}", strict=False)
    with ev.step(f"{this_pc()} henter ny DHCP-adresse"):
        end, got = time.time() + 45, ""
        while time.time() < end:
            got = local_ip(mif)
            if got and ipaddress.ip_address(got) in net:
                break
            time.sleep(2)
    router = local_router(mif)
    if got and ipaddress.ip_address(got) in net:
        ev.ok(f"{this_pc()} har nå {got}, gateway {router or '?'}")
    else:
        ev.warn(f"{this_pc()} fikk ikke adresse i {net} ennå. Trekk ut og sett inn kabelen.")


def diagnose_old(opts: RunOptions, pw: str, lan: dict, ev: Events) -> None:
    """Ny IP svarer ikke. Sjekk om boksen fortsatt er på gammel IP, og vis hva interfacet har."""
    if not wait_port(opts.host, 22, 5):
        ev.warn(f"Boksen svarer heller ikke på {opts.host}.")
        return
    try:
        d = LiveDevice(opts.host, opts.user, pw)
        ifs = edits(parse_fos(d.read("show system interface")), "system interface")
        got = iface_ip(ifs.get(lan["iface"], {}))
        ev.warn(f"Boksen svarer fortsatt på {opts.host}. {lan['iface']} har IP {got}, "
                f"altså ble IP-byttet ikke utført.")
        d.close()
    except Exception as e:
        ev.warn(f"Boksen svarer på port 22 på {opts.host}, men innlogging feilet: {e}")


_IFACE_CACHE: dict[tuple[str, str], str | None] = {}


def link_status(host: str) -> dict | None:
    """Hvilket nettverkskort på maskinen som når host, og om det har adresse i samme /24.

    Kalles hvert 2. sekund fra appen. På Windows er PowerShell tregt, så adressen leses
    direkte fra operativsystemet og kortnavnet slås bare opp når adressen endrer seg.
    """
    if not local_net_supported():
        return None
    try:
        net = ipaddress.ip_network(f"{host}/24", strict=False)
        if is_windows():
            ip = route_src_ip(host)
            key = (host, ip)
            if key not in _IFACE_CACHE:
                _IFACE_CACHE.clear()
                _IFACE_CACHE[key] = local_iface_for(host) if ip else None
            iface = _IFACE_CACHE[key]
        else:
            iface = local_iface_for(host)
            ip = local_ip(iface) if iface else ""
        ready = bool(ip) and ipaddress.ip_address(ip) in net
    except (ValueError, OSError):
        return None
    return {"iface": iface, "ip": ip, "ready": ready}


def open_url(url: str) -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", url])
    else:
        webbrowser.open(url)


def summary_rows(ctx: dict) -> list[tuple[str, str] | None]:
    """Oppsummeringen som (etikett, verdi)-rader. None = mellomrom mellom grupper."""
    lan = ctx["lan"]
    tls = ", ".join("TLS " + v.replace("tlsv", "").replace("-", ".") for v in ctx["admin_tls"].split())
    rows: list = [("Hostname", ctx["hostname"]), ("Tidssone", ctx["timezone"]),
                  ("Admin-timeout", f"{ctx['admin_timeout']} min"),
                  ("Admin HTTPS", f"port {ctx['admin_https_port']}  ({tls})"), None,
                  ("LAN", f"{lan['iface']}  {lan['cidr']}")]
    if lan["dhcp_enabled"]:
        rows += [("DHCP", f"{lan['dhcp_start']} – {lan['dhcp_end']}  (lease {lan['lease_time'] // 3600} t)"),
                 ("DNS", ", ".join(lan["dns"]))]
    else:
        rows.append(("DHCP", "av"))
    rows += [None, ("WAN-modus", "SD-WAN" if ctx["wan_mode"] == "sdwan" else "én WAN")]
    for i, w in enumerate(ctx["wans"], 1):
        addr = f"{w['ip']}/{a_prefix(w)}  gw {w['gateway']}" if w["mode"] == "static" else "DHCP"
        name = f"  «{w['alias']}»" if w["alias"] else ""
        rows.append((f"WAN{i}", f"{w['port']}{name}  {addr}"))
    th = [f"{t.split()[0]}/{a_prefix({'mask': t.split()[1]})}" for t in ctx["final_trusthosts"]]
    rows += [None, ("Trusted hosts", ", ".join(th) or "(ingen)"),
             ("SNMPv3", ", ".join(u["name"] for u in ctx["snmp"]["users"]) or "(ingen)")]
    return rows


def summary_notes(ctx: dict) -> list[str]:
    notes = []
    if ctx["delete_hc"]:
        notes.append(f"Fjerner {len(ctx['delete_hc'])} default SD-WAN-helsesjekker (Default_*)")
    for desc in ctx["replace_policies_desc"]:
        notes.append(f"Default-policy {desc} erstattes av «{ctx['policy']['name']}»")
    if ctx["policy"]["exists"]:
        notes.append(f"Policy «{ctx['policy']['name']}» finnes fra før og beholdes")
    if ctx["lan"]["same_iface"]:
        notes.append(f"{this_pc()} får midlertidig {ctx['lan']['alias_ip']} for å nå nytt LAN "
                     + ("(Windows ber om administratortilgang)" if is_windows()
                        else "(macOS spør om Mac-passordet ditt)"))
    return notes
