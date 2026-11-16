"""Choose backup content before estimating space or copying files."""
from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QVBoxLayout
from .scopes import normalize


class BackupDialog(QDialog):
    def __init__(self, profile, parent=None):
        super().__init__(parent)
        # Imported on opening the dialog, after the main window module loads.
        from .window import combo_box
        self.setWindowTitle('选择备份类型')
        self.resize(600, 460)
        self.plan_only = False
        self.updating = False
        layout = QVBoxLayout(self)
        intro = QLabel('只复制本次选中的内容。系统、软件和用户可以分别创建版本，需要时再选择完整备份。')
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.preset = combo_box()
        for text, value in [('完整备份', 'full'), ('系统备份', 'system'), ('软件备份', 'software'), ('用户备份', 'home'), ('自选范围', 'custom')]:
            self.preset.addItem(text, value)
        form.addRow('备份类型', self.preset)
        layout.addLayout(form)
        self.checks = {}
        selected = normalize(profile.backup_areas)
        for area, title in [('system', '系统：系统文件、启动文件、基础配置，保留整体回滚能力'),
                             ('software', '软件：软件清单、版本、配置及应用数据'),
                             ('home', '用户：/home 下的文档、代码、隐藏配置'),
                             ('config', '仅系统配置：/etc，适合修改配置前备份')]:
            checkbox = QCheckBox(title)
            checkbox.setChecked(area in selected)
            checkbox.toggled.connect(self.changed)
            self.checks[area] = checkbox
            layout.addWidget(checkbox)
        self.software_mode = combo_box()
        self.software_mode.addItem('清单重装 · 不打包程序文件，保留配置及应用数据', 'reinstall')
        self.software_mode.addItem('程序文件打包 · 同时保留程序、配置和数据', 'files')
        self.software_mode.setCurrentIndex(self.software_mode.findData(profile.software_mode))
        form = QFormLayout()
        form.addRow('软件备份方式', self.software_mode)
        layout.addLayout(form)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.error = QLabel()
        self.error.setStyleSheet('color: #b34444;')
        layout.addWidget(self.error)
        layout.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('创建备份')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        plan = buttons.addButton('先检查空间', QDialogButtonBox.ButtonRole.ActionRole)
        plan.clicked.connect(self.plan)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        name = 'full' if set(selected) == {'system', 'home'} else selected[0] if len(selected) == 1 and selected[0] != 'config' else 'custom'
        self.preset.setCurrentIndex(self.preset.findData(name))
        self.preset.currentIndexChanged.connect(self.choose_preset)
        self.changed(update_preset=False)

    def choose_preset(self, *_):
        preset = self.preset.currentData()
        if preset == 'custom':
            return
        selected = {'system', 'home'} if preset == 'full' else {preset}
        self.updating = True
        for area, checkbox in self.checks.items():
            checkbox.setChecked(area in selected)
        if preset == 'software':
            self.software_mode.setCurrentIndex(self.software_mode.findData('reinstall'))
        self.updating = False
        self.changed(update_preset=False)

    def changed(self, *_, update_preset=True):
        if self.updating:
            return
        self.updating = True
        system = self.checks['system'].isChecked()
        if system:
            self.checks['config'].setChecked(False)
            self.checks['software'].setChecked(True)
        self.checks['config'].setEnabled(not system)
        self.checks['software'].setEnabled(not system)
        self.software_mode.setEnabled(not system and self.checks['software'].isChecked())
        self.notice.setText('Linux 系统与软件有共享文件。系统备份保留共享程序以支持整体回滚，不重复打包软件；用户目录单独选择。' if system else
                            '清单重装依赖相同系统版本和可用安装源；程序文件恢复适用于相同系统版本。应用数据可能占用较多空间。')
        if update_preset:
            self.preset.blockSignals(True)
            self.preset.setCurrentIndex(self.preset.findData('custom'))
            self.preset.blockSignals(False)
        self.updating = False

    def selected(self):
        return normalize([area for area, checkbox in self.checks.items() if checkbox.isChecked()])

    def mode(self):
        return 'files' if self.checks['system'].isChecked() else self.software_mode.currentData()

    def plan(self):
        self.plan_only = True
        self.accept()

    def accept(self):
        try:
            self.selected()
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        super().accept()
