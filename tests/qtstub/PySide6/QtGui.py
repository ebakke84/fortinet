from unittest.mock import MagicMock
from ._base import Base


class QAction(Base):
    MenuRole = MagicMock()
    triggered = MagicMock()


QBrush = MagicMock(name="QBrush")
QColor = MagicMock(name="QColor")
QDesktopServices = MagicMock(name="QDesktopServices")
QFont = MagicMock(name="QFont")
QKeySequence = MagicMock(name="QKeySequence")
