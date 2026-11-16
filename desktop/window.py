import argparse
from dataclasses import asdict, replace
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sys

from PySide6.QtCore import QLockFile, QStandardPaths, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QPalette
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QListView, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QScrollArea, QSizePolicy, QSpacerItem, QSpinBox, QSplitter, QStackedWidget,
    QStyledItemDelegate, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from . import VERSION, bundles
from .backend import resource_root
from .jobs import Job
from .models import Profile, SettingsStore, import_legacy, validate_id
from .credentials import CredentialStore, CredentialError
from .key_dialog import KeySetupDialog
from .backup_dialog import BackupDialog
from . import scopes
from .task_panel import TaskPanel
from .terminal import clean_log_text
from .widgets import ComboItemDelegate, ProfileComboBox, TaskSplitter, ValueComboBox, ValueSpinBox

ACTIONS = {'connect': '检查连接', 'install': '部署组件', 'plan': '环境检查', 'backup': '创建备份',
           'catalog': '刷新版本', 'verify': '完整性校验', 'fetch': '取回备份', 'upload': '上传备份',
           'unpack': '解包备份', 'restore': '恢复预览', 'remove_export': '清理导出包', 'setup_key': '设置 SSH 密钥'}


def action_title(action, arguments=None):
    if action == 'restore' and (arguments or {}).get('area') == 'packages':
        return '按清单安装软件' if arguments.get('apply') else '软件安装预览'
    if action == 'restore' and (arguments or {}).get('apply'):
        return '执行恢复'
    return ACTIONS[action]

STYLE = """
* { font-family: 'Noto Sans CJK SC', 'Microsoft YaHei UI', 'Segoe UI', sans-serif; font-size: 13px; }
QMainWindow, QWidget#workspace { background: #f4f6fa; color: #15243b; }
QWidget#sidebar { background: #14263e; color: #eff5ff; }
QLabel { color: #24344c; background: transparent; }
QWidget#sidebar QLabel { color: #eef4ff; }
QLabel#brand { font-size: 22px; font-weight: 700; color: white; }
QLabel#muted { color: #708098; }
QLabel#title { font-size: 27px; font-weight: 700; color: #17273f; }
QLabel#section { font-size: 16px; font-weight: 600; }
QLabel#number { font-size: 28px; font-weight: 700; color: #17273f; }
QLabel#badge { color: #167a71; background: #e4f4ef; border-radius: 11px; padding: 5px 12px; }
QFrame#card { background: white; border: 1px solid #e2e7ef; border-radius: 12px; }
QFrame#hero { background: #eaf0fc; border: 1px solid #d9e4f7; border-radius: 13px; }
QListWidget#navigation { background: transparent; border: 0; color: #bdcde0; outline: none; }
QListWidget#navigation::item { height: 44px; padding-left: 16px; border-radius: 7px; margin: 3px 0; }
QListWidget#navigation::item:selected { background: #2a4262; color: white; }
QListWidget#navigation::item:hover { background: #203753; }
QPushButton { background: white; color: #314560; border: 1px solid #d8e0eb; padding: 9px 15px; border-radius: 7px; }
QPushButton:hover { background: #edf2fa; border-color: #b4c7e0; }
QPushButton:disabled { background: #f0f2f6; color: #9ba8ba; border-color: #e5e9f0; }
QPushButton#primary { background: #315fe5; color: white; border-color: #315fe5; font-weight: 600; }
QPushButton#primary:hover { background: #254fcb; }
QPushButton#primary:disabled { background: #c8d3ed; border-color: #c8d3ed; }
QPushButton#danger { color: #b34444; }
QLineEdit, QComboBox, QSpinBox { background: white; color: #24344c; border: 1px solid #d8e0eb; border-radius: 6px; padding: 7px 9px; min-height: 20px; selection-background-color: #315fe5; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: #5780ee; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { background: #f0f2f6; color: #8997ac; border-color: #e2e7ef; }
QComboBox { padding-right: 38px; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: top right; width: 31px; background: #f8fafc; border-left: 1px solid #d8e0eb; border-top-right-radius: 5px; border-bottom-right-radius: 5px; }
QComboBox::drop-down:hover { background: #edf2fa; }
QComboBox::down-arrow { image: url("__ASSETS__/chevron-down.svg"); width: 14px; height: 14px; }
QComboBox QLineEdit { background: transparent; color: #24344c; border: 0; padding: 0; min-height: 0; }
QComboBox#profileSelector { background: #213852; color: #f8fafc; border-color: #38506d; padding-left: 8px; padding-right: 4px; }
QComboBox#profileSelector:disabled { color: #bac9dc; }
QComboBox#profileSelector::drop-down { width: 23px; background: #2a4262; border-left-color: #38506d; }
QComboBox#profileSelector::drop-down:hover { background: #355172; }
QComboBox#profileSelector::down-arrow { image: url("__ASSETS__/chevron-down-light.svg"); }
QSpinBox { padding-right: 38px; }
QSpinBox QLineEdit { background: transparent; color: #24344c; border: 0; border-radius: 0; padding: 0; min-height: 0; }
QSpinBox::up-button { subcontrol-origin: border; subcontrol-position: top right; width: 30px; background: #f8fafc; border: 1px solid #b9c7dc; border-top-right-radius: 5px; }
QSpinBox::down-button { subcontrol-origin: border; subcontrol-position: bottom right; width: 30px; background: #f8fafc; border: 1px solid #b9c7dc; border-top: 0; border-bottom-right-radius: 5px; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background: #edf2fa; }
QSpinBox::up-button:pressed, QSpinBox::down-button:pressed { background: #dfe9fc; }
QSpinBox::up-button:disabled, QSpinBox::down-button:disabled { background: #f0f2f6; }
QSpinBox::up-arrow { image: url("__ASSETS__/chevron-up.svg"); width: 12px; height: 12px; }
QSpinBox::down-arrow { image: url("__ASSETS__/chevron-down.svg"); width: 12px; height: 12px; }
QTableWidget { background: white; border: 1px solid #e2e7ef; border-radius: 9px; gridline-color: #edf0f5; selection-background-color: #e9effd; selection-color: #1b3564; }
QHeaderView::section { background: #f8fafc; color: #65758d; border: 0; border-bottom: 1px solid #e2e7ef; padding: 12px; font-weight: 500; }
QTableWidget::item { padding: 8px; border: 0; }
QPlainTextEdit, QTextBrowser { background: white; color: #314560; border: 1px solid #e2e7ef; border-radius: 8px; padding: 12px; }
QProgressBar { background: #e5ebf4; border: 0; border-radius: 4px; height: 8px; text-align: center; }
QProgressBar::chunk { background: #4676e9; border-radius: 4px; }
QProgressBar[taskState="失败"]::chunk { background: #c65a58; }
QProgressBar[taskState="已取消"]::chunk { background: #96a3b7; }
QFrame#taskPanel { background: white; border: 1px solid #d8e0eb; border-radius: 9px; }
QFrame#taskPanel QPushButton { padding: 5px 10px; }
QLabel#taskTitle { color: #24344c; font-weight: 600; }
QLabel#taskOutcome { color: #315fe5; background: #edf2ff; border-radius: 5px; padding: 4px 8px; }
QLabel#taskOutcome[taskState="已完成"] { color: #167a71; background: #e4f4ef; }
QLabel#taskOutcome[taskState="失败"] { color: #ad4644; background: #fff0ee; }
QLabel#taskOutcome[taskState="已取消"] { color: #65758d; background: #edf0f5; }
QPlainTextEdit#taskConsole { font-family: 'Cascadia Mono', 'Consolas', 'DejaVu Sans Mono', monospace; font-size: 12px; padding: 7px; border-radius: 5px; }
QSplitter::handle:vertical { background: #e2e7ef; height: 5px; margin: 1px 0; }
QSplitter#taskSplitter::handle:vertical { background: transparent; height: 10px; margin: 0; }
QScrollArea { border: 0; background: transparent; }
QStatusBar { background: #eef2f7; color: #63758e; border-top: 1px solid #e0e6ee; }
QToolTip { background: #14263e; color: white; border: 0; padding: 6px; }
""".replace('__ASSETS__', (resource_root() / 'assets').as_posix())


def apply_theme(app):
    app.setStyle('Fusion')
    # Windows may provide a dark system palette even though our workspace is
    # light. Set all text/base roles explicitly, including inactive windows.
    palette = QPalette()
    colors = {
        QPalette.ColorRole.Window: '#f4f6fa', QPalette.ColorRole.WindowText: '#24344c',
        QPalette.ColorRole.Base: '#ffffff', QPalette.ColorRole.AlternateBase: '#f8fafc',
        QPalette.ColorRole.Text: '#24344c', QPalette.ColorRole.Button: '#ffffff',
        QPalette.ColorRole.ButtonText: '#314560', QPalette.ColorRole.Highlight: '#315fe5',
        QPalette.ColorRole.HighlightedText: '#ffffff', QPalette.ColorRole.PlaceholderText: '#708098',
        QPalette.ColorRole.ToolTipBase: '#14263e', QPalette.ColorRole.ToolTipText: '#ffffff',
    }
    for role, color in colors.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor('#8997ac'))
    app.setPalette(palette)
    app.setStyleSheet(STYLE)


def combo_box(profile=False):
    widget = ProfileComboBox() if profile else ValueComboBox()
    view = QListView(widget)
    view.setStyleSheet('''
        QListView { background: white; color: #24344c; border: 1px solid #d8e0eb; padding: 4px; outline: 0; }
        QListView::item { padding: 4px 8px; color: #24344c; }
        QListView::item:selected { background: #e8effc; color: #254fcb; }
        QListView::item:hover { background: #f0f4fc; }
        QListView::item:disabled { color: #8997ac; }
    ''')
    widget.setView(view)
    view.setUniformItemSizes(True)
    view.setSpacing(2)
    widget.setItemDelegate(ComboItemDelegate(view))
    widget.setMaxVisibleItems(12)
    return widget


def label(text, name='', wrap=False):
    widget = QLabel(text)
    widget.setObjectName(name)
    widget.setWordWrap(wrap)
    widget.setMinimumHeight(38 if name == 'number' else 34 if name == 'title' else 20)
    return widget


def card(name='card'):
    widget = QFrame()
    widget.setObjectName(name)
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(22, 20, 22, 20)
    layout.setSpacing(12)
    return widget, layout


def size_text(value):
    if value is None:
        return '共享文件'
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:.1f} {unit}'
        value /= 1024


class MainWindow(QMainWindow):
    def __init__(self, data_directory):
        super().__init__()
        self.setWindowTitle('回溯 RestorePoint · 系统备份')
        self.setWindowIcon(QIcon(str(resource_root() / 'assets/icon.svg')))
        self.resize(1280, 860)
        self.setMinimumSize(1020, 720)
        self.store = SettingsStore(data_directory)
        self.credential_store = CredentialStore(data_directory)
        self.session_credentials = {}
        self.credential_scope = None
        self.loading_profile = False
        self.profiles = self.store.load()
        self.job = None
        self.rows = []
        self.preview_signature = None
        self.buttons = []
        self.history = []
        self.history_path = self.store.directory / 'history.json'
        if self.history_path.exists():
            try:
                self.history = json.loads(self.history_path.read_text(encoding='utf-8'))
            except (ValueError, OSError):
                pass
        self.pending_close = False
        self.log_path = None
        root = QWidget()
        root.setObjectName('workspace')
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.setCentralWidget(root)
        sidebar = QWidget()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(222)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(19, 30, 19, 22)
        side.setSpacing(15)
        side.addWidget(label('◈  回溯', 'brand'))
        side.addWidget(label('RESTOREPOINT', 'muted'))
        side.addSpacing(20)
        side.addWidget(label('当前备份配置'))
        self.profile_combo = combo_box(profile=True)
        self.profile_combo.setObjectName('profileSelector')
        self.profile_combo.setPlaceholderText('暂无配置')
        side.addWidget(self.profile_combo)
        self.navigation = QListWidget()
        self.navigation.setObjectName('navigation')
        for title, page_index in [('概览', 0), ('1  连接与配置', 3), ('2  备份版本', 1), ('3  恢复数据', 2), ('使用帮助', 4)]:
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, page_index)
            self.navigation.addItem(item)
        side.addWidget(self.navigation, 1)
        side.addWidget(label('Linux  /  Windows'))
        side.addWidget(label('桌面客户端  ·  v' + VERSION, 'muted'))
        outer.addWidget(sidebar)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(32, 25, 32, 18)
        body.setSpacing(18)
        heading = QHBoxLayout()
        heading.addWidget(label('系统备份工作台', 'muted'))
        heading.addStretch()
        self.badge = label('', 'badge')
        heading.addWidget(self.badge)
        body.addLayout(heading)
        self.pages = QStackedWidget()
        self.pages.setMinimumHeight(280)
        self.task_splitter = TaskSplitter(Qt.Orientation.Vertical)
        self.task_splitter.setObjectName('taskSplitter')
        self.task_splitter.setChildrenCollapsible(False)
        self.task_splitter.setHandleWidth(10)
        self.task_splitter.addWidget(self.pages)
        body.addWidget(self.task_splitter, 1)
        outer.addWidget(content, 1)
        self.build_overview()
        self.build_versions()
        self.build_restore()
        self.build_settings()
        self.build_history()
        self.build_help()
        self.task_panel = TaskPanel()
        self.task_splitter.addWidget(self.task_panel)
        self.task_splitter.setStretchFactor(0, 1)
        self.task_splitter.setStretchFactor(1, 0)
        self.console = self.task_panel.console
        self.task_status = self.task_panel.phase
        self.progress = self.task_panel.progress
        self.cancel_button = self.task_panel.cancel_button
        self.task_panel.cancel_requested.connect(self.cancel_task)
        self.task_panel.history_requested.connect(self.show_history)
        self.task_panel.export_requested.connect(self.export_log)
        self.task_panel.open_logs_requested.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.store.directory / 'logs'))))
        self.task_panel.expanded_changed.connect(lambda expanded: QTimer.singleShot(0, lambda: self.resize_task_panel(expanded)))
        self.statusBar().showMessage('本机系统备份支持 Linux；两端客户端均可管理远程 Linux 测试机。')
        self.navigation.currentRowChanged.connect(lambda row: self.pages.setCurrentIndex(self.navigation.item(row).data(Qt.ItemDataRole.UserRole)) if row >= 0 else None)
        self.navigation.setCurrentRow(0)
        self.profile_combo.currentIndexChanged.connect(self.select_profile)
        self.reload_profiles(0)
        self.render_history()
        self.restore_last_task()

    def resize_task_panel(self, expanded):
        total = sum(self.task_splitter.sizes())
        height = 240 if expanded else 84
        self.task_splitter.setSizes([max(280, total - height), height])

    def show_page(self, index):
        for row in range(self.navigation.count()):
            if self.navigation.item(row).data(Qt.ItemDataRole.UserRole) == index:
                self.navigation.setCurrentRow(row)
                break

    def button(self, text, callback, primary=False, requires_profile=True):
        widget = QPushButton(text)
        if primary:
            widget.setObjectName('primary')
        widget.clicked.connect(callback)
        widget.setProperty('requiresProfile', requires_profile)
        self.buttons.append(widget)
        return widget

    def page(self, title, subtitle):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)
        layout.addWidget(label(title, 'title'))
        layout.addWidget(label(subtitle, 'muted', True))
        self.pages.addWidget(page)
        return layout

    def build_overview(self):
        outer = self.page('为下一次修改，留一个恢复点', '保存系统配置、已安装软件与用户数据，按版本恢复。')
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName('workspace')
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 5, 0)
        layout.setSpacing(18)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        guide, guide_box = card()
        guide_box.addWidget(label('按这三步开始', 'section'))
        steps = QHBoxLayout()
        for title, index in [('1  连接与配置', 3), ('2  创建和管理备份', 1), ('3  预览并恢复数据', 2)]:
            steps.addWidget(self.button(title, lambda checked=False, page=index: self.show_page(page), requires_profile=False))
        guide_box.addLayout(steps)
        guide_box.addWidget(label('先保存连接并检查环境，再创建备份；需要还原时，先预览再执行。', 'muted', True))
        layout.addWidget(guide)
        stats = QHBoxLayout()
        self.stat_values = []
        for title, hint in [('备份版本', '当前仓库中的完整版本'), ('可用空间', '备份所在磁盘的可用空间'), ('最近任务', '当前配置的任务状态')]:
            widget, box = card()
            box.setContentsMargins(20, 16, 20, 16)
            box.setSpacing(6)
            box.addWidget(label(title, 'muted'))
            value = label('—', 'number')
            value.setMinimumWidth(140)
            box.addWidget(value)
            box.addWidget(label(hint, 'muted'))
            self.stat_values.append(value)
            stats.addWidget(widget)
        layout.addLayout(stats)
        hero, hero_box = card('hero')
        self.hero_title = label('准备你的第一个恢复点', 'section')
        hero_box.addWidget(self.hero_title)
        self.hero_description = label('', '', True)
        hero_box.addWidget(self.hero_description)
        actions = QHBoxLayout()
        actions.addWidget(self.button('创建备份', self.create_backup, True))
        actions.addWidget(self.button('环境检查', lambda: self.start('plan')))
        actions.addStretch()
        hero_box.addLayout(actions)
        layout.addWidget(hero)
        widget, box = card()
        widget.setMinimumHeight(204)
        box.addWidget(label('备份范围', 'section'))
        grid = QGridLayout()
        for i, (title, text) in enumerate([
            ('系统配置', '/etc · 服务与软件源配置'), ('软件与系统数据', '/usr · /opt · /var/lib · /boot'),
            ('用户文件', '/home · 文档、代码和隐藏配置'), ('完整性信息', '软件版本清单 · SHA-256 校验'),
        ]):
            grid.addWidget(label('✓  ' + title), i // 2 * 2, i % 2)
            grid.addWidget(label(text, 'muted'), i // 2 * 2 + 1, i % 2)
        grid.setVerticalSpacing(12)
        grid.setHorizontalSpacing(30)
        box.addLayout(grid)
        box.addWidget(label('默认排除临时文件、缓存、日志、交换文件和备份仓库自身。独立 /home、/boot 等挂载布局会提示停止。', 'muted', True))
        layout.addWidget(widget)
        self.next_step = label('先在「连接与配置」中配置位置，再检查环境。', 'muted', True)
        layout.addWidget(self.next_step)
        layout.addStretch()

    def build_versions(self):
        layout = self.page('备份版本', '选择一个已完成的版本，检查完整性或进入恢复。未完成的下载不会列为恢复点。')
        toolbar = QHBoxLayout()
        toolbar.addWidget(self.button('创建备份', self.create_backup, True))
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索版本、主机或时间…')
        self.search.textChanged.connect(self.render_rows)
        toolbar.addWidget(self.search, 1)
        toolbar.addWidget(self.button('刷新版本', lambda: self.start('catalog')))
        self.fetch_button = self.button('取回指定版本', self.fetch_dialog)
        toolbar.addWidget(self.fetch_button)
        layout.addLayout(toolbar)
        self.table = QTableWidget(0, 5)
        self.table.setObjectName('versionsTable')
        self.table.setHorizontalHeaderLabels(['版本 / 创建时间', '来源主机', '类型', '大小', '状态'])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3, 4):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.cellDoubleClicked.connect(lambda *_: self.show_details())
        layout.addWidget(self.table, 1)
        self.empty_label = label('还没有读取到已完成的备份。配置仓库后点击「刷新版本」。', 'muted')
        layout.addWidget(self.empty_label)
        buttons = QHBoxLayout()
        buttons.addWidget(self.button('校验选中版本', lambda: self.start('verify', snapshot=self.selected_id())))
        buttons.addWidget(self.button('查看详情', self.show_details))
        buttons.addWidget(self.button('恢复选中版本', self.go_restore, True))
        buttons.addStretch()
        self.cleanup_button = self.button('清理测试机导出包', self.cleanup_export)
        buttons.addWidget(self.cleanup_button)
        layout.addLayout(buttons)

    def build_restore(self):
        layout = self.page('恢复数据', '先预览将要恢复的内容，再执行恢复。系统恢复需要测试机处于 Live ISO 或救援环境。')
        self.restore_scroll = QScrollArea()
        self.restore_scroll.setWidgetResizable(True)
        self.restore_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName('workspace')
        self.restore_scroll.setWidget(content)
        layout.addWidget(self.restore_scroll, 1)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 5, 0)
        layout.setSpacing(16)
        widget, box = card()
        form = QFormLayout()
        form.setSpacing(16)
        self.restore_version = combo_box()
        self.restore_version.setObjectName('restoreVersion')
        self.restore_version.setEditable(True)
        self.restore_version.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.restore_version.setPlaceholderText('选择或输入完整版本名')
        self.restore_version.currentTextChanged.connect(self.invalidate_preview)
        self.restore_version.currentTextChanged.connect(self.update_restore_areas)
        form.addRow('备份版本', self.restore_version)
        self.restore_area = combo_box()
        self.restore_area.addItem('用户目录 / 文件', 'home')
        self.restore_area.addItem('系统与已安装软件', 'system')
        self.restore_area.currentIndexChanged.connect(self.area_changed)
        form.addRow('恢复范围', self.restore_area)
        self.restore_target = QLineEdit('/home')
        self.restore_target.setObjectName('restoreTarget')
        self.restore_target.textChanged.connect(self.invalidate_preview)
        form.addRow('测试机目标目录', self.restore_target)
        self.restore_relative = QLineEdit()
        self.restore_relative.setPlaceholderText('例如 wyh/文档；留空恢复整个用户目录范围')
        self.restore_relative.textChanged.connect(self.invalidate_preview)
        self.relative_label = label('选定用户路径')
        form.addRow(self.relative_label, self.restore_relative)
        box.addLayout(form)
        self.remote_steps = QWidget()
        step_box = QHBoxLayout(self.remote_steps)
        step_box.setContentsMargins(0, 8, 0, 0)
        step_box.addWidget(self.button('1. 上传到测试机', lambda: self.restore_action('upload')))
        step_box.addWidget(self.button('2. 解包并校验', lambda: self.restore_action('unpack')))
        step_box.addStretch()
        box.addWidget(self.remote_steps)
        box.addWidget(label('用户恢复会覆盖选中范围内的同名文件。系统恢复会同步旧版本并清理后来新增的系统文件，用户目录单独处理。', 'muted', True))
        layout.addWidget(widget)
        actions = QHBoxLayout()
        actions.addWidget(self.button('预览恢复内容', lambda: self.restore_action('restore'), True))
        self.apply_button = self.button('执行恢复', self.apply_restore)
        self.apply_button.setObjectName('danger')
        self.apply_button.setEnabled(False)
        actions.addWidget(self.apply_button)
        actions.addStretch()
        layout.addLayout(actions)
        self.restore_notice = label('远程模式：上传 → 解包 → 预览 → 恢复。每一步的结果会记录在任务日志。', 'muted', True)
        layout.addWidget(self.restore_notice)
        layout.addStretch()

    def path_field(self, placeholder='', directory=True):
        row = QWidget()
        box = QHBoxLayout(row)
        box.setContentsMargins(0, 0, 0, 0)
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        button = QPushButton('选择…')
        def browse():
            path = QFileDialog.getExistingDirectory(self, '选择目录', edit.text()) if directory else QFileDialog.getOpenFileName(self, '选择文件', edit.text())[0]
            if path:
                edit.setText(path)
        button.clicked.connect(browse)
        box.addWidget(edit, 1)
        box.addWidget(button)
        return row, edit

    def build_settings(self):
        layout = self.page('连接与配置', '先配置连接与保存位置，再检查连接和部署组件。凭据可选保存在本机系统凭据库。')
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        tools = QHBoxLayout()
        tools.addWidget(self.button('新增配置', self.add_profile, requires_profile=False))
        tools.addWidget(self.button('导入配置', self.import_profile, requires_profile=False))
        tools.addWidget(self.button('导出配置', self.export_profile))
        tools.addStretch()
        tools.addWidget(self.button('删除当前配置', self.delete_profile))
        layout.addLayout(tools)
        self.settings_empty = label('暂无连接配置。点击「新增配置」填写测试机信息，或导入已有配置。', 'muted', True)
        layout.addWidget(self.settings_empty)
        self.settings_empty_space = QSpacerItem(0, 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding)
        layout.addSpacerItem(self.settings_empty_space)
        scroll = QScrollArea()
        self.settings_scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName('workspace')
        self.settings_content = content
        fields_box = QVBoxLayout(content)
        fields_box.setContentsMargins(0, 0, 6, 0)
        self.fields = {}
        widget, box = card()
        form = QFormLayout()
        self.fields['name'] = QLineEdit()
        form.addRow('配置名称', self.fields['name'])
        self.mode = combo_box()
        self.mode.addItem('本机 Linux · rsync 快照', 'local')
        self.mode.addItem('远程 Linux · 完整备份包', 'remote')
        if sys.platform != 'linux':
            self.mode.model().item(0).setEnabled(False)
        self.mode.currentIndexChanged.connect(self.mode_changed)
        form.addRow('备份模式', self.mode)
        self.fields['repository'] = QLineEdit()
        self.repository_label = label('本机快照仓库')
        form.addRow(self.repository_label, self.fields['repository'])
        row, self.fields['storage'] = self.path_field('选择本机保存远程备份包的目录')
        self.storage_row = row
        self.storage_label = label('备份包保存目录')
        form.addRow(self.storage_label, row)
        self.reserve = ValueSpinBox()
        self.reserve.lineEdit().setTextMargins(8, 0, 0, 0)
        self.reserve.setRange(8, 65536)
        self.reserve.setSuffix(' GiB')
        self.reserve.valueChanged.connect(self.configuration_edited)
        form.addRow('最低预留空间', self.reserve)
        box.addLayout(form)
        fields_box.addWidget(widget)
        self.connection_card, box = card()
        box.addWidget(label('SSH 连接', 'section'))
        form = QFormLayout()
        for key, title, hint in [('host', '地址 / SSH 别名', '例如 192.168.1.20 或 test-machine'), ('username', 'SSH 用户名', '留空时读取 SSH 配置')]:
            edit = QLineEdit()
            edit.setPlaceholderText(hint)
            self.fields[key] = edit
            form.addRow(title, edit)
        self.port = ValueSpinBox()
        self.port.lineEdit().setTextMargins(8, 0, 0, 0)
        self.port.setRange(1, 65535)
        form.addRow('SSH 端口', self.port)
        self.auth_method = combo_box()
        for title, value in [('自动 · SSH 配置 / agent / 密钥 / 密码', 'auto'), ('密码认证', 'password'), ('私钥文件认证', 'key')]:
            self.auth_method.addItem(title, value)
        form.addRow('认证方式', self.auth_method)
        row, self.fields['identity_file'] = self.path_field('可选；留空使用 SSH agent、默认密钥或密码', False)
        self.identity_row = row
        form.addRow('私钥文件', row)
        self.secret_fields = {}
        for kind, title, hint in [('login', 'SSH 登录密码', '可留空，连接时输入'),
                                  ('key', '私钥解锁口令', '加密私钥使用；无口令私钥留空'),
                                  ('sudo', 'sudo 密码', '可留空，执行管理任务时输入')]:
            edit = QLineEdit()
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            edit.setPlaceholderText(hint)
            edit.textChanged.connect(self.configuration_edited)
            self.secret_fields[kind] = edit
            form.addRow(title, edit)
        self.sudo_uses_login = QCheckBox('sudo 使用 SSH 登录密码')
        self.sudo_uses_login.toggled.connect(self.configuration_edited)
        form.addRow('', self.sudo_uses_login)
        self.remember_secrets = QCheckBox('在本机安全保存凭据')
        self.remember_secrets.toggled.connect(self.configuration_edited)
        form.addRow('', self.remember_secrets)
        self.credential_note = label('未勾选时仅在本次会话使用。勾选后点击「保存配置」；配置导出不包含密码。', 'muted', True)
        form.addRow('', self.credential_note)
        self.auth_method.currentIndexChanged.connect(self.configuration_edited)
        self.auth_method.currentIndexChanged.connect(self.auth_method_changed)
        for key in ('host', 'username', 'identity_file'):
            self.fields[key].textChanged.connect(self.connection_identity_edited)
        self.port.valueChanged.connect(self.connection_identity_edited)
        box.addLayout(form)
        actions = QHBoxLayout()
        actions.addWidget(self.button('检查 SSH 连接', lambda: self.start('connect')))
        self.setup_key_button = self.button('一键设置 SSH 密钥', self.setup_ssh_key)
        actions.addWidget(self.setup_key_button)
        actions.addWidget(self.button('部署 / 更新测试机组件', lambda: self.start('install')))
        actions.addStretch()
        box.addLayout(actions)
        fields_box.addWidget(self.connection_card)
        self.paths_card, box = card()
        box.addWidget(label('测试机路径', 'section'))
        form = QFormLayout()
        for key, title, hint in [('remote_helper', '远程组件', '/usr/local/lib/test-backup/portable.py'),
                                 ('export_root', '备份导出目录', '/var/backups/portable-snapshots'),
                                 ('inbox', '恢复包上传目录', '留空时为 SSH 用户创建独立私有目录'),
                                 ('staging', '恢复解包目录', '/var/tmp/test-backup-restore')]:
            edit = QLineEdit()
            edit.setPlaceholderText(hint)
            self.fields[key] = edit
            form.addRow(title, edit)
        box.addLayout(form)
        box.addWidget(label('在救援环境中恢复系统时，解包目录应放在容量充足的磁盘上。', 'muted', True))
        fields_box.addWidget(self.paths_card)
        fields_box.addStretch()
        for edit in self.fields.values():
            edit.textChanged.connect(self.configuration_edited)
        self.port.valueChanged.connect(self.configuration_edited)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        save = QHBoxLayout()
        save.addWidget(self.button('保存配置', self.save_profile, True))
        save.addStretch()
        self.saved_label = label('', 'muted')
        save.addWidget(self.saved_label)
        layout.addLayout(save)

    def build_history(self):
        self.history_dialog = QDialog(self)
        self.history_dialog.setWindowTitle('历史任务')
        self.history_dialog.resize(960, 620)
        layout = QVBoxLayout(self.history_dialog)
        self.history_caption = label('选择历史任务查看日志。当前任务在主窗口底部持续更新。', 'muted')
        layout.addWidget(self.history_caption)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.history_table = QTableWidget(0, 4)
        self.history_table.setHorizontalHeaderLabels(['时间', '配置', '任务', '结果'])
        self.history_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.history_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.history_table.verticalHeader().hide()
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.history_table.cellClicked.connect(self.open_history)
        splitter.addWidget(self.history_table)
        self.history_console = QPlainTextEdit()
        self.history_console.setReadOnly(True)
        splitter.addWidget(self.history_console)
        splitter.setSizes([190, 360])
        layout.addWidget(splitter, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        export = buttons.addButton('导出选中日志', QDialogButtonBox.ButtonRole.ActionRole)
        export.clicked.connect(self.export_history_log)
        buttons.rejected.connect(self.history_dialog.close)
        layout.addWidget(buttons)

    def build_help(self):
        layout = self.page('使用帮助', '从配置到恢复的完整操作路径。')
        browser = QTextBrowser()
        browser.setMarkdown('''# 回溯 RestorePoint

## 本机 Linux
1. 在「连接与配置」选择本机模式，设置快照仓库。
2. 运行环境检查，再创建备份。需要权限时在弹窗中输入 sudo 密码。
3. 在「备份版本」刷新并选择版本，可校验或进入恢复。

## Windows / Linux 管理远程测试机
1. 填写 SSH 地址、用户名和本机备份包目录，保存配置。
2. 检查 SSH 连接，首次连接核对主机指纹，再部署测试机组件。
3. 运行环境检查，创建备份。备份包会下载到本机，校验后才成为完整版本。
4. 下载中断时，可用「取回指定版本」重试。只有手动清理导出包时才删除测试机暂存数据。

## 恢复文件
远程版本先上传、解包，再预览。相对路径包含用户名，例如 `wyh/文档`。
用户恢复覆盖同名文件，但保留目标中额外存在的文件。预览成功后才可点击「执行恢复」。

## 恢复系统
先将测试机启动至 Live ISO / 救援系统，启用 SSH，核对主机密钥及系统根分区。
把系统分区挂载到 `/mnt/target`，重新部署组件，并使用救援环境专用配置。
解包区需要足够空间，例如 `/mnt/target/var/tmp/test-backup-restore`。
系统恢复会清理备份后新增的系统文件；用户目录需独立恢复。完成后按需检查引导配置。

## 范围与限制
- Windows 客户端管理 Linux 来源；本机模式不备份 Windows 操作系统。
- 当前来源要求根目录、home、boot 等在同一文件系统。额外挂载点会停止并提示。
- 这是文件级备份，分区表、引导扇区、进程和内存状态需要另行处理。
- 备份前退出重要应用，暂停数据库或先做应用自身的导出。
- 同盘本地备份无法应对该磁盘损坏。远程版本为独立完整包，会使用更多存储空间。
- 可选在本机系统凭据库保存远程 SSH 密码、私钥口令及 sudo 密码；配置导出与日志不含密码。
''')
        layout.addWidget(browser, 1)

    def reload_profiles(self, selected):
        selected = min(max(selected, 0), len(self.profiles) - 1)
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for profile in self.profiles:
            self.profile_combo.addItem(profile.name, profile.id)
        self.profile_combo.setCurrentIndex(selected)
        self.profile_combo.blockSignals(False)
        self.select_profile(selected)

    def select_profile(self, index):
        if self.credential_scope is not None:
            self.session_credentials[self.credential_scope] = self.credential_values()
        self.loading_profile = True
        present = 0 <= index < len(self.profiles)
        self.settings_scroll.setVisible(present)
        self.settings_empty.setVisible(not present)
        self.settings_empty_space.changeSize(0, 0, QSizePolicy.Policy.Minimum,
                                             QSizePolicy.Policy.Fixed if present else QSizePolicy.Policy.Expanding)
        self.settings_scroll.parentWidget().layout().invalidate()
        if not present:
            self.profile_combo.setToolTip('')
            for edit in self.fields.values():
                edit.clear()
            self.mode.setCurrentIndex(-1)
            self.credential_scope = None
            for edit in self.secret_fields.values():
                edit.clear()
            self.remember_secrets.setChecked(False)
            self.sudo_uses_login.setChecked(False)
            self.rows = []
            self.restore_target.clear()
            self.restore_relative.clear()
            self.invalidate_preview()
            self.render_rows()
            self.badge.setText('暂无连接配置')
            self.hero_description.setText('在「连接与配置」中新增测试机配置，或导入已有配置。')
            self.restore_notice.setText('请先新增或导入一个备份配置。')
            self.saved_label.setText('暂无配置 · 可新增或导入')
            self.loading_profile = False
            self.set_busy(False)
            return
        profile = self.profiles[index]
        self.profile_combo.setToolTip(profile.name)
        for key, edit in self.fields.items():
            edit.setText(str(getattr(profile, key)))
        self.mode.setCurrentIndex(0 if profile.mode == 'local' else 1)
        self.port.setValue(profile.port)
        self.reserve.setValue(profile.reserve_gib)
        self.auth_method.setCurrentIndex(self.auth_method.findData(profile.auth_method))
        self.remember_secrets.setChecked(profile.remember_secrets)
        self.sudo_uses_login.setChecked(profile.sudo_uses_login)
        self.credential_scope = self.credential_store.scope(profile)
        values = self.session_credentials.get(self.credential_scope)
        self.credential_note.setText('未勾选时仅在本次会话使用。勾选后点击「保存配置」；配置导出不包含密码。')
        if values is None:
            values = {}
            if profile.remember_secrets:
                try:
                    values = self.credential_store.load(profile)
                    self.credential_note.setText('已从本机系统凭据库读取；取消勾选并保存可删除已存凭据。')
                except CredentialError as exc:
                    self.credential_note.setText(str(exc))
        for kind, edit in self.secret_fields.items():
            edit.setText(values.get(kind, ''))
        self.rows = []
        if profile.mode == 'remote':
            try:
                self.rows = bundles.catalog(profile.storage)
            except (OSError, ValueError):
                pass
        self.mode_changed()
        self.render_rows()
        self.saved_label.setText('配置更改后请保存')
        if not self.restore_target.text():
            self.area_changed()
        self.loading_profile = False
        self.set_busy(False)

    def has_profile(self):
        return 0 <= self.profile_combo.currentIndex() < len(self.profiles)

    def current_profile(self):
        if not self.has_profile():
            raise ValueError('请先新增或导入一个备份配置。')
        original = self.profiles[self.profile_combo.currentIndex()]
        data = asdict(original)
        data.update({key: edit.text().strip() for key, edit in self.fields.items()})
        data.update(mode=self.mode.currentData(), port=self.port.value(), reserve_gib=self.reserve.value(),
                    auth_method=self.auth_method.currentData(), remember_secrets=self.remember_secrets.isChecked(),
                    sudo_uses_login=self.sudo_uses_login.isChecked())
        return Profile.from_dict(data)

    def mode_changed(self, *_):
        remote = self.mode.currentData() == 'remote'
        self.connection_card.setVisible(remote)
        self.paths_card.setVisible(remote)
        self.repository_label.setVisible(not remote)
        self.fields['repository'].setVisible(not remote)
        self.storage_label.setVisible(remote)
        self.storage_row.setVisible(remote)
        self.remote_steps.setVisible(remote)
        self.fetch_button.setVisible(remote)
        self.cleanup_button.setVisible(remote)
        self.badge.setText('SSH 远程 · 按范围备份' if remote else 'Linux 本地 · 版本快照')
        self.hero_description.setText('测试机保留 Linux 元数据，备份包传回当前电脑并完成校验。Linux 与 Windows 使用相同的操作界面。' if remote else '在本机保留系统与用户数据的历史版本。未变化的文件共享空间，适合升级或修改配置前创建恢复点。')
        self.restore_notice.setText('远程模式：上传 → 解包 → 预览 → 恢复。当前配置的测试机路径应与运行环境一致。' if remote else '本机模式直接读取快照。系统恢复需要从救援环境启动，挂载目标分区后再执行。')
        self.invalidate_preview()
        self.update_stats()

    def configuration_edited(self, *_):
        if self.loading_profile:
            return
        self.invalidate_preview()
        if hasattr(self, 'saved_label'):
            self.saved_label.setText('有未保存的配置更改')

    def credential_values(self):
        return {kind: edit.text() for kind, edit in self.secret_fields.items() if edit.text()}

    def auth_method_changed(self, *_):
        method = self.auth_method.currentData()
        self.identity_row.setEnabled(method != 'password')
        self.secret_fields['key'].setEnabled(method != 'password')
        self.secret_fields['login'].setEnabled(method != 'key')

    def connection_identity_edited(self, *_):
        if self.loading_profile:
            return
        # Credentials for one endpoint must not follow an edited host/user/key.
        for edit in self.secret_fields.values():
            edit.clear()
        if self.has_profile():
            self.credential_scope = self.credential_store.scope(self.current_profile())

    def remember_credential(self, kind, value):
        profile = self.active_profile
        scope = self.credential_store.scope(profile)
        values = self.session_credentials.setdefault(scope, {})
        values[kind] = value
        if self.credential_scope == scope:
            self.loading_profile = True
            self.secret_fields[kind].setText(value)
            self.loading_profile = False
        saved = any(item.id == profile.id and item.remember_secrets and
                    self.credential_store.scope(item) == scope for item in self.profiles)
        if profile.remember_secrets and saved:
            try:
                self.credential_store.save(profile, values)
                self.credential_note.setText('凭据已保存在本机系统凭据库。')
            except CredentialError as exc:
                self.credential_note.setText(str(exc))
        elif profile.remember_secrets:
            self.credential_note.setText('当前凭据仅在本次会话中使用；点击「保存配置」后保存到系统凭据库。')

    def save_profile(self):
        try:
            profile = self.current_profile()
            profile.validate()
            index = self.profile_combo.currentIndex()
            original = self.profiles[index]
            values = self.credential_values()
            if profile.remember_secrets and values:
                self.credential_store.save(profile, values)
            elif original.remember_secrets and self.credential_store.scope(original) == self.credential_store.scope(profile):
                self.credential_store.delete(original)
            if original.remember_secrets and self.credential_store.scope(original) != self.credential_store.scope(profile):
                self.credential_store.delete(original)
            self.session_credentials[self.credential_store.scope(profile)] = values
            updated = list(self.profiles)
            updated[index] = profile
            self.store.save(updated)
            self.profiles = updated
            self.profile_combo.setItemText(index, profile.name)
            self.saved_label.setText('已保存 · 凭据在系统凭据库中' if profile.remember_secrets and values else '已保存 · 配置文件不含密码')
            self.update_stats()
            return True
        except Exception as exc:
            QMessageBox.warning(self, '无法保存配置', str(exc))
            return False

    def setup_ssh_key(self):
        try:
            profile = self.current_profile()
            if profile.mode != 'remote':
                raise ValueError('SSH 密钥设置用于远程 Linux 测试机。')
            replace(profile, auth_method='auto').validate(remote=True)
        except ValueError as exc:
            QMessageBox.information(self, '检查连接配置', str(exc))
            return
        dialog = KeySetupDialog(profile, self.store.directory, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            secret = dialog.passphrase.text()
            self.start('setup_key', transient_credentials={'setup_key': secret},
                       encrypt=bool(secret), remember=dialog.remember.isChecked())

    def add_profile(self):
        self.profiles.append(Profile(name='新测试机'))
        self.reload_profiles(len(self.profiles) - 1)
        self.show_page(3)

    def delete_profile(self):
        if not self.has_profile() or self.job:
            return
        if QMessageBox.question(self, '删除配置', '删除当前连接配置？备份数据不随配置删除。') == QMessageBox.StandardButton.Yes:
            index = self.profile_combo.currentIndex()
            profile = self.profiles[index]
            if profile.remember_secrets:
                try:
                    self.credential_store.delete(profile)
                except CredentialError as exc:
                    QMessageBox.warning(self, '无法删除凭据', str(exc))
                    return
            self.session_credentials.pop(self.credential_store.scope(profile), None)
            self.credential_scope = None
            del self.profiles[index]
            self.store.save(self.profiles)
            self.reload_profiles(index)

    def import_profile(self):
        path = QFileDialog.getOpenFileName(self, '导入 JSON 配置', '', 'JSON (*.json)')[0]
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
            profile = import_legacy(data) if 'SshTarget' in data else Profile.from_dict(data)
            profile.id = Profile().id
            profile.validate()
            self.profiles.append(profile)
            self.reload_profiles(len(self.profiles) - 1)
            self.show_page(3)
        except Exception as exc:
            QMessageBox.warning(self, '导入失败', str(exc))

    def export_profile(self):
        if not self.has_profile():
            return
        path = QFileDialog.getSaveFileName(self, '导出连接配置', 'restorepoint-profile.json', 'JSON (*.json)')[0]
        if path:
            try:
                Path(path).write_text(json.dumps(asdict(self.current_profile()), ensure_ascii=False, indent=2), encoding='utf-8')
            except OSError as exc:
                QMessageBox.warning(self, '导出失败', str(exc))

    def render_rows(self, *_):
        query = self.search.text().casefold()
        rows = [r for r in self.rows if query in (r['id'] + r.get('host', '') + r.get('date', '')).casefold()]
        self.table.setRowCount(len(rows))
        for row, data in enumerate(rows):
            for column, value in enumerate([data['id'] + '\n' + data.get('date', '')[:19].replace('T', ' '), data.get('host', ''), data['kind'], size_text(data.get('bytes')), '需检查' if data.get('error') else '完整版本']):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, data['id'])
                self.table.setItem(row, column, item)
            self.table.setRowHeight(row, 62)
        self.empty_label.setVisible(not rows)
        current = self.restore_version.currentText()
        self.restore_version.blockSignals(True)
        self.restore_version.clear()
        self.restore_version.addItems([r['id'] for r in self.rows if not r.get('error')])
        if current in [r['id'] for r in self.rows]:
            self.restore_version.setCurrentText(current)
        self.restore_version.blockSignals(False)
        self.update_restore_areas()
        self.update_stats()

    def create_backup(self):
        if not self.has_profile() or self.job:
            return
        dialog = BackupDialog(self.current_profile(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            areas, mode = list(dialog.selected()), dialog.mode()
            profile = self.profiles[self.profile_combo.currentIndex()]
            profile.backup_areas, profile.software_mode = areas, mode
            self.start('plan' if dialog.plan_only else 'backup', areas=areas, software_mode=mode)

    def update_restore_areas(self, *_):
        if not hasattr(self, 'restore_area') or not hasattr(self, 'restore_relative'):
            return
        selected = self.restore_area.currentData()
        row = next((item for item in self.rows if item['id'] == self.restore_version.currentText() and not item.get('error')), None)
        choices = scopes.restorable(row['metadata']) if row else [('用户目录 / 文件', 'home'), ('系统与已安装软件', 'system')]
        old = [(self.restore_area.itemText(i), self.restore_area.itemData(i)) for i in range(self.restore_area.count())]
        if old == choices:
            return
        self.restore_area.blockSignals(True)
        self.restore_area.clear()
        for title, area in choices:
            self.restore_area.addItem(title, area)
        index = self.restore_area.findData(selected)
        self.restore_area.setCurrentIndex(max(0, index))
        self.restore_area.blockSignals(False)
        if self.restore_area.currentData() != selected:
            self.area_changed()

    def update_stats(self):
        if not hasattr(self, 'fields'):
            return
        if not self.has_profile():
            for widget, text in zip(self.stat_values, ('0', '—', '尚无任务')):
                widget.setText(text)
            self.hero_title.setText('先添加一台测试机')
            self.next_step.setText('在「连接与配置」中新增或导入配置，再检查连接和备份环境。')
            return
        self.stat_values[0].setText(str(len([r for r in self.rows if not r.get('error')])))
        profile = self.current_profile()
        path = Path(profile.storage if profile.mode == 'remote' else profile.repository)
        try:
            while not path.exists() and path != path.parent:
                path = path.parent
            self.stat_values[1].setText(size_text(shutil.disk_usage(path).free))
        except OSError:
            self.stat_values[1].setText('需授权')
        recent = next((h for h in reversed(self.history) if h['profile_id'] == profile.id), None)
        self.stat_values[2].setText(recent['status'] if recent else '尚无任务')
        self.hero_title.setText('已有恢复点，继续保护当前状态' if self.rows else '准备你的第一个恢复点')
        self.next_step.setText('选择版本可查看清单、校验文件，或进入恢复流程。' if self.rows else '先配置连接和存储位置，检查环境，再创建第一个完整备份。')

    def selected_id(self):
        row = self.table.currentRow()
        return self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) if row >= 0 else ''

    def show_details(self):
        row = next((r for r in self.rows if r['id'] == self.selected_id()), None)
        if row:
            self.text_dialog('备份版本详情', json.dumps(row.get('metadata', row), ensure_ascii=False, indent=2))

    def text_dialog(self, title, text):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(850, 560)
        layout = QVBoxLayout(dialog)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(text[-150000:])
        layout.addWidget(view)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def go_restore(self):
        if self.selected_id():
            self.restore_version.setCurrentText(self.selected_id())
            self.show_page(2)

    def fetch_dialog(self):
        name, accepted = QInputDialog.getText(self, '取回备份', '输入测试机上已完成导出的完整版本名：')
        if accepted:
            self.start('fetch', snapshot=name.strip())

    def area_changed(self, *_):
        area = self.restore_area.currentData()
        self.restore_target.setText({'system': '/mnt/target', 'config': '/etc', 'software': '/mnt/software-restore', 'packages': '/'}.get(area, '/home'))
        self.restore_target.setEnabled(area != 'packages' and self.job is None and self.has_profile())
        self.restore_relative.setEnabled(area in ('home', 'config', 'software') and self.job is None and self.has_profile())
        self.relative_label.setText('选定用户路径' if area == 'home' else '选定文件路径')
        self.restore_relative.setPlaceholderText('例如 wyh/文档；留空恢复整个用户目录范围' if area == 'home' else '相对备份范围的路径；留空恢复整个范围')
        if hasattr(self, 'apply_button'):
            self.apply_button.setText('按清单安装软件' if area == 'packages' else '执行恢复')
        self.invalidate_preview()

    def signature(self):
        if not self.has_profile():
            return None
        return (json.dumps(asdict(self.current_profile()), sort_keys=True), self.restore_version.currentText(), self.restore_area.currentData(), self.restore_target.text(), self.restore_relative.text())

    def invalidate_preview(self, *_):
        self.preview_signature = None
        if hasattr(self, 'apply_button'):
            self.apply_button.setEnabled(False)

    def restore_action(self, action, apply=False):
        self.start(action, snapshot=self.restore_version.currentText().strip(), area=self.restore_area.currentData(),
                   target=self.restore_target.text().strip(), relative=self.restore_relative.text().strip() if self.restore_area.currentData() in ('home', 'config', 'software') else '', apply=apply)

    def apply_restore(self):
        if self.preview_signature is None or self.preview_signature != self.signature():
            self.invalidate_preview()
            return
        area = self.restore_area.currentData()
        detail = '将按精确版本安装 apt 软件，可能升级、降级或重启相关服务；禁止移除已有软件。不会自动回写备份中的配置与应用数据。' if area == 'packages' else '将还原系统文件，并清理备份之后新增的系统文件。' if area == 'system' else '将覆盖目标中选定范围内的同名文件，保留额外文件。'
        if QMessageBox.warning(self, '执行恢复', f'版本：{self.restore_version.currentText()}\n目标：{self.restore_target.text()}\n\n{detail}\n请确认已查看恢复预览。', QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Yes:
            self.restore_action('restore', True)

    def cleanup_export(self):
        name = self.selected_id()
        if name and QMessageBox.question(self, '清理测试机导出包', '将先校验当前电脑的副本，再删除测试机上此版本的暂存导出包：\n' + name) == QMessageBox.StandardButton.Yes:
            self.start('remove_export', snapshot=name)

    def start(self, action, transient_credentials=None, **arguments):
        if self.job:
            return
        try:
            profile = self.current_profile()
            checked = replace(profile, auth_method='auto') if action == 'setup_key' else profile
            checked.validate(remote=profile.mode == 'remote' and action not in ('catalog', 'verify'))
            if profile.mode == 'local' and sys.platform != 'linux':
                raise ValueError('本机系统快照需要 Linux。请在 Windows 上选择远程 Linux 模式。')
            if action in ('verify', 'fetch', 'upload', 'unpack', 'restore', 'remove_export'):
                validate_id(arguments.get('snapshot', ''))
            if action == 'restore' and not arguments.get('target', '').startswith('/'):
                raise ValueError('请输入 Linux 恢复目标的绝对路径。')
        except ValueError as exc:
            QMessageBox.information(self, '检查配置', str(exc))
            return
        self.active_profile = profile
        self.active_action = action
        self.active_arguments = arguments
        self.outcome = '运行中'
        self.task_started = dt.datetime.now().astimezone().isoformat(timespec='seconds')
        logs = self.store.directory / 'logs'
        logs.mkdir(exist_ok=True)
        self.log_path = logs / (dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f-') + profile.id[:8] + '-' + action + '.log')
        self.task_panel.begin(f'{action_title(action, arguments)} · {profile.name}')
        self.append_log(f'{action_title(action, arguments)} · {profile.name}')
        self.set_busy(True)
        values = self.credential_values() if profile.mode == 'remote' else {}
        self.session_credentials[self.credential_store.scope(profile)] = dict(values)
        values.update(transient_credentials or {})
        self.job = Job(profile, action, self.store.directory, credentials=values, **arguments)
        self.job.credential_accepted.connect(self.remember_credential)
        self.job.message.connect(self.append_log)
        self.job.update.connect(self.progress_changed)
        self.job.question.connect(self.answer_question)
        self.job.succeeded.connect(self.operation_success)
        self.job.failed.connect(self.operation_failed)
        self.job.cancelled.connect(self.operation_cancelled)
        self.job.finished.connect(self.operation_finished)
        self.job.start()

    def set_busy(self, busy):
        present = self.has_profile()
        for button in self.buttons:
            button.setEnabled(not busy and (present or not button.property('requiresProfile')))
        self.profile_combo.setEnabled(not busy and present)
        self.settings_content.setEnabled(not busy and present)
        for widget in (self.restore_version, self.restore_area, self.restore_target, self.restore_relative):
            widget.setEnabled(not busy and present)
        if self.restore_area.currentData() == 'packages':
            self.restore_target.setEnabled(False)
            self.restore_relative.setEnabled(False)
        elif self.restore_area.currentData() == 'system':
            self.restore_relative.setEnabled(False)
        self.cancel_button.setEnabled(busy)
        self.apply_button.setEnabled(not busy and present and self.preview_signature is not None and self.preview_signature == self.signature())
        if busy:
            self.progress.setRange(0, 0)

    def append_log(self, text):
        for message in clean_log_text(text).splitlines():
            line = dt.datetime.now().strftime('%H:%M:%S') + '  ' + message
            self.console.appendPlainText(line)
            if self.log_path:
                with self.log_path.open('a', encoding='utf-8') as stream:
                    stream.write(line + '\n')

    def progress_changed(self, value, text):
        self.task_panel.set_progress(value, clean_log_text(text))

    def answer_question(self, request):
        if request.kind == 'password':
            value, accepted = QInputDialog.getText(self, request.data['title'], request.data['text'], QLineEdit.EchoMode.Password)
            request.answer = value if accepted else None
        else:
            text = f"主机：{request.data['host']}\n类型：{request.data['type']}\n指纹：{request.data['fingerprint']}\n\n核对指纹后，是否信任并保存该主机密钥？"
            request.answer = QMessageBox.question(self, '首次连接此测试机', text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes
        request.event.set()

    def operation_success(self, result):
        if self.active_action == 'setup_key' and not self.apply_key_setup(result):
            self.outcome = '失败'
            self.append_log('密钥登录已验证，但连接配置未保存。密钥文件已保留，可重新保存配置。')
            self.task_panel.set_expanded(True)
            return
        self.outcome = '已完成'
        self.append_log('任务已完成。')
        if self.active_action == 'catalog':
            self.rows = result
            self.render_rows()
        elif self.active_action in ('backup', 'fetch') and self.active_profile.mode == 'remote':
            self.rows = bundles.catalog(self.active_profile.storage)
            self.render_rows()
        elif self.active_action == 'backup' and self.active_profile.mode == 'local':
            self.rows = result
            self.render_rows()
        elif self.active_action == 'restore' and not self.active_arguments.get('apply'):
            self.preview_signature = self.signature()
            self.text_dialog('软件安装预览 · 尚未安装' if self.active_arguments.get('area') == 'packages' else '恢复预览 · 尚未写入目标', str(result))
        elif self.active_action == 'restore':
            self.invalidate_preview()
        elif self.active_action == 'connect':
            self.text_dialog('环境与连接结果', json.dumps(result, ensure_ascii=False, indent=2))

    def apply_key_setup(self, result):
        old_scope = self.credential_store.scope(self.active_profile)
        values = dict(self.session_credentials.get(old_scope, {}))
        values.update(result.credentials)
        self.loading_profile = True
        try:
            self.fields['identity_file'].setText(result.identity_file)
            self.auth_method.setCurrentIndex(self.auth_method.findData('key'))
            self.remember_secrets.setChecked(self.active_arguments.get('remember', False))
            for kind, edit in self.secret_fields.items():
                edit.setText(values.get(kind, ''))
        finally:
            self.loading_profile = False
        self.credential_scope = self.credential_store.scope(self.current_profile())
        if not self.save_profile():
            return False
        self.append_log('密钥登录验证通过，已自动保存连接配置。')
        self.credential_note.setText('密码和口令仅在当前客户端会话中使用，未保存记录。' if not self.remember_secrets.isChecked() else '密码和口令已按你的选择保存到本机系统凭据库。')
        return True

    def operation_failed(self, error):
        self.outcome = '失败'
        error = clean_log_text(error)
        self.append_log('错误：' + error)
        self.task_panel.set_expanded(True)
        QMessageBox.warning(self, '操作未完成', error[-2500:])

    def operation_cancelled(self):
        self.outcome = '已取消'
        self.append_log('已请求取消；未完成目录会保留，后续请检查对应日志。')

    def operation_finished(self):
        self.history.append({'time': self.task_started, 'profile': self.active_profile.name, 'profile_id': self.active_profile.id,
                             'action': action_title(self.active_action, self.active_arguments), 'action_id': self.active_action,
                             'arguments': self.active_arguments, 'status': self.outcome, 'log': str(self.log_path)})
        self.history = self.history[-500:]
        self.history_path.write_text(json.dumps(self.history, ensure_ascii=False, indent=2), encoding='utf-8')
        finished = self.job
        self.job = None
        finished.deleteLater()
        self.set_busy(False)
        self.task_panel.finish(self.outcome)
        self.render_history()
        self.update_stats()
        if self.pending_close:
            self.close()

    def render_history(self):
        self.history_table.setRowCount(len(self.history))
        for row, entry in enumerate(reversed(self.history)):
            for column, value in enumerate([entry['time'][:19].replace('T', ' '), entry['profile'], entry['action'], entry['status']]):
                self.history_table.setItem(row, column, QTableWidgetItem(value))

    def open_history(self, row, *_):
        try:
            entry = self.history[-1 - row]
            text = self.read_log(entry)
            self.history_console.setPlainText(text)
            self.history_caption.setText(f"{entry['action']} · {entry['profile']} · {entry['status']}")
            self.render_history()
        except OSError as exc:
            QMessageBox.warning(self, '无法读取日志', str(exc))

    def read_log(self, entry):
        with Path(entry['log']).open('rb') as stream:
            stream.seek(max(0, stream.seek(0, 2) - 1000000))
            text = clean_log_text(stream.read().decode('utf-8', 'replace'))
        if entry.get('action_id'):
            entry['action'] = action_title(entry['action_id'], entry.get('arguments'))
        elif entry['action'] == '恢复预览' and any(line.split('  ', 1)[-1].strip() == '实际恢复' for line in text.splitlines()):
            entry['action'] = '执行恢复'
        return text

    def show_history(self):
        self.render_history()
        if self.history:
            self.history_table.selectRow(0)
            self.open_history(0)
        self.history_dialog.show()
        self.history_dialog.raise_()
        self.history_dialog.activateWindow()

    def restore_last_task(self):
        if self.history:
            entry = self.history[-1]
            try:
                self.console.setPlainText(self.read_log(entry))
            except OSError:
                return
            self.task_panel.title.setText(f"{entry['action']} · {entry['profile']}")
            self.task_panel.finish(entry['status'])
            self.task_panel.set_expanded(entry['status'] != '已完成')
            self.render_history()

    def export_log(self):
        self.export_log_text(self.console.toPlainText())

    def export_history_log(self):
        self.export_log_text(self.history_console.toPlainText())

    def export_log_text(self, text):
        path = QFileDialog.getSaveFileName(self, '导出日志', 'restorepoint-task.log', 'Log (*.log)')[0]
        if path:
            try:
                Path(path).write_text(text, encoding='utf-8')
            except OSError as exc:
                QMessageBox.warning(self, '无法导出日志', str(exc))

    def cancel_task(self):
        if self.job:
            if self.active_action == 'restore' and self.active_arguments.get('apply'):
                if QMessageBox.warning(self, '中断恢复', '中断正在执行的恢复可能留下不完整的目标状态。仍要请求中断吗？', QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                    return
            self.job.stop_event.set()
            self.cancel_button.setEnabled(False)
            self.task_status.setText('正在请求中断任务…')

    def closeEvent(self, event):
        if self.job:
            if QMessageBox.question(self, '任务运行中', '请求中断当前任务，并在后台任务结束后退出？') == QMessageBox.StandardButton.Yes:
                self.pending_close = True
                self.cancel_task()
            event.ignore()
        else:
            self.history_dialog.close()
            event.accept()


def main():
    parser = argparse.ArgumentParser(description='RestorePoint Qt desktop')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--screenshot', type=Path, help='渲染界面并保存截图后退出，不运行备份')
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName('RestorePoint')
    app.setOrganizationName('RestorePoint')
    apply_theme(app)
    directory = args.data_dir or Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
    directory.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(directory / 'desktop.lock'))
    if not lock.tryLock(100):
        QMessageBox.information(None, 'RestorePoint', '此配置目录已有一个客户端正在运行。')
        return 1
    try:
        window = MainWindow(directory)
    except Exception as exc:
        QMessageBox.critical(None, '无法启动', str(exc))
        return 1
    window.show()
    if args.screenshot:
        def capture():
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(args.screenshot))
            app.quit()
        QTimer.singleShot(700, capture)
    result = app.exec()
    lock.unlock()
    return result
