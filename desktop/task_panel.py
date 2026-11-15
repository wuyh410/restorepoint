"""Persistent task output and progress shared by all workspace pages."""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout, QWidget


class TaskPanel(QFrame):
    cancel_requested = Signal()
    history_requested = Signal()
    export_requested = Signal()
    open_logs_requested = Signal()
    expanded_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.setObjectName('taskPanel')
        self.last_percent = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        header = QHBoxLayout()
        self.title = QLabel('任务与日志')
        self.title.setObjectName('taskTitle')
        header.addWidget(self.title, 1)
        self.outcome = QLabel('就绪')
        self.outcome.setObjectName('taskOutcome')
        header.addWidget(self.outcome)
        self.toggle = QPushButton('展开日志')
        self.expanded = False
        self.toggle.clicked.connect(lambda: self.set_expanded(not self.expanded))
        header.addWidget(self.toggle)
        history = QPushButton('任务历史')
        history.clicked.connect(self.history_requested)
        header.addWidget(history)
        self.cancel_button = QPushButton('取消任务')
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_requested)
        header.addWidget(self.cancel_button)
        layout.addLayout(header)
        row = QHBoxLayout()
        self.phase = QLabel('尚未执行任务')
        self.phase.setObjectName('muted')
        row.addWidget(self.phase, 1)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(210)
        self.progress.setFixedHeight(9)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        row.addWidget(self.progress)
        self.percentage = QLabel('—')
        self.percentage.setMinimumWidth(40)
        row.addWidget(self.percentage)
        layout.addLayout(row)
        self.details = QWidget()
        details = QVBoxLayout(self.details)
        details.setContentsMargins(0, 2, 0, 0)
        details.setSpacing(6)
        self.console = QPlainTextEdit()
        self.console.setObjectName('taskConsole')
        self.console.setReadOnly(True)
        self.console.setMinimumHeight(75)
        self.console.setMaximumBlockCount(4000)
        self.console.setPlaceholderText('运行输出会显示在这里。切换页面时日志和进度保持可见。')
        details.addWidget(self.console, 1)
        toolbar = QHBoxLayout()
        export = QPushButton('导出当前日志')
        export.clicked.connect(self.export_requested)
        toolbar.addWidget(export)
        directory = QPushButton('打开日志目录')
        directory.clicked.connect(self.open_logs_requested)
        toolbar.addWidget(directory)
        toolbar.addStretch()
        details.addLayout(toolbar)
        layout.addWidget(self.details, 1)
        self.set_expanded(False)

    def set_expanded(self, expanded):
        self.expanded = expanded
        self.details.setVisible(expanded)
        self.toggle.setText('收起日志' if expanded else '展开日志')
        self.setMinimumHeight(220 if expanded else 84)
        self.setMaximumHeight(16777215 if expanded else 84)
        self.expanded_changed.emit(expanded)

    def state(self, outcome):
        self.outcome.setText(outcome)
        self.outcome.setProperty('taskState', outcome)
        self.outcome.style().unpolish(self.outcome)
        self.outcome.style().polish(self.outcome)
        self.progress.setProperty('taskState', outcome)
        self.progress.style().unpolish(self.progress)
        self.progress.style().polish(self.progress)

    def begin(self, title):
        self.title.setText(title)
        self.console.clear()
        self.last_percent = None
        self.state('运行中')
        self.set_progress(-1, '准备执行任务')
        self.cancel_button.setEnabled(True)
        self.set_expanded(True)

    def set_progress(self, value, text):
        self.phase.setText(text)
        self.progress.setRange(0, 0 if value < 0 else 100)
        if value < 0:
            self.percentage.setText('处理中')
        else:
            self.last_percent = min(100, max(0, value))
            self.progress.setValue(self.last_percent)
            self.percentage.setText(str(self.last_percent) + '%')

    def finish(self, outcome):
        self.state(outcome)
        self.cancel_button.setEnabled(False)
        self.progress.setRange(0, 100)
        if outcome == '已完成':
            self.progress.setValue(100)
            self.percentage.setText('100%')
        else:
            self.progress.setValue(self.last_percent or 0)
            self.percentage.setText('—')
            self.set_expanded(True)
        self.phase.setText(outcome)
