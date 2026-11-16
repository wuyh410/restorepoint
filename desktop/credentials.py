"""Store optional credentials only in the current user's OS credential vault."""
import hashlib
import json
from pathlib import Path
import sys

KINDS = ('login', 'key', 'sudo')


class CredentialError(RuntimeError):
    pass


def system_backend():
    # Select a system backend explicitly: never use a plaintext fallback or
    # third-party backend selected by environment configuration.
    if sys.platform == 'win32':
        from keyring.backends.Windows import WinVaultKeyring
        backend = WinVaultKeyring()
        backend.persist = 'local machine'
        return backend
    if sys.platform == 'linux':
        from keyring.backends.SecretService import Keyring
        return Keyring()
    raise CredentialError('当前系统不支持保存凭据。')


class CredentialStore:
    def __init__(self, directory, backend=None):
        self.directory = str(Path(directory).resolve())
        self.backend = backend

    def scope(self, profile):
        identity = [self.directory, profile.id, profile.host, profile.port,
                    profile.username, profile.identity_file]
        return hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()

    def _backend(self):
        if self.backend is None:
            self.backend = system_backend()
        return self.backend

    def load(self, profile):
        try:
            value = self._backend().get_password('RestorePoint', self.scope(profile))
            data = json.loads(value) if value else {}
            return {kind: data[kind] for kind in KINDS if isinstance(data.get(kind), str) and data[kind]}
        except Exception as exc:
            raise CredentialError('无法读取系统凭据库。请解锁凭据库，或为本次任务重新输入凭据。') from exc

    def save(self, profile, values):
        data = {kind: values[kind] for kind in KINDS if values.get(kind)}
        try:
            if data:
                self._backend().set_password('RestorePoint', self.scope(profile), json.dumps(data, ensure_ascii=False))
            else:
                self.delete(profile)
        except Exception as exc:
            raise CredentialError('无法保存到系统凭据库。Windows 请检查凭据管理器；Linux 请启用并解锁 Secret Service。') from exc

    def delete(self, profile):
        try:
            backend = self._backend()
            account = self.scope(profile)
            if backend.get_password('RestorePoint', account) is not None:
                backend.delete_password('RestorePoint', account)
        except Exception as exc:
            raise CredentialError('无法删除已保存的凭据，请解锁系统凭据库后重试。') from exc
