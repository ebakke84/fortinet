"""Minimal, tillatende PySide6-stub for å teste Python-logikken i app.py uten Qt."""
from unittest.mock import MagicMock


class _Any(MagicMock):
    pass


class Signal:
    def __init__(self, *types):
        self.name = None

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        key = "_sig_" + self.name
        if key not in obj.__dict__:
            obj.__dict__[key] = BoundSignal()
        return obj.__dict__[key]


class BoundSignal:
    def __init__(self):
        self.slots = []

    def connect(self, slot, *a):
        self.slots.append(slot)

    def disconnect(self):
        if not self.slots:
            raise RuntimeError("no connections")
        self.slots = []

    def emit(self, *args):
        for s in list(self.slots):
            s(*args)


class Base:
    def __init__(self, *a, **k):
        self._kids = []
        self._visible = True
        self._enabled = True
        self._props = {}

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        m = MagicMock(name=f"{type(self).__name__}.{name}")
        object.__setattr__(self, name, m)
        return m

    def setVisible(self, v): self._visible = bool(v)
    def isVisible(self): return self._visible
    def show(self): self._visible = True
    def hide(self): self._visible = False
    def setEnabled(self, v): self._enabled = bool(v)
    def isEnabled(self): return self._enabled
    def setProperty(self, k, v): self._props[k] = v
    def property(self, k): return self._props.get(k)
