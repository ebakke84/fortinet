from unittest.mock import MagicMock
from ._base import Base, Signal

Qt = MagicMock(name="Qt")
QUrl = MagicMock(name="QUrl")


class QObject(Base):
    pass


class QSettings(Base):
    def value(self, k, default=None): return default
    def setValue(self, k, v): ...


class QThread(QObject):
    def start(self):
        self.run()

    def isRunning(self): return False


class QTimer(QObject):
    timeout = Signal()
    def start(self, *a): ...
    def stop(self): ...
