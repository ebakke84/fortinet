from unittest.mock import MagicMock
from ._base import Base, Signal, BoundSignal


class QWidget(Base):
    pass


class QApplication(Base):
    pass


class QMainWindow(QWidget):
    def statusBar(self):
        if not hasattr(self, "_sb"):
            self._sb = MagicMock()
        return self._sb


class QLabel(QWidget):
    def __init__(self, text="", *a):
        super().__init__()
        self._text = text
    def setText(self, t): self._text = t
    def text(self): return self._text


class QLineEdit(QWidget):
    textChanged = Signal(str)
    textEdited = Signal(str)
    EchoMode = MagicMock()
    def __init__(self, text="", *a):
        super().__init__()
        self._text = text
    def setText(self, t):
        self._text = t
        self.textChanged.emit(t)
    def type(self, t):
        self._text = t
        self.textEdited.emit(t)
        self.textChanged.emit(t)
    def text(self): return self._text


class QPlainTextEdit(QWidget):
    def __init__(self, text="", *a):
        super().__init__()
        self._text = text
    def setPlainText(self, t): self._text = t
    def toPlainText(self): return self._text


class _Item:
    def __init__(self): self.enabled = True
    def setEnabled(self, v): self.enabled = v


class _Model:
    def __init__(self, combo): self.combo = combo
    def item(self, i): return self.combo._items[i][2]


class QComboBox(QWidget):
    currentIndexChanged = Signal(int)
    def __init__(self, *a):
        super().__init__()
        self._items, self._idx, self._blocked = [], -1, False
    def blockSignals(self, b): self._blocked = b
    def addItem(self, text, data=None):
        self._items.append((text, data, _Item()))
        if self._idx < 0:
            self.setCurrentIndex(0)
    def clear(self):
        self._items, self._idx = [], -1
    def count(self): return len(self._items)
    def model(self): return _Model(self)
    def setCurrentIndex(self, i):
        self._idx = i
        if not self._blocked:
            self.currentIndexChanged.emit(i)
    def currentIndex(self): return self._idx
    def currentData(self): return self._items[self._idx][1] if 0 <= self._idx < len(self._items) else None
    def currentText(self): return self._items[self._idx][0] if 0 <= self._idx < len(self._items) else ""
    def setCurrentText(self, t):
        for i, it in enumerate(self._items):
            if it[0] == t:
                self.setCurrentIndex(i)
    def findData(self, d):
        for i, it in enumerate(self._items):
            if it[1] == d:
                return i
        return -1


class _Check(QWidget):
    toggled = Signal(bool)
    def __init__(self, text="", *a):
        super().__init__()
        self._c = False
    def setChecked(self, v):
        if bool(v) != self._c:
            self._c = bool(v)
            self.toggled.emit(self._c)
    def isChecked(self): return self._c


class QCheckBox(_Check):
    pass


class QRadioButton(_Check):
    pass


class QButtonGroup(Base):
    def __init__(self, *a):
        super().__init__()
        self._b = []
    def addButton(self, b):
        self._b.append(b)
        orig = b.setChecked
        def set_checked(v, orig=orig, me=b):
            if v:
                for o in self._b:
                    if o is not me and o.isChecked():
                        o._c = False
                        o.toggled.emit(False)
            orig(v)
        b.setChecked = set_checked


class QPushButton(QWidget):
    clicked = Signal()
    toggled = Signal(bool)
    def __init__(self, text="", *a):
        super().__init__()
        self._text = text
    def setText(self, t): self._text = t


class QListWidgetItem(Base):
    def __init__(self, text=""):
        super().__init__()
        self._text = text
    def setText(self, t): self._text = t
    def text(self): return self._text


class QListWidget(QWidget):
    def __init__(self, *a):
        super().__init__()
        self.items = []
    def addItem(self, it):
        self.items.append(it if isinstance(it, QListWidgetItem) else QListWidgetItem(it))
    def clear(self): self.items = []
    def texts(self): return [i.text() for i in self.items]


class _Layout(Base):
    def __init__(self, parent=None, *a):
        super().__init__()
        self._n = 0
        self._w = []
    def addWidget(self, w, *a): self._w.append(w)
    def addLayout(self, *a): ...
    def addStretch(self, *a): ...
    def count(self): return 0


class QVBoxLayout(_Layout):
    pass


class QHBoxLayout(_Layout):
    pass


class QGridLayout(_Layout):
    pass


class QFormLayout(_Layout):
    def addRow(self, *a): self._n += 1
    def rowCount(self): return self._n
    def setRowVisible(self, r, v): ...


class QStackedWidget(QWidget):
    def addWidget(self, w): ...
    def setCurrentWidget(self, w): self.current = w


class QGroupBox(QWidget):
    pass


class QScrollArea(QWidget):
    pass


class QFrame(QWidget):
    Shape = MagicMock()
    Shadow = MagicMock()


class QDialog(QWidget):
    DialogCode = MagicMock()
    def exec(self): return 1
    def accept(self): ...
    def reject(self): ...


class QDialogButtonBox(QWidget):
    StandardButton = MagicMock()
    accepted = Signal()
    rejected = Signal()


QMessageBox = MagicMock(name="QMessageBox")
QInputDialog = MagicMock(name="QInputDialog")
QFileDialog = MagicMock(name="QFileDialog")
