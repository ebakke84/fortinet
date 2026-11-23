#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FortiGate Setup – Mac-app (PySide6) oppå den samme kjernen som Terminal-versjonen.

Flyt:  Koble til  →  Skjema  →  Oppsummering  →  Konfigurering
Start fra kildekode:  python3 app.py
Bygg .app:            ./build_app.sh
"""
from __future__ import annotations

import contextlib
import os
import shutil
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QDesktopServices, QFont, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QRadioButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)

from fgt import core as C

GREEN, RED, ORANGE, GREY = "#1f9d55", "#d93025", "#c77700", "#6b7280"

STYLE = f"""
QLabel#title    {{ font-size: 22px; font-weight: 600; }}
QLabel#subtitle {{ color: {GREY}; }}
QLabel#error    {{ color: {RED}; font-size: 12px; }}
QLabel#value    {{ font-weight: 600; }}
QLabel#key      {{ color: {GREY}; }}
QLabel#note     {{ color: {GREY}; }}
QLabel#warn     {{ color: {ORANGE}; }}
QLineEdit[invalid="true"] {{ border: 1px solid {RED}; border-radius: 4px; }}
QPushButton#primary {{ font-weight: 600; padding: 6px 18px; }}
QListWidget {{ border: none; font-size: 14px; }}
"""


# ───────────────────────────── tråd og hendelser ─────────────────────────────

class Bridge(QObject):
    step_start = Signal(str)
    step_end = Signal(str, bool)
    message = Signal(str, str)            # (kind, text)  kind: info|ok|warn
    ask = Signal(object)                  # dict, besvares i hovedtråden (blokkerende)


class GuiEvents(C.Events):
    """Kjernens hendelser, sendt fra arbeidstråden til vinduet."""

    def __init__(self, bridge: Bridge):
        self.b = bridge

    @contextlib.contextmanager
    def step(self, name: str):
        self.b.step_start.emit(name)
        try:
            yield
        except BaseException:
            self.b.step_end.emit(name, False)
            raise
        self.b.step_end.emit(name, True)

    def info(self, msg): self.b.message.emit("info", msg)
    def ok(self, msg): self.b.message.emit("ok", msg)
    def warn(self, msg): self.b.message.emit("warn", msg)

    def _ask(self, kind: str, msg: str = ""):
        req = {"kind": kind, "msg": msg, "result": None}
        self.b.ask.emit(req)              # BlockingQueuedConnection: venter på svar
        if req["result"] is None:
            raise C.Abort("Avbrutt.")
        return req["result"]

    def confirm(self, msg): self._ask("confirm", msg)
    def new_password(self): return self._ask("new_password")
    def password(self, prompt): return self._ask("password", prompt)


class Worker(QThread):
    done = Signal(object)
    failed = Signal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn())
        except BaseException as e:  # noqa: BLE001 – alt skal vises i vinduet
            e.tb = traceback.format_exc()
            self.failed.emit(e)


# ───────────────────────────── små byggeklosser ─────────────────────────────

def label(text: str = "", name: str | None = None, wrap: bool = True) -> QLabel:
    lb = QLabel(text)
    if name:
        lb.setObjectName(name)
    lb.setWordWrap(wrap)
    lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lb


def header(title: str, subtitle: str = "") -> QWidget:
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 8)
    w.t, w.s = label(title, "title"), label(subtitle, "subtitle")
    v.addWidget(w.t)
    v.addWidget(w.s)
    w.s.setVisible(bool(subtitle))
    return w


def disconnect_all(*signals):
    for sig in signals:
        with contextlib.suppress(RuntimeError, TypeError):
            sig.disconnect()


def brush(color: str) -> QBrush:
    return QBrush(QColor(color))


def hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setFrameShadow(QFrame.Shadow.Sunken)
    return f


def buttons(*items) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 8, 0, 0)
    h.addStretch(1)
    for b in items:
        h.addWidget(b)
    return w


def scroll(inner: QWidget) -> QScrollArea:
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.Shape.NoFrame)
    sa.setWidget(inner)
    return sa


class FieldForm:
    """QFormLayout med feilmelding under hvert felt."""

    def __init__(self, layout: QFormLayout):
        self.lay = layout
        self.err: dict[str, QLabel] = {}
        self.widgets: dict[str, QWidget] = {}
        self.rows: dict[str, list[int]] = {}

    def add(self, key: str, text: str, widget: QWidget, hint: str = ""):
        row = self.lay.rowCount()
        self.lay.addRow(text, widget)
        rows = [row]
        if hint:
            self.lay.addRow("", label(hint, "note"))
            rows.append(row + len(rows))
        e = label("", "error")
        e.hide()
        self.lay.addRow(e)
        self.err[key], self.widgets[key] = e, widget
        self.rows[key] = rows
        return widget

    def set_visible(self, key: str, visible: bool):
        for r in self.rows[key]:
            self.lay.setRowVisible(r, visible)
        if not visible:
            self.err[key].hide()

    def show_errors(self, errors: dict[str, str]):
        for k, e in self.err.items():
            msg = errors.get(k, "")
            e.setText(msg)
            e.setVisible(bool(msg))
            w = self.widgets[k]
            w.setProperty("invalid", bool(msg))
            w.style().unpolish(w)
            w.style().polish(w)


# ───────────────────────────── sider ─────────────────────────────

class ConnectPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        v = QVBoxLayout(self)
        v.addWidget(header("Koble til FortiGate",
                           "Koble Mac-en til en LAN-port på en fabrikknullstilt boks og vent "
                           "til den har fått en 192.168.1.x-adresse."))
        f = QFormLayout()
        self.host = QLineEdit("192.168.1.99")
        self.user = QLineEdit("admin")
        f.addRow("FortiGate-adresse", self.host)
        f.addRow("Bruker", self.user)
        v.addLayout(f)
        self.log = QListWidget()
        v.addWidget(self.log, 1)
        self.offline_btn = QPushButton("Åpne lagret boks-data …")
        self.offline_btn.setToolTip("Prøvekjøring uten boks: velg en mappe med status.txt, interface.txt osv. "
                                    "(f.eks. tests/fixtures/61F eller raw/ fra en tidligere kjøring)")
        self.go = QPushButton("Koble til")
        self.go.setObjectName("primary")
        self.go.setDefault(True)
        v.addWidget(buttons(self.offline_btn, self.go))
        self.go.clicked.connect(lambda: win.connect_box(self.host.text().strip(), self.user.text().strip()))
        self.offline_btn.clicked.connect(win.open_offline)

    def busy(self, on: bool):
        for w in (self.go, self.offline_btn, self.host, self.user):
            w.setEnabled(not on)


class FormPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.info: dict = {}
        self.d: dict = {}
        self.dhcp_touched = False
        outer = QVBoxLayout(self)
        self.head = header("Oppsett", "")
        outer.addWidget(self.head)
        self.warn_lbl = label("", "warn")
        self.warn_lbl.hide()
        outer.addWidget(self.warn_lbl)

        inner = QWidget()
        v = QVBoxLayout(inner)
        v.setContentsMargins(0, 0, 12, 0)

        # Grunnoppsett
        g = QGroupBox("Grunnoppsett")
        self.fg = FieldForm(QFormLayout(g))
        self.hostname = self.fg.add("hostname", "Hostname", QLineEdit())
        self.hostname.setPlaceholderText("f.eks. kunde-oslo-fw01")
        v.addWidget(g)

        # LAN
        g = QGroupBox("LAN")
        self.fl = FieldForm(QFormLayout(g))
        self.lan_iface = self.fl.add("lan_iface", "Interface", QComboBox())
        self.lan_ip = self.fl.add("lan_ip", "IP / prefiks", QLineEdit())
        self.lan_ip.setPlaceholderText("f.eks. 10.20.30.1/24")
        self.dhcp_on = self.fl.add("dhcp_enabled", "DHCP-server", QCheckBox("Slå på DHCP for LAN"))
        self.dhcp_on.setChecked(True)
        self.dhcp_start = self.fl.add("dhcp_start", "DHCP fra", QLineEdit())
        self.dhcp_end = self.fl.add("dhcp_end", "DHCP til", QLineEdit())
        self.dns = self.fl.add("dns", "DNS-servere", QLineEdit(), "Kommaseparert, maks 4")
        v.addWidget(g)

        # WAN
        g = QGroupBox("WAN")
        gv = QVBoxLayout(g)
        mode_row = QHBoxLayout()
        self.single = QRadioButton("Én WAN")
        self.sdwan = QRadioButton("To WAN med SD-WAN")
        self.mode_grp = QButtonGroup(self)
        self.mode_grp.addButton(self.single)
        self.mode_grp.addButton(self.sdwan)
        mode_row.addWidget(self.single)
        mode_row.addWidget(self.sdwan)
        mode_row.addStretch(1)
        gv.addLayout(mode_row)
        grid = QGridLayout()
        self.wan_boxes, self.fw = [], []
        for n in (1, 2):
            box = QGroupBox(f"WAN{n}")
            ff = FieldForm(QFormLayout(box))
            w = {
                "port": ff.add(f"wan{n}_port", "Port", QComboBox()),
                "mode": ff.add(f"wan{n}_mode", "Adressering", QComboBox()),
                "ip": ff.add(f"wan{n}_ip", "IP / prefiks", QLineEdit()),
                "gateway": ff.add(f"wan{n}_gateway", "Gateway", QLineEdit()),
                "alias": ff.add(f"wan{n}_alias", "Navn", QLineEdit(), "Valgfritt, f.eks. Telenor (maks 25 tegn)"),
            }
            w["mode"].addItem("DHCP", "dhcp")
            w["mode"].addItem("Statisk", "static")
            w["ip"].setPlaceholderText("f.eks. 84.208.10.10/29")
            w["mode"].currentIndexChanged.connect(self.update_visibility)
            w["port"].currentIndexChanged.connect(self.update_port_hint)
            self.wan_boxes.append((box, w))
            self.fw.append(ff)
            grid.addWidget(box, 0, n - 1)
        gv.addLayout(grid)
        self.port_hint = label("", "warn")
        self.port_hint.hide()
        gv.addWidget(self.port_hint)
        v.addWidget(g)
        v.addStretch(1)
        outer.addWidget(scroll(inner), 1)

        back = QPushButton("Tilbake")
        nxt = QPushButton("Neste")
        nxt.setObjectName("primary")
        nxt.setDefault(True)
        outer.addWidget(buttons(back, nxt))
        back.clicked.connect(win.restart)
        nxt.clicked.connect(self.submit)
        self.single.toggled.connect(self.update_visibility)
        self.dhcp_on.toggled.connect(self.update_visibility)
        self.lan_ip.textChanged.connect(self.suggest)
        self.lan_iface.currentIndexChanged.connect(self.fill_ports)
        for w in (self.dhcp_start, self.dhcp_end):
            w.textEdited.connect(lambda _t: setattr(self, "dhcp_touched", True))

    # ── fyll inn fra boksen ──
    def load(self, info: dict, d: dict, warns: list[str]):
        self.info, self.d = info, d
        v = ".".join(map(str, info["version"]))
        self.head.t.setText(f"FortiGate-{info['model']}")
        self.head.s.setText(
            f"FortiOS {v}  ·  serienummer {info['serial']}  ·  koblet via {info['mgmt_iface'] or 'ukjent'}")
        self.head.s.show()
        self.warn_lbl.setText("\n".join("⚠  " + w for w in warns))
        self.warn_lbl.setVisible(bool(warns))
        fd = C.form_defaults(info, d)
        self.lan_iface.blockSignals(True)
        self.lan_iface.clear()
        for name, node in info["ifaces"].items():
            if (C.first(node, "type") in ("tunnel", "vap-switch") or name in info["aggregate_of"]
                    or name in C.NON_PORTS):
                continue
            self.lan_iface.addItem(name)
        self.lan_iface.setCurrentText(fd["lan_iface"])
        self.lan_iface.blockSignals(False)
        self.dns.setText(fd["dns"])
        (self.sdwan if fd["wan_mode"] == "sdwan" else self.single).setChecked(True)
        self.fill_ports()
        for (box, w), key in zip(self.wan_boxes, ("wan1_port", "wan2_port")):
            i = w["port"].findData(fd[key])
            if i >= 0:
                w["port"].setCurrentIndex(i)
        self.dhcp_touched = False
        self.suggest()
        self.update_visibility()
        for ff in [self.fg, self.fl, *self.fw]:
            ff.show_errors({})
        self.hostname.setFocus()

    def fill_ports(self, *_):
        if not self.info:
            return
        choices = C.port_choices(self.info, self.lan_iface.currentText())
        for _box, w in self.wan_boxes:
            cur = w["port"].currentData()
            w["port"].blockSignals(True)
            w["port"].clear()
            for c in choices:
                w["port"].addItem(f"{c.name}   —   {c.label}" if c.label else c.name, c.name)
                if not c.usable:
                    with contextlib.suppress(AttributeError):   # grå ut porter som ikke kan brukes
                        w["port"].model().item(w["port"].count() - 1).setEnabled(False)
            i = w["port"].findData(cur)
            w["port"].setCurrentIndex(i if i >= 0 else 0)
            w["port"].blockSignals(False)
        self.update_port_hint()

    def suggest(self, *_):
        if self.dhcp_touched or not self.d:
            return
        s = C.suggest_dhcp(self.lan_ip.text(), self.d)
        if s:
            self.dhcp_start.setText(s[0])
            self.dhcp_end.setText(s[1])

    def update_visibility(self, *_):
        dh = self.dhcp_on.isChecked()
        for k in ("dhcp_start", "dhcp_end", "dns"):
            self.fl.set_visible(k, dh)
        self.wan_boxes[1][0].setVisible(self.sdwan.isChecked())
        for n, ((_box, w), ff) in enumerate(zip(self.wan_boxes, self.fw), 1):
            st = w["mode"].currentData() == "static"
            ff.set_visible(f"wan{n}_ip", st)
            ff.set_visible(f"wan{n}_gateway", st)
        self.update_port_hint()

    def update_port_hint(self, *_):
        if not self.info:
            return
        hints = []
        count = 2 if self.sdwan.isChecked() else 1
        for _box, w in self.wan_boxes[:count]:
            p = w["port"].currentData()
            if p in self.info["vswitch_of"]:
                hints.append(f"⚠  {p} flyttes ut av switchen '{self.info['vswitch_of'][p]}'. "
                             "Pass på at Mac-en IKKE står i denne porten.")
        self.port_hint.setText("\n".join(hints))
        self.port_hint.setVisible(bool(hints))

    # ── send inn ──
    def form(self) -> dict:
        f = {
            "hostname": self.hostname.text(), "lan_iface": self.lan_iface.currentText(),
            "lan_ip": self.lan_ip.text(), "dhcp_enabled": self.dhcp_on.isChecked(),
            "dhcp_start": self.dhcp_start.text(), "dhcp_end": self.dhcp_end.text(),
            "dns": self.dns.text(), "wan_mode": "sdwan" if self.sdwan.isChecked() else "single",
        }
        for n, (_box, w) in enumerate(self.wan_boxes, 1):
            f[f"wan{n}_port"] = w["port"].currentData() or ""
            f[f"wan{n}_mode"] = w["mode"].currentData()
            for k in ("ip", "gateway", "alias"):
                f[f"wan{n}_{k}"] = w[k].text()
        return f

    def submit(self, *_):
        answers, err = C.validate_form(self.info, self.d, self.form())
        for ff in [self.fg, self.fl, *self.fw]:
            ff.show_errors(err)
        if err:
            self.win.statusBar().showMessage(f"Rett {len(err)} felt markert i rødt", 6000)
            return
        self.win.make_plan(answers)


class SummaryPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        v = QVBoxLayout(self)
        v.addWidget(header("Oppsummering", "Sjekk at alt stemmer før boksen konfigureres."))
        inner = QWidget()
        self.iv = QVBoxLayout(inner)
        v.addWidget(scroll(inner), 1)
        self.cli = QPlainTextEdit()
        self.cli.setReadOnly(True)
        self.cli.setFont(QFont("Menlo", 12))
        self.cli.hide()
        v.addWidget(self.cli, 1)
        self.toggle = QPushButton("Vis generert CLI")
        self.toggle.setCheckable(True)
        self.toggle.toggled.connect(lambda on: (self.cli.setVisible(on),
                                                self.toggle.setText("Skjul generert CLI" if on else "Vis generert CLI")))
        back = QPushButton("Tilbake")
        self.run = QPushButton("Konfigurer boksen")
        self.run.setObjectName("primary")
        v.addWidget(buttons(self.toggle, back, self.run))
        back.clicked.connect(lambda: win.show_page(win.form_page))
        self.run.clicked.connect(win.confirm_run)

    def load(self, plan: C.Plan, offline: bool):
        while self.iv.count():
            it = self.iv.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        r = 0
        for row in C.summary_rows(plan.ctx):
            if row is None:
                grid.addWidget(hline(), r, 0, 1, 2)
            else:
                grid.addWidget(label(row[0], "key", wrap=False), r, 0, Qt.AlignmentFlag.AlignTop)
                grid.addWidget(label(row[1], "value"), r, 1)
            r += 1
        grid.setColumnStretch(1, 1)
        box = QWidget()
        box.setLayout(grid)
        self.iv.addWidget(box)
        for n in C.summary_notes(plan.ctx):
            self.iv.addWidget(label("·  " + n, "note"))
        for p, lines in plan.refs.items():
            for ln in lines:
                self.iv.addWidget(label(f"⚠  {p} er i bruk: {ln}", "warn"))
        if offline:
            self.iv.addWidget(label("Prøvekjøring med lagret data – ingenting sendes til en boks.", "warn"))
        self.iv.addWidget(label(f"Generert CLI er lagret i {plan.out_dir / 'config.txt'}", "note"))
        self.iv.addStretch(1)
        self.cli.setPlainText(plan.cfg_text)
        self.toggle.setChecked(False)
        self.run.setEnabled(not offline)


class RunPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        v = QVBoxLayout(self)
        self.head = header("Konfigurerer", "Ikke koble fra kabelen mens dette pågår.")
        v.addWidget(self.head)
        self.list = QListWidget()
        v.addWidget(self.list, 1)
        self.result = label("")
        self.result.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.result.setOpenExternalLinks(True)
        self.result.hide()
        v.addWidget(self.result)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFont(QFont("Menlo", 11))
        self.detail.setMaximumHeight(160)
        self.detail.hide()
        v.addWidget(self.detail)
        self.out_btn = QPushButton("Åpne output-mappe")
        self.gui_btn = QPushButton("Åpne FortiGate i nettleseren")
        self.new_btn = QPushButton("Sett opp en ny boks")
        self.new_btn.setObjectName("primary")
        self.btns = buttons(self.out_btn, self.gui_btn, self.new_btn)
        self.btns.hide()
        v.addWidget(self.btns)
        self.out_btn.clicked.connect(win.open_output)
        self.gui_btn.clicked.connect(win.open_box_gui)
        self.new_btn.clicked.connect(win.restart)
        self.items: dict[str, QListWidgetItem] = {}

    def reset(self):
        self.list.clear()
        self.items.clear()
        self.result.hide()
        self.detail.hide()
        self.btns.hide()
        self.head.t.setText("Konfigurerer")

    def add(self, text: str, color: str | None = None) -> QListWidgetItem:
        it = QListWidgetItem(text)
        if color:
            it.setForeground(brush(color))
        self.list.addItem(it)
        self.list.scrollToBottom()
        return it

    def step_start(self, name):
        self.items[name] = self.add(f"⏳  {name} …")

    def step_end(self, name, ok):
        it = self.items.get(name) or self.add(name)
        it.setText(f"{'✔' if ok else '✖'}  {name}")
        it.setForeground(brush(GREEN if ok else RED))

    def message(self, kind, text):
        icon, col = {"ok": ("✔", GREEN), "warn": ("⚠", ORANGE)}.get(kind, ("·", GREY))
        self.add(f"{icon}  {text}", col)

    def finish_ok(self, ctx: dict, status: C.Status):
        self.head.t.setText("Ferdig")
        if status.default_routes:
            self.message("ok", "Default-rute finnes")
        else:
            self.message("warn", "Ingen default-rute ennå – er WAN-kabelen koblet til?")
        for port, state in status.sdwan:
            if state == "alive":
                self.message("ok", f"SD-WAN {port}: oppe")
            else:
                self.message("warn" if state else "info", f"SD-WAN {port}: {state or 'ingen status ennå'}")
        url = C.gui_url(ctx)
        self.result.setText(f"<span style='color:{GREEN};font-size:16px;font-weight:600'>"
                            f"✔  {ctx['hostname']} er konfigurert og lagret</span><br>"
                            f"GUI: <a href='{url}'>{url}</a> &nbsp; · &nbsp; SSH: ssh admin@{ctx['lan']['ip']}")
        self.result.show()
        self.gui_btn.show()
        self.btns.show()

    def finish_fail(self, e: BaseException):
        self.head.t.setText("Stoppet")
        self.result.setText(f"<span style='color:{RED};font-size:15px;font-weight:600'>✖  {e}</span><br>"
                            "Endringene frem til feilen er lagret på boksen. Rett feilen og kjør på nytt, "
                            "eller start helt på nytt med <code>execute factoryreset</code> på boksen.")
        self.result.show()
        if isinstance(e, C.ApplyError):
            self.detail.setPlainText("Svar fra FortiGaten:\n" + C.LOG.mask(e.output.strip()))
            self.detail.show()
        self.gui_btn.hide()
        self.btns.show()


# ───────────────────────────── innstillinger ─────────────────────────────

class SettingsDialog(QDialog):
    def __init__(self, data_dir: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Innstillinger")
        self.resize(760, 720)
        self.data_dir = data_dir
        self.yaml_path = data_dir / "site_defaults.yaml"
        self.env_path = data_dir / "secrets.env"
        v = QVBoxLayout(self)
        v.addWidget(label("Standardverdier som brukes på alle bokser (site_defaults.yaml). "
                          "Behold innrykket – bruk mellomrom, ikke tab.", "note"))
        self.edit = QPlainTextEdit(self.yaml_path.read_text(encoding="utf-8") if self.yaml_path.exists() else "")
        self.edit.setFont(QFont("Menlo", 12))
        v.addWidget(self.edit, 1)

        g = QGroupBox("SNMP-passord (lagres i secrets.env)")
        f = QFormLayout(g)
        C.load_env_file(self.env_path)
        self.auth = QLineEdit(os.environ.get("FGT_SNMP_AUTH", ""))
        self.priv = QLineEdit(os.environ.get("FGT_SNMP_PRIV", ""))
        for w in (self.auth, self.priv):
            w.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        f.addRow("Auth-passord", self.auth)
        f.addRow("Priv-passord", self.priv)
        v.addWidget(g)

        self.err = label("", "error")
        self.err.hide()
        v.addWidget(self.err)
        folder = QPushButton("Åpne datamappe")
        folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(data_dir))))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(folder)
        row.addStretch(1)
        row.addWidget(bb)
        v.addLayout(row)

    def save(self, *_):
        if "'" in self.auth.text() + self.priv.text():
            self.err.setText("SNMP-passordene kan ikke inneholde '")
            self.err.show()
            return
        os.environ["FGT_SNMP_AUTH"], os.environ["FGT_SNMP_PRIV"] = self.auth.text(), self.priv.text()
        tmp = self.yaml_path.with_suffix(".check.yaml")
        try:
            tmp.write_text(self.edit.toPlainText(), encoding="utf-8")
            C.load_defaults(tmp)
        except Exception as e:  # noqa: BLE001
            self.err.setText(f"Ikke lagret: {e}")
            self.err.show()
            return
        finally:
            tmp.unlink(missing_ok=True)
        if self.yaml_path.exists():
            shutil.copy2(self.yaml_path, self.yaml_path.with_suffix(".yaml.bak"))
        self.yaml_path.write_text(self.edit.toPlainText(), encoding="utf-8")
        self.env_path.write_text(
            "# SNMP-passord. Leses av appen og menyen. Ikke del denne fila.\n"
            f"FGT_SNMP_AUTH='{self.auth.text()}'\nFGT_SNMP_PRIV='{self.priv.text()}'\n", encoding="utf-8")
        self.env_path.chmod(0o600)
        self.accept()


class NewPasswordDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Nytt FortiGate-passord")
        v = QVBoxLayout(self)
        v.addWidget(label("Fabrikknullstilt boks: velg nytt admin-passord for FortiGaten. "
                          "Det brukes også resten av oppsettet."))
        f = QFormLayout()
        self.p1, self.p2 = QLineEdit(), QLineEdit()
        for w in (self.p1, self.p2):
            w.setEchoMode(QLineEdit.EchoMode.Password)
        f.addRow("Nytt passord", self.p1)
        f.addRow("Gjenta", self.p2)
        v.addLayout(f)
        self.err = label("", "error")
        v.addWidget(self.err)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.check)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def check(self, *_):
        if not self.p1.text() or self.p1.text() != self.p2.text():
            self.err.setText("Passordene er tomme eller like ikke")
            return
        self.accept()


# ───────────────────────────── hovedvindu ─────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(C.APP_NAME)
        self.resize(900, 780)
        self.settings = QSettings("FortiGateSetup", "FortiGateSetup")
        self.data_dir = Path(self.settings.value("data_dir", str(C.default_data_dir())))
        made = C.ensure_data_dir(self.data_dir)

        self.bridge = Bridge()
        self.bridge.ask.connect(self.on_ask, Qt.ConnectionType.BlockingQueuedConnection)
        self.ev = GuiEvents(self.bridge)
        self.worker: Worker | None = None
        self.dev = None
        self.pw = ""
        self.raw = self.info = self.d = None
        self.plan: C.Plan | None = None
        self.offline = False
        self.host, self.user = "192.168.1.99", "admin"

        self.stack = QStackedWidget()
        self.connect_page = ConnectPage(self)
        self.form_page = FormPage(self)
        self.summary_page = SummaryPage(self)
        self.run_page = RunPage(self)
        for p in (self.connect_page, self.form_page, self.summary_page, self.run_page):
            wrap = QWidget()
            lay = QVBoxLayout(wrap)
            lay.setContentsMargins(28, 22, 28, 18)
            lay.addWidget(p)
            p.wrap = wrap
            self.stack.addWidget(wrap)
        self.setCentralWidget(self.stack)
        self.build_menu()
        if made:
            self.statusBar().showMessage("Laget " + " og ".join(made) +
                                         " fra eksempel. Fyll inn verdiene under Innstillinger (⌘,).", 15000)

    def build_menu(self):
        m = self.menuBar().addMenu("Fil")
        a = QAction("Innstillinger …", self)
        a.setShortcut(QKeySequence.StandardKey.Preferences)
        a.setMenuRole(QAction.MenuRole.PreferencesRole)
        a.triggered.connect(self.open_settings)
        m.addAction(a)
        a = QAction("Åpne lagret boks-data …", self)
        a.triggered.connect(self.open_offline)
        m.addAction(a)
        a = QAction("Åpne output-mappe", self)
        a.triggered.connect(self.open_output)
        m.addAction(a)
        a = QAction("Velg datamappe …", self)
        a.triggered.connect(self.choose_data_dir)
        m.addAction(a)

    # ── navigasjon ──
    def show_page(self, page):
        self.stack.setCurrentWidget(page.wrap)

    def restart(self, *_):
        if self.dev is not None and not self.offline:
            with contextlib.suppress(Exception):
                self.dev.close()
        self.dev, self.plan, self.offline = None, None, False
        self.connect_page.log.clear()
        self.connect_page.busy(False)
        self.show_page(self.connect_page)

    def run_bg(self, fn, done, failed):
        self.worker = Worker(fn, self)
        self.worker.done.connect(done)
        self.worker.failed.connect(failed)
        self.worker.start()

    # ── innstillinger ──
    def load_settings(self) -> dict:
        C.load_env_file(self.data_dir / "secrets.env")
        return C.load_defaults(self.data_dir / "site_defaults.yaml")

    def open_settings(self, *_):
        SettingsDialog(self.data_dir, self).exec()

    def choose_data_dir(self, *_):
        d = QFileDialog.getExistingDirectory(self, "Velg datamappe (site_defaults.yaml, secrets.env, output/)",
                                             str(self.data_dir))
        if d:
            self.data_dir = Path(d)
            self.settings.setValue("data_dir", d)
            C.ensure_data_dir(self.data_dir)
            self.statusBar().showMessage(f"Datamappe: {d}", 8000)

    def open_output(self, *_):
        p = self.plan.out_dir if self.plan else self.data_dir / "output"
        p.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def open_box_gui(self, *_):
        if self.plan:
            QDesktopServices.openUrl(QUrl(C.gui_url(self.plan.ctx)))

    # ── steg 1: koble til ──
    def log_to_connect(self):
        b = self.bridge
        disconnect_all(b.step_start, b.step_end, b.message)
        log = self.connect_page.log
        b.step_start.connect(lambda n: log.addItem(f"⏳  {n} …"))
        b.step_end.connect(lambda n, ok: log.addItem(f"{'✔' if ok else '✖'}  {n}"))
        b.message.connect(lambda k, t: log.addItem(("⚠  " if k == "warn" else "·  ") + t))

    def connect_box(self, host: str, user: str):
        try:
            self.d = self.load_settings()
        except C.Abort as e:
            QMessageBox.warning(self, "Innstillinger", f"{e}\n\nÅpne Innstillinger (⌘,) og rett feilen.")
            return
        self.host, self.user, self.offline = host or "192.168.1.99", user or "admin", False
        self.log_to_connect()
        self.connect_page.busy(True)

        def work():
            dev, pw = C.connect(self.host, self.user, self.ev)
            raw, info = C.read_box(dev, self.host, self.ev)
            return dev, pw, raw, info
        self.run_bg(work, self.connected, self.connect_failed)

    def open_offline(self, *_):
        d = QFileDialog.getExistingDirectory(self, "Velg mappe med lagret boks-data (status.txt, interface.txt …)",
                                             str(C.RESOURCE_DIR / "tests" / "fixtures"))
        if not d:
            return
        try:
            self.d = self.load_settings()
            dev = C.OfflineDevice(Path(d))
            raw, info = C.read_box(dev, self.host, C.Events())
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Kunne ikke lese data", str(e))
            return
        self.offline = True
        self.connected((dev, "", raw, info))

    def connected(self, res):
        self.dev, self.pw, self.raw, self.info = res
        self.connect_page.busy(False)
        errors, warns = C.preflight(self.info)
        if errors:
            QMessageBox.critical(self, "Kan ikke fortsette", "\n".join(errors))
            return
        self.form_page.load(self.info, self.d, warns)
        self.show_page(self.form_page)

    def connect_failed(self, e):
        self.connect_page.busy(False)
        log = self.connect_page.log
        log.addItem(f"✖  {e or type(e).__name__}")
        if not isinstance(e, C.Abort):
            log.addItem(f"·  {type(e).__name__} – detaljer i Terminal hvis appen startes derfra")
            print(getattr(e, "tb", ""), file=sys.stderr)

    # ── steg 2–3: plan ──
    def make_plan(self, answers: dict):
        try:
            self.plan = C.make_plan(self.dev, self.info, self.raw, self.d, answers, self.data_dir)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Kunne ikke lage konfig", str(e))
            return
        self.summary_page.load(self.plan, self.offline)
        self.show_page(self.summary_page)

    # ── steg 4: kjør ──
    def confirm_run(self, *_):
        if self.plan.refs:
            r = QMessageBox.question(self, "WAN-porter i bruk",
                                     "Noen WAN-porter er i bruk andre steder, og SD-WAN-steget vil sannsynligvis "
                                     "feile. Fortsette likevel?")
            if r != QMessageBox.StandardButton.Yes:
                return
        r = QMessageBox.question(self, "Konfigurer boksen",
                                 f"Konfigurere {self.plan.ctx['hostname']} nå?\n\n"
                                 "Hvert steg lagres på boksen med en gang.")
        if r != QMessageBox.StandardButton.Yes:
            return
        rp = self.run_page
        rp.reset()
        b = self.bridge
        disconnect_all(b.step_start, b.step_end, b.message)
        b.step_start.connect(rp.step_start)
        b.step_end.connect(rp.step_end)
        b.message.connect(rp.message)
        self.show_page(rp)
        opts = C.RunOptions(host=self.host, user=self.user, mac_alias=True)
        plan, dev, pw = self.plan, self.dev, self.pw
        self.run_bg(lambda: C.execute(dev, plan, opts, pw, self.ev, C.OsaRunner()),
                    lambda st: rp.finish_ok(plan.ctx, st), rp.finish_fail)

    # ── spørsmål fra arbeidstråden ──
    def on_ask(self, req: dict):
        kind = req["kind"]
        if kind == "new_password":
            dlg = NewPasswordDialog(self)
            if dlg.exec():
                req["result"] = dlg.p1.text()
        elif kind == "password":
            text, ok = QInputDialog.getText(self, "FortiGate-passord", req["msg"],
                                            QLineEdit.EchoMode.Password)
            if ok and text:
                req["result"] = text
        elif kind == "confirm":
            r = QMessageBox.information(self, "Gjør dette først", req["msg"],
                                        QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
            if r == QMessageBox.StandardButton.Ok:
                req["result"] = True

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            r = QMessageBox.question(self, "Avslutte?", "Konfigureringen pågår. Avslutte likevel?")
            if r != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(C.APP_NAME)
    f = app.font()
    f.setPointSize(max(f.pointSize(), 14))
    app.setFont(f)
    app.setStyleSheet(STYLE)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
