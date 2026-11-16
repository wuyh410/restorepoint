from dataclasses import asdict, dataclass, fields
from pathlib import Path
import json
import os
import re
import uuid

ID_PATTERN = r'\d{8}T\d{6}\.\d{6}Z-[0-9a-f]{8}'


@dataclass
class Profile:
    id: str = ''
    name: str = '我的测试机'
    mode: str = 'remote'
    repository: str = '/var/backups/local-snapshots'
    storage: str = ''
    host: str = ''
    port: int = 22
    username: str = ''
    identity_file: str = ''
    auth_method: str = 'auto'
    remember_secrets: bool = False
    sudo_uses_login: bool = False
    remote_helper: str = '/usr/local/lib/test-backup/portable.py'
    export_root: str = '/var/backups/portable-snapshots'
    inbox: str = ''
    staging: str = '/var/tmp/test-backup-restore'
    reserve_gib: int = 8

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex
        if not self.storage:
            self.storage = str(Path.home() / 'RestorePoint' / 'Backups')

    @classmethod
    def from_dict(cls, data):
        return cls(**{f.name: data[f.name] for f in fields(cls) if f.name in data})

    def validate(self, remote=False):
        if not self.name.strip():
            raise ValueError('请输入配置名称。')
        if self.mode not in ('local', 'remote'):
            raise ValueError('无效的备份模式。')
        if self.auth_method not in ('auto', 'password', 'key'):
            raise ValueError('无效的 SSH 认证方式。')
        if not 1 <= int(self.port) <= 65535 or not 8 <= int(self.reserve_gib) <= 65536:
            raise ValueError('端口或预留空间无效，至少预留 8 GiB。')
        if self.mode == 'local' and not self.repository.startswith('/'):
            raise ValueError('本地仓库需要 Linux 绝对路径。')
        if not self.storage.strip():
            raise ValueError('请选择备份包保存目录。')
        if remote:
            if self.auth_method == 'key' and not self.identity_file.strip():
                raise ValueError('密钥认证需要选择私钥文件。')
            if not self.host.strip() or any(c.isspace() for c in self.host):
                raise ValueError('请输入主机地址或 SSH 配置别名。')
            for value in (self.remote_helper, self.export_root, self.staging, self.inbox):
                if value and (not value.startswith('/') or '..' in value.split('/') or '\n' in value or '\r' in value):
                    raise ValueError('远程目录需要不含 .. 或换行的 Linux 绝对路径。')


class SettingsStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'profiles.json'

    def load(self):
        if not self.path.exists():
            local = Profile(name='本机 Linux', mode='local')
            remote = Profile(name='远程测试机')
            return [local, remote] if os.name != 'nt' else [remote]
        data = json.loads(self.path.read_text(encoding='utf-8'))
        if data.get('schema') != 1:
            raise ValueError('不支持的配置文件格式。')
        profiles = [Profile.from_dict(p) for p in data['profiles']]
        return profiles

    def save(self, profiles):
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'schema': 1, 'profiles': [asdict(p) for p in profiles]}, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.chmod(0o600)
        temporary.replace(self.path)


def import_legacy(data):
    target = data.get('SshTarget', '')
    user, host = target.split('@', 1) if '@' in target else ('', target)
    machine = data.get('MachineName', 'test-machine')
    return Profile(name=machine, host=host, username=user, port=int(data.get('Port', 22)),
                   identity_file=data.get('IdentityFile', ''),
                   storage=str(Path(data.get('BackupRoot', str(Path.home() / 'RestorePoint'))) / machine),
                   remote_helper=data.get('RemoteHelper', Profile.remote_helper),
                   export_root=data.get('ExportRoot', Profile.export_root),
                   inbox=data.get('RestoreInbox', ''), staging=data.get('RestoreStagingRoot', Profile.staging),
                   reserve_gib=int(data.get('ReserveGiB', 8)))


def validate_id(value):
    if re.fullmatch(ID_PATTERN, value) is None:
        raise ValueError('请选择完整的备份版本。')
    return value
