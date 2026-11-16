"""Value controls that never change a setting in response to the mouse wheel."""
from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QWheelEvent
from PySide6.QtWidgets import QApplication, QAbstractScrollArea, QComboBox, QSpinBox, QSplitter, QSplitterHandle, QStyledItemDelegate, QStyle, QStyleOptionComboBox, QStylePainter


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


class TaskSplitterHandle(QSplitterHandle):
    def __init__(self, orientation, parent):
        super().__init__(orientation, parent)
        self.pressed = False
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.SplitVCursor)
        self.setToolTip('拖动调整日志高度')

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = '#5279bb' if self.pressed else '#7e98bf' if self.underMouse() else '#bdcadc'
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        painter.drawRoundedRect(QRectF((self.width() - 44) / 2, (self.height() - 3) / 2, 44, 3), 1.5, 1.5)

    def enterEvent(self, event):
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.update()

    def mousePressEvent(self, event):
        self.pressed = True
        self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self.pressed = False
        self.update()


class TaskSplitter(QSplitter):
    def createHandle(self):
        return TaskSplitterHandle(self.orientation(), self)


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


class ProfileComboBox(ValueComboBox):
    """Keep the actual name intact; elide only the closed selector's label."""
    def __init__(self):
        super().__init__()
        self.currentTextChanged.connect(self.setToolTip)

    def display_text(self, width):
        text = self.currentText()
        metrics = self.fontMetrics()
        if metrics.horizontalAdvance(text) <= width:
            return text
        left, right = 0, len(text)
        while left < right:
            middle = (left + right + 1) // 2
            if metrics.horizontalAdvance(text[:middle] + '...') <= width:
                left = middle
            else:
                right = middle - 1
        return text[:left] + '...'

    def paintEvent(self, event):
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        rect = self.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, option,
                                           QStyle.SubControl.SC_ComboBoxEditField, self)
        option.currentText = self.display_text(max(0, rect.width() - 2))
        painter = QStylePainter(self)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)
