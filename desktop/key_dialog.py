"""User choices for setting up a dedicated SSH key."""
from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout

from .ssh_keys import key_path


class KeySetupDialog(QDialog):
    def __init__(self, profile, directory, parent=None):
        super().__init__(parent)
        self.setWindowTitle('一键设置 SSH 密钥')
        self.resize(540, 330)
        layout = QVBoxLayout(self)
        intro = QLabel(f'为 {profile.name} 设置专用密钥：生成密钥 → 安装公钥 → 验证登录 → 自动配置。')
        intro.setWordWrap(True)
        layout.addWidget(intro)
        notice = QLabel('私钥留在当前电脑，测试机只接收公钥。首次连接可能需要输入登录密码并核对主机指纹。')
        notice.setWordWrap(True)
        layout.addWidget(notice)
        existing = key_path(directory, profile).exists()
        self.passphrase = QLineEdit()
        self.confirmation = QLineEdit()
        for edit in (self.passphrase, self.confirmation):
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            edit.setEnabled(not existing)
        self.passphrase.setPlaceholderText('可选；留空生成无口令私钥')
        self.confirmation.setPlaceholderText('再次输入私钥口令')
        form = QFormLayout()
        form.addRow('私钥口令', self.passphrase)
        form.addRow('确认口令', self.confirmation)
        layout.addLayout(form)
        self.remember = QCheckBox('在本机保存该配置的密码和口令（可选）')
        self.remember.setChecked(profile.remember_secrets)
        layout.addWidget(self.remember)
        detail = QLabel('未勾选时不会保存密码或口令记录；输入的凭据只用于当前客户端会话。密钥文件会限制访问权限。')
        if existing:
            detail.setText('将复用已有专用密钥，不改变其口令；若需要解锁，会在任务中询问。未勾选保存时，口令只用于当前客户端会话。')
        detail.setWordWrap(True)
        layout.addWidget(detail)
        self.error = QLabel()
        self.error.setStyleSheet('color: #b34444;')
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('设置并验证')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self):
        if self.passphrase.text() != self.confirmation.text():
            self.error.setText('两次输入的私钥口令不一致。')
            return
        super().accept()
