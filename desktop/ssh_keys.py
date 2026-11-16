"""Create client-side SSH keys and install only the public key on Linux."""
from dataclasses import dataclass, field, replace
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


INSTALL_PUBLIC_KEY = r'''import base64,fcntl,json,os,pathlib,pwd,shlex,stat,sys
if sys.platform!='linux': raise SystemExit('SSH key setup requires Linux')
public=sys.argv[1]
parts=public.split()
if len(parts)!=3 or parts[0]!='ssh-ed25519' or '\n' in public or '\r' in public:
 raise SystemExit('Invalid public key')
base64.b64decode(parts[1],validate=True)
home=pathlib.Path(pwd.getpwuid(os.geteuid()).pw_dir)
directory=home/'.ssh'
try: directory.mkdir(mode=0o700)
except FileExistsError: pass
dfd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
try:
 info=os.fstat(dfd)
 if info.st_uid!=os.geteuid(): raise SystemExit('The SSH directory must belong to the SSH user')
 os.fchmod(dfd,0o700)
 try: existing=os.stat('authorized_keys',dir_fd=dfd,follow_symlinks=False)
 except FileNotFoundError: existing=None
 if existing is not None and (not stat.S_ISREG(existing.st_mode) or existing.st_uid!=os.geteuid()):
  raise SystemExit('authorized_keys must be a regular file owned by the SSH user')
 fd=os.open('authorized_keys',os.O_RDWR|os.O_APPEND|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=dfd)
 with os.fdopen(fd,'a+b',buffering=0) as stream:
  fcntl.flock(stream,fcntl.LOCK_EX)
  info=os.fstat(stream.fileno())
  if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid():
   raise SystemExit('authorized_keys must be a regular file owned by the SSH user')
  if info.st_size>8*1024*1024: raise SystemExit('authorized_keys is too large to update automatically')
  os.fchmod(stream.fileno(),0o600)
  stream.seek(0)
  original=stream.read()
  found=False
  for line in original.decode('utf-8','replace').splitlines():
   try: words=shlex.split(line,comments=True)
   except ValueError: continue
   if any(words[i:i+2]==parts[:2] for i in range(len(words)-1)): found=True
  if not found:
   pending=(b'\n' if original and not original.endswith(b'\n') else b'')+public.encode('ascii')+b'\n'
   while pending:
    count=stream.write(pending)
    if not count: raise OSError('Unable to append public key')
    pending=pending[count:]
   os.fsync(stream.fileno())
  print('RP_RESULT='+json.dumps({'added':not found,'path':str(directory/'authorized_keys')}))
finally: os.close(dfd)
'''


def key_path(directory, profile):
    identity = [profile.id, profile.host, profile.port, profile.username]
    name = hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()[:32]
    return Path(directory) / 'ssh-keys' / (name + '_ed25519')


def protect(path, directory=False):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(path, 'is_junction', lambda: False)():
        raise ValueError('密钥路径不能是符号链接或目录联接。')
    if directory != stat.S_ISDIR(info.st_mode) or not directory and not stat.S_ISREG(info.st_mode):
        raise ValueError('密钥路径类型不正确。')
    if sys.platform == 'win32':
        from .windows_security import protect_path
        protect_path(path, directory)
    else:
        if info.st_uid != os.getuid():
            raise ValueError('密钥文件或目录必须属于当前用户。')
        path.chmod(0o700 if directory else 0o600)


def write_new(path, payload):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            protect(path)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


@dataclass
class LocalKey:
    path: Path
    public: str
    fingerprint: str
    created: bool
    passphrase: str = field(default='', repr=False)


def prepare_key(directory, profile, context, encrypt=False):
    path = key_path(directory, profile)
    context.check()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    protect(path.parent, directory=True)
    created = not path.exists() and not path.is_symlink()
    secret = ''
    if created:
        key = Ed25519PrivateKey.generate()
        if encrypt:
            secret = context.get_credential('setup_key') or ''
            if not secret:
                raise ValueError('请为加密私钥填写口令。')
        protection = serialization.BestAvailableEncryption(secret.encode()) if secret else serialization.NoEncryption()
        write_new(path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, protection))
    else:
        protect(path)
        data = path.read_bytes()
        try:
            key = serialization.load_ssh_private_key(data, password=None)
        except TypeError:
            secret = context.get_credential('key') or context.get_credential('setup_key') or ''
            for attempt in range(2):
                if not secret or attempt:
                    secret = context.ask('password', {'title': '解锁已有专用私钥', 'text': '请输入已有专用私钥的口令。',
                                                      'credential': 'key', 'retry': bool(secret) or bool(attempt)})
                try:
                    key = serialization.load_ssh_private_key(data, password=secret.encode())
                    break
                except (TypeError, ValueError) as exc:
                    if attempt:
                        raise RuntimeError('无法解锁已有专用私钥，原文件未改变。') from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError('已有专用密钥类型不正确，未覆盖原文件。')
    public = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode('ascii')
    public += ' restorepoint-' + path.stem[:12]
    pub_path = path.with_name(path.name + '.pub')
    if not pub_path.exists() and not pub_path.is_symlink():
        write_new(pub_path, (public + '\n').encode('ascii'))
    else:
        protect(pub_path)
        if pub_path.read_text(encoding='ascii').strip() != public:
            raise ValueError('已有公钥文件与私钥不匹配，未覆盖原文件。')
    fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(base64.b64decode(public.split()[1])).digest()).decode().rstrip('=')
    return LocalKey(path, public, fingerprint, created, secret)


class SetupContext:
    def __init__(self, parent, key=None):
        self.parent, self.key = parent, key
        self.accepted = {}

    def __getattr__(self, name):
        return getattr(self.parent, name)

    def get_credential(self, kind):
        if self.key is not None:
            return self.key.passphrase if kind == 'key' else None
        return getattr(self.parent, 'get_credential', lambda kind: None)(kind)

    def accept_credential(self, kind, value):
        if value:
            self.accepted[kind] = value


@dataclass
class KeySetupResult:
    identity_file: str
    fingerprint: str
    created: bool
    added: bool
    credentials: dict = field(default_factory=dict, repr=False)


def setup_key(profile, context, directory, encrypt=False):
    from .backend import RemoteConnection, parse_result
    bootstrap_profile = profile
    if profile.auth_method == 'key' and not Path(profile.identity_file).is_file():
        bootstrap_profile = replace(profile, auth_method='auto', identity_file='')
    bootstrap_profile.validate(remote=True)
    context.progress(5, '连接测试机')
    bootstrap = SetupContext(context)
    with RemoteConnection(bootstrap_profile, bootstrap, directory) as connection:
        context.check()
        context.progress(25, '准备本机专用密钥')
        local = prepare_key(directory, profile, context, encrypt)
        if local.passphrase and hasattr(context, 'secrets') and local.passphrase not in context.secrets:
            context.secrets.append(local.passphrase)
        context.log(('已生成' if local.created else '复用已有') + '专用密钥：' + str(local.path))
        context.log('公钥指纹：' + local.fingerprint)
        context.progress(50, '安装公钥到测试机')
        installed = parse_result(connection.run(['python3', '-c', INSTALL_PUBLIC_KEY, local.public], raw=True))
        context.log('公钥已添加到测试机。' if installed['added'] else '测试机已有该公钥，无需重复添加。')
        context.check()
        context.progress(75, '验证新密钥登录')
        verification = replace(profile, identity_file=str(local.path), auth_method='key')
        isolated = SetupContext(context, local)
        try:
            with RemoteConnection(verification, isolated, directory):
                context.check()
        except Exception:
            context.log('新密钥未启用，原连接配置保持不变；专用密钥已保留，可重试设置。')
            raise
    values = dict(bootstrap.accepted)
    values['key'] = local.passphrase
    context.progress(95, '密钥登录验证通过，正在更新配置')
    return KeySetupResult(str(local.path), local.fingerprint, local.created, installed['added'], values)
