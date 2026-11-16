from dataclasses import dataclass, field
import threading
import time

from PySide6.QtCore import QThread, Signal

from .backend import Cancelled, LocalService, RemoteService
from .terminal import clean_log_text


@dataclass
class Question:
    kind: str
    data: dict
    event: threading.Event = field(default_factory=threading.Event)
    answer: object = None


class Job(QThread):
    message = Signal(str)
    update = Signal(int, str)
    question = Signal(object)
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()
    credential_accepted = Signal(str, str)

    def __init__(self, profile, action, data_directory, credentials=None, **arguments):
        super().__init__()
        self.profile, self.action, self.data_directory, self.arguments = profile, action, data_directory, arguments
        self.stop_event = threading.Event()
        self.secrets = []
        self.last_progress = 0
        self.credentials = dict(credentials or {})
        self.secrets.extend(value for value in self.credentials.values() if value)

    def get_credential(self, kind):
        value = self.credentials.get(kind)
        if kind == 'sudo' and not value and self.profile.sudo_uses_login:
            value = self.credentials.get('login')
        return value

    def accept_credential(self, kind, value):
        if value:
            self.credentials[kind] = value
            if value not in self.secrets:
                self.secrets.append(value)
            self.credential_accepted.emit(kind, value)

    def check(self):
        if self.stop_event.is_set():
            raise Cancelled('任务已中断。')

    def log(self, message):
        message = str(message)
        for secret in self.secrets:
            message = message.replace(secret, '••••')
        message = clean_log_text(message)
        for secret in self.secrets:
            message = message.replace(secret, '••••')
        if message:
            self.message.emit(str(message))

    def progress(self, value, text):
        now = time.monotonic()
        if value < 0 or value == 100 or now - self.last_progress >= 0.08:
            self.update.emit(value, text)
            self.last_progress = now

    def ask(self, kind, data):
        self.check()
        credential = data.get('credential') if kind == 'password' else None
        if credential and not data.get('retry'):
            value = self.get_credential(credential)
            if value:
                return value
        request = Question(kind, data)
        self.question.emit(request)
        while not request.event.wait(0.1):
            self.check()
        self.check()
        if request.answer is None:
            raise Cancelled('用户取消认证。')
        if kind == 'password' and request.answer:
            self.secrets.append(request.answer)
        return request.answer

    def run(self):
        try:
            service = LocalService(self.profile, self) if self.profile.mode == 'local' else RemoteService(self.profile, self, self.data_directory)
            result = service.run(self.action, **self.arguments)
            self.check()
            self.succeeded.emit(result)
        except Cancelled:
            self.cancelled.emit()
        except Exception as exc:
            message = str(exc)
            for secret in self.secrets:
                message = message.replace(secret, '••••')
            self.failed.emit(clean_log_text(message))
        finally:
            self.secrets.clear()
            self.credentials.clear()
