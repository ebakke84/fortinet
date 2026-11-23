"""Tester app.py-logikken med en enkel PySide6-erstatning (tests/qtstub), uten ekte Qt.

Fanger feil i Python-koden (navn, flyt, validering), ikke utseende.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "qtstub"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import app as A  # noqa: E402
import test_core as T  # noqa: E402
from fgt import core as C  # noqa: E402
from PySide6 import QtWidgets as W  # noqa: E402


@pytest.fixture
def win(monkeypatch, tmp_path):
    monkeypatch.setenv("FGT_SNMP_AUTH", "AuthPass123!")
    monkeypatch.setenv("FGT_SNMP_PRIV", "PrivPass456!")
    monkeypatch.setenv("FGT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(C.time, "sleep", lambda s: None)
    monkeypatch.setattr(A.sys, "platform", "linux")          # ingen Mac-alias i test
    C.LOG.path, C.LOG.buf = None, []
    T.FakeDevice.state = {"ip": False, "dhcp": False, "pending": False}
    monkeypatch.setattr(C, "LiveDevice", T.FakeDevice)
    monkeypatch.setattr(C, "wait_port", lambda *a, **k: True)
    monkeypatch.setattr(C, "connect", lambda h, u, ev: (T.FakeDevice(), "pw"))
    W.QMessageBox.question.return_value = W.QMessageBox.StandardButton.Yes
    W.QMessageBox.information.return_value = W.QMessageBox.StandardButton.Ok
    return A.MainWindow()


def fill(fp, **kw):
    fp.hostname.setText(kw.get("hostname", "kunde-fw01"))
    fp.lan_ip.setText(kw.get("lan_ip", "10.20.30.1/24"))


def test_offline_is_dry_run(win):
    W.QFileDialog.getExistingDirectory.return_value = str(ROOT / "tests" / "fixtures" / "61F")
    win.open_offline()
    fp = win.form_page
    assert fp.head.t.text() == "FortiGate-61F"
    assert "modem" not in [i[0] for i in fp.lan_iface._items]
    fp.submit()                                                  # tomt skjema
    assert fp.fg.err["hostname"].isVisible() and fp.fl.err["lan_ip"].isVisible()
    fill(fp)
    assert (fp.dhcp_start.text(), fp.dhcp_end.text()) == ("10.20.30.50", "10.20.30.250")
    fp.submit()
    assert win.stack.current is win.summary_page.wrap
    assert not win.summary_page.run.isEnabled()                 # offline: ingen kjøring


def test_live_run_ok(win):
    win.connect_box("192.168.1.99", "admin")
    fp = win.form_page
    fill(fp)
    fp.single.setChecked(True)
    assert not fp.wan_boxes[1][0].isVisible()
    fp.submit()
    win.confirm_run()
    texts = win.run_page.list.texts()
    assert "✔  Kontrollerer at alt er lagret" in texts
    assert win.run_page.head.t.text() == "Ferdig"
    assert "https://10.20.30.1:8443" in win.run_page.result.text()


def test_live_run_fail_shows_box_reply(win, monkeypatch):
    orig = T.FakeDevice.line

    def bad(self, c):
        if c.startswith("set alias"):
            raise C.ApplyError("wan", c, "command parse error before 'alias'")
        return orig(self, c)
    monkeypatch.setattr(T.FakeDevice, "line", bad)
    win.connect_box("192.168.1.99", "admin")
    fp = win.form_page
    fill(fp)
    fp.wan_boxes[0][1]["alias"].setText("Telenor")
    fp.submit()
    win.confirm_run()
    assert win.run_page.head.t.text() == "Stoppet"
    assert "parse error" in win.run_page.detail.toPlainText()
    assert not win.run_page.gui_btn.isVisible()


def test_settings_validates(win):
    dlg = A.SettingsDialog(win.data_dir)
    dlg.edit.setPlainText("system:\n  admin_timeout: 9999\n")
    dlg.save()
    assert "admin_timeout" in dlg.err.text()
    dlg.edit.setPlainText((ROOT / "site_defaults.example.yaml").read_text())
    dlg.auth.setText("NyAuth12345")
    dlg.priv.setText("NyPriv12345")
    dlg.save()
    assert "FGT_SNMP_AUTH='NyAuth12345'" in (win.data_dir / "secrets.env").read_text()
