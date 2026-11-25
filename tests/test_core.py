"""Tester for fgt.core uten ekte FortiGate.  Kjør:  python3 -m pytest -q"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fgt import core as C  # noqa: E402

FIX = ROOT / "tests" / "fixtures"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("FGT_SNMP_AUTH", "AuthPass123!")
    monkeypatch.setenv("FGT_SNMP_PRIV", "PrivPass456!")
    monkeypatch.setattr(C.time, "sleep", lambda s: None)
    C.LOG.path, C.LOG.buf = None, []


@pytest.fixture
def defaults():
    return C.load_defaults(ROOT / "site_defaults.example.yaml")


def box(model: str):
    dev = C.OfflineDevice(FIX / model)
    raw, info = C.read_box(dev, "192.168.1.99", C.Events())
    return dev, raw, info


def form(**kw):
    f = {"hostname": "kunde-fw01", "lan_iface": "internal", "lan_ip": "10.20.30.1/24",
         "dhcp_enabled": True, "dhcp_start": "10.20.30.50", "dhcp_end": "10.20.30.250",
         "dns": "1.1.1.1, 8.8.8.8", "wan_mode": "sdwan",
         "wan1_port": "wan1", "wan1_mode": "static", "wan1_ip": "84.208.10.10/29",
         "wan1_gateway": "84.208.10.9", "wan1_alias": "Telenor",
         "wan2_port": "wan2", "wan2_mode": "dhcp", "wan2_alias": ""}
    f.update(kw)
    return f


# ───────────── lesing ─────────────

def test_analyze_61f():
    _, _, info = box("61F")
    assert info["model"] == "61F"
    assert info["version"] == (7, 6, 6)
    assert info["mgmt_iface"] == "internal"
    assert "modem" not in [c.name for c in C.port_choices(info, "internal")]
    assert len(info["sdwan_healthchecks"]) == 5
    assert C.preflight(info) == ([], [])


def test_form_defaults_61f(defaults):
    _, _, info = box("61F")
    fd = C.form_defaults(info, defaults)
    assert fd["wan_mode"] == "sdwan" and fd["wan1_port"] == "wan1" and fd["wan2_port"] == "wan2"
    assert C.suggest_dhcp("10.20.30.1/24", defaults) == ("10.20.30.50", "10.20.30.250")
    assert C.suggest_dhcp("tull", defaults) is None


# ───────────── skjema ─────────────

def test_validate_form_ok(defaults):
    _, _, info = box("61F")
    a, err = C.validate_form(info, defaults, form())
    assert err == {}
    assert a["wans"][0]["alias"] == "Telenor" and a["wans"][1]["alias"] == ""


@pytest.mark.parametrize("change,field", [
    ({"hostname": "ugyldig navn"}, "hostname"),
    ({"lan_ip": "10.20.30.1"}, "lan_ip"),
    ({"dhcp_start": "10.20.31.5"}, "dhcp_start"),
    ({"dhcp_start": "10.20.30.200", "dhcp_end": "10.20.30.100"}, "dhcp_end"),
    ({"wan1_ip": "10.20.30.5/24"}, "wan1_ip"),
    ({"wan1_gateway": "1.2.3.4"}, "wan1_gateway"),
    ({"wan2_port": "wan1"}, "wan2_port"),
    ({"wan2_port": "a"}, "wan2_port"),
    ({"wan1_alias": "x" * 30}, "wan1_alias"),
])
def test_validate_form_errors(defaults, change, field):
    _, _, info = box("61F")
    a, err = C.validate_form(info, defaults, form(**change))
    assert a is None and field in err


# ───────────── plan / generert CLI ─────────────

def test_plan_61f_sdwan(defaults, tmp_path):
    dev, raw, info = box("61F")
    a, _ = C.validate_form(info, defaults, form())
    plan = C.make_plan(dev, info, raw, defaults, a, tmp_path)
    cfg = plan.cfg_text
    assert 'delete 1' in cfg                                  # default-policy internal -> wan1
    assert 'delete "Default_Google Search"' in cfg            # default helsesjekker
    assert 'set alias "Telenor"' in cfg
    assert "set admin-sport 8443" in cfg
    assert "set admin-https-ssl-versions tlsv1-2 tlsv1-3" in cfg
    assert "set lease-time 28800" in cfg
    assert 'set dstintf "virtual-wan-link"' in cfg
    assert cfg.count("set allowaccess ping https\n") == 2          # https på begge WAN
    assert "********" in cfg and "AuthPass123!" not in cfg    # passord maskert
    assert (plan.out_dir / "config.txt").exists()
    assert C.gui_url(plan.ctx) == "https://10.20.30.1:8443"


def test_plan_40f_single_wan(defaults, tmp_path):
    dev, raw, info = box("40F")
    f = form(lan_iface="lan", wan_mode="single", wan1_port="wan", wan1_mode="dhcp", wan1_alias="")
    a, err = C.validate_form(info, defaults, f)
    assert err == {}
    plan = C.make_plan(dev, info, raw, defaults, a, tmp_path)
    assert 'set dstintf "wan"' in plan.cfg_text
    assert "set allowaccess ping https\n" in plan.cfg_text           # https også med én WAN
    assert "config system sdwan\n    set status enable" not in plan.cfg_text


# ───────────── utførelse mot simulert boks ─────────────

class FakeDevice:
    """Husker LAN-IP og DHCP fra det som sendes, og svarer som en 61F."""
    state = {"ip": False, "dhcp": False, "pending": False}

    def __init__(self, host="192.168.1.99", user="", pw=""):
        self.host, self.sent = host, []

    def read(self, cmd):
        s = FakeDevice.state
        if cmd == "show system interface" and s["ip"]:
            return 'config system interface\n edit "internal"\n set ip 10.20.30.1 255.255.255.0\n next\nend\n'
        if cmd == "show system dhcp server" and s["dhcp"]:
            return ('config system dhcp server\n edit 1\n set default-gateway 10.20.30.1\n'
                    ' set interface "internal"\n config ip-range\n edit 1\n set start-ip 10.20.30.50\n'
                    ' set end-ip 10.20.30.250\n next\n end\n next\nend\n')
        return C.OfflineDevice(FIX / "61F").read(cmd)

    def line(self, c):
        self.sent.append(c)
        if c.startswith("set ip 10.20.30.1"):
            FakeDevice.state["pending"] = True
        if c.startswith("set start-ip"):
            FakeDevice.state["dhcp"] = True
        return ""

    def commit_and_drop(self, lines):
        if FakeDevice.state["pending"]:
            FakeDevice.state["ip"] = True

    def refresh_prompt(self): ...
    def wait_for(self, c, pat, timeout): return "config saved."
    def alive(self): return True
    def close(self): ...


class RecEvents(C.Events):
    def __init__(self):
        self.steps, self.msgs = [], []

    def step(self, name):
        self.steps.append(name)
        return super().step(name)

    def info(self, m): self.msgs.append(m)
    ok = warn = confirm = info


def test_execute_full_flow(defaults, tmp_path, monkeypatch):
    FakeDevice.state = {"ip": False, "dhcp": False, "pending": False}
    monkeypatch.setattr(C, "LiveDevice", FakeDevice)
    monkeypatch.setattr(C, "wait_port", lambda *a, **k: True)
    dev, raw, info = box("61F")
    a, _ = C.validate_form(info, defaults, form())
    plan = C.make_plan(dev, info, raw, defaults, a, tmp_path)
    ev = RecEvents()
    st = C.execute(FakeDevice(), plan, C.RunOptions(mac_alias=False), "pw", ev, C.AdminRunner())
    assert ev.steps[-1] == "Kontrollerer at alt er lagret"
    assert "DHCP-server for nytt LAN" in ev.steps
    assert st.sdwan == [("wan1", None), ("wan2", None)]


def test_commit_and_drop_answers_yes():
    class Ch:
        def __init__(s): s.sent, s.dead = [], False
        def write_channel(s, t):
            if s.dead:
                raise OSError("closed")
            s.sent.append(t.strip())
            if t.strip() == "y":
                s.dead = True
        def read_channel(s):
            if s.dead:
                raise OSError("closed")
            return ("1 admin session(s) are currently connected on this interface.\n"
                    "Are you sure you want to continue? (y/n)") if s.sent[-1] == "next" else ""
    d = C.LiveDevice.__new__(C.LiveDevice)
    d.conn = Ch()
    d.drop = lambda: None
    d.commit_and_drop(["next", "end"])
    assert d.conn.sent == ["next", "y"]


def test_load_env_file(tmp_path, monkeypatch):
    p = tmp_path / "secrets.env"
    p.write_text("# kommentar\nFGT_X='a b'\nexport FGT_Y=\"c\"\n")
    monkeypatch.delenv("FGT_X", raising=False)
    C.load_env_file(p)
    assert os.environ["FGT_X"] == "a b" and os.environ["FGT_Y"] == "c"


def test_ensure_data_dir(tmp_path):
    made = C.ensure_data_dir(tmp_path / "data")
    assert set(made) == {"site_defaults.yaml", "secrets.env"}
    assert C.ensure_data_dir(tmp_path / "data") == []


def test_mac_link_status(monkeypatch):
    monkeypatch.setattr(C.sys, "platform", "darwin")
    monkeypatch.setattr(C, "mac_iface_for", lambda h: "en5")
    monkeypatch.setattr(C, "mac_ip", lambda i: "192.168.1.110")
    assert C.mac_link_status("192.168.1.99") == {"iface": "en5", "ip": "192.168.1.110", "ready": True}
    monkeypatch.setattr(C, "mac_ip", lambda i: "10.0.0.5")
    assert C.mac_link_status("192.168.1.99")["ready"] is False
    monkeypatch.setattr(C.sys, "platform", "linux")
    assert C.mac_link_status("192.168.1.99") is None


class _FakeT:
    def __init__(self, log): self.log, self.closed = log, False
    def close(self): self.closed = True; self.log.append("close")


class _FakeChan:
    def __init__(self, log): self.log = log
    def send(self, t): self.log.append("send")


def test_first_login_asks_password_with_no_session_open(monkeypatch):
    log = []

    def empty_login(host, user):
        log.append("login")
        return _FakeT(log), _FakeChan(log), "You are forced to change your password.\nNew Password:"
    monkeypatch.setattr(C, "_empty_login", empty_login)
    monkeypatch.setattr(C, "_read_until", lambda ch, pat, to: "Confirm Password:" if "Confirm" in pat else "FGT #")

    class Ev(C.Events):
        def new_password(self):
            log.append("ASK")
            return "Hemmelig1"
    assert C.first_login("192.168.1.99", "admin", Ev()) == "Hemmelig1"
    # sjekk -> lukk -> spør -> ny innlogging -> send to ganger -> lukk
    assert log == ["login", "close", "ASK", "login", "send", "send", "close"]


def test_connect_turns_ssh_errors_into_clear_message(monkeypatch):
    monkeypatch.setattr(C, "wait_port", lambda *a, **k: True)

    def boom(*a):
        raise OSError("Socket is closed")
    monkeypatch.setattr(C, "first_login", boom)
    with pytest.raises(C.Abort, match="SSH-feil mot 192.168.1.99: OSError: Socket is closed"):
        C.connect("192.168.1.99", "admin", C.Events())


def test_wan_https_always_added(tmp_path):
    y = (ROOT / "site_defaults.example.yaml").read_text().replace('allowaccess: "ping https"', 'allowaccess: "ping"')
    f = tmp_path / "d.yaml"
    f.write_text(y)
    assert C.load_defaults(f)["wan_allowaccess"] == "ping https"
