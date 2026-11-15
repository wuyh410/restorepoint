"""Value controls that never change a setting in response to the mouse wheel."""
from PySide6.QtCore import QPoint, QPointF, QSize
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QAbstractScrollArea, QComboBox, QSpinBox, QStyledItemDelegate


def scroll_parent(widget, event):
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            viewport = parent.viewport()
            position = viewport.mapFromGlobal(event.globalPosition().toPoint())
            forwarded = QWheelEvent(QPointF(position), event.globalPosition(), event.pixelDelta(),
                                    event.angleDelta(), event.buttons(), event.modifiers(), event.phase(),
                                    event.inverted(), event.source(), event.pointingDevice())
            QApplication.sendEvent(viewport, forwarded)
            event.accept()
            return
        parent = parent.parentWidget()
    event.ignore()


class ValueSpinBox(QSpinBox):
    def wheelEvent(self, event):
        scroll_parent(self, event)


class ComboItemDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        return QSize(size.width(), max(36, option.fontMetrics.height() + 12))


class ValueComboBox(QComboBox):
    def wheelEvent(self, event):
        scroll_parent(self, event)

    def showPopup(self):
        super().showPopup()
        popup = self.view().window()
        if not popup.isVisible():
            return
        available = self.screen().availableGeometry()
        width = min(max(self.width(), popup.width()), available.width())
        height = min(popup.height(), available.height())
        below = self.mapToGlobal(QPoint(0, self.height() + 4))
        above = self.mapToGlobal(QPoint(0, 0)).y() - height - 4
        top = below.y() if below.y() + height <= available.bottom() + 1 else max(available.top(), above)
        left = max(available.left(), min(below.x(), available.right() - width + 1))
        popup.setGeometry(left, top, width, height)
