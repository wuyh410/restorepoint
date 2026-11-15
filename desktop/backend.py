"""Qt-independent operation adapters. Importing this module never runs a backup."""
import base64
import codecs
import datetime as dt
import getpass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from . import bundles
from .models import validate_id
from .runtime import application_executable, compiled_application, helper_source, resource_root


class Cancelled(RuntimeError):
    pass


def engine_command(engine, arguments):
    prefix = [application_executable()] if compiled_application() else [sys.executable, str(resource_root() / 'app.py')]
    return prefix + ['--engine', engine] + list(arguments)


def parse_result(text):
    for line in reversed(text.splitlines()):
        if line.startswith('RP_RESULT='):
            return json.loads(line[len('RP_RESULT='):])
    raise RuntimeError('操作未返回可识别的结果，请查看任务日志。')


def new_id():
    return dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ-') + uuid.uuid4().hex[:8]


def run_local(command, context, privileged=False):
    if privileged and sys.platform != 'linux':
        raise RuntimeError('本机系统备份仅适用于 Linux。Windows 请使用远程 Linux 模式。')
    password = None
    if privileged and os.geteuid() != 0:
        password = context.ask('password', {'title': 'Linux 管理员认证', 'text': '请输入当前用户的 sudo 密码（仅用于本次任务）。\n已配置免密 sudo 时可留空。'})
        command = ['sudo', '-S', '-p', '__RESTOREPOINT_SUDO__', '--'] + command
    context.progress(-1, '正在执行本机任务')
    context.log('开始：' + shlex.join(command))
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=(os.name != 'nt'), env=dict(os.environ, PYTHONUNBUFFERED='1'))
    if password is not None:
        try:
            process.stdin.write((password + '\n').encode())
            process.stdin.flush()
        except BrokenPipeError:
            pass
        password = None
    process.stdin.close()
    chunks = queue.Queue()
    def read_output():
        try:
            while chunk := process.stdout.read1(4096):
                chunks.put(chunk)
        finally:
            chunks.put(None)
    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    decoder, output, ended = codecs.getincrementaldecoder('utf-8')('replace'), [], False
    try:
        while not ended:
            context.check()
            try:
                chunk = chunks.get(timeout=0.1)
            except queue.Empty:
                continue
            if chunk is None:
                ended = True
                continue
            text = decoder.decode(chunk).replace('__RESTOREPOINT_SUDO__', '')
            output.append(text)
            if 'RP_RESULT=' not in text:
                context.log(text.rstrip())
        code = process.wait()
        if code != 0:
            raise RuntimeError(f'操作失败，退出码 {code}。\n' + ''.join(output)[-3000:])
        return ''.join(output)
    except Cancelled:
        try:
            os.killpg(process.pid, signal.SIGINT)
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.terminate()
            except OSError:
                pass
            context.log('已请求中断；请检查正在结束的权限任务和未完成目录。')
        raise
    finally:
        process.stdout.close()


class LocalService:
    def __init__(self, profile, context):
        self.profile, self.context = profile, context

    def run(self, action, snapshot='', area='home', target='', relative='', apply=False):
        self.profile.validate()
        root = ['--repository', self.profile.repository]
        if action == 'catalog':
            path = Path(self.profile.repository)
            if not path.exists():
                return []
            return parse_result(run_local(engine_command('gui_bridge', ['catalog'] + root), self.context, True))
        if action == 'backup':
            return parse_result(run_local(engine_command('gui_bridge', ['create'] + root + ['--reserve-gib', str(self.profile.reserve_gib)]), self.context, True))
        if action == 'plan':
            return run_local(engine_command('snapshot', root + ['plan', '--reserve-gib', str(self.profile.reserve_gib)]), self.context, True)
        if action == 'connect':
            return parse_result(run_local(engine_command('gui_bridge', ['doctor']), self.context))
        validate_id(snapshot)
        if action == 'verify':
            arguments = root + ['verify', snapshot]
        elif action == 'restore':
            arguments = root + ['restore', snapshot, '--area', area, '--target', target]
            if relative:
                arguments += ['--relative', relative]
            if apply:
                arguments += ['--apply']
        else:
            raise ValueError('本地模式不支持此操作。')
        return run_local(engine_command('snapshot', arguments), self.context, True)


class RemoteConnection:
    def __init__(self, profile, context, data_directory):
        self.profile, self.context, self.data_directory = profile, context, Path(data_directory)
        self.client = None
        self.username = profile.username

    def __enter__(self):
        import paramiko
        profile, context = self.profile, self.context
        profile.validate(remote=True)
        configuration = {}
        config_path = Path.home() / '.ssh/config'
        if config_path.is_file():
            with config_path.open(encoding='utf-8') as stream:
                configuration = paramiko.SSHConfig.from_file(stream).lookup(profile.host)
        if configuration.get('proxyjump') and not configuration.get('proxycommand'):
            raise ValueError('此版本请用直连或 SSH 配置的 ProxyCommand；暂不支持 ProxyJump。')
        self.username = profile.username or configuration.get('user') or getpass.getuser()
        host = configuration.get('hostname', profile.host)
        port = profile.port if profile.port != 22 else int(configuration.get('port', profile.port))
        identity = profile.identity_file or configuration.get('identityfile')
        known_hosts = self.data_directory / 'known_hosts'
        class AskHostKey(paramiko.MissingHostKeyPolicy):
            def missing_host_key(policy, client, hostname, key):
                fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')
                if not context.ask('hostkey', {'host': hostname, 'type': key.get_name(), 'fingerprint': fingerprint}):
                    raise Cancelled('未信任测试机主机密钥。')
                client.get_host_keys().add(hostname, key.get_name(), key)
                client.save_host_keys(str(known_hosts))
                known_hosts.chmod(0o600)
        secret = None
        for attempt in range(2):
            client = paramiko.SSHClient()
            client.load_system_host_keys()
            system_hosts = Path.home() / '.ssh/known_hosts'
            if system_hosts.exists():
                client.load_system_host_keys(str(system_hosts))
            if known_hosts.exists():
                client.load_host_keys(str(known_hosts))
            client.set_missing_host_key_policy(AskHostKey())
            proxy = paramiko.ProxyCommand(configuration['proxycommand']) if configuration.get('proxycommand') else None
            try:
                context.check()
                client.connect(host, port=port, username=self.username, key_filename=identity or None,
                               password=secret, passphrase=secret, timeout=12, auth_timeout=20, banner_timeout=12, sock=proxy)
                client.get_transport().set_keepalive(15)
                self.client = client
                context.log(f'已连接 {self.username}@{host}:{port}')
                return self
            except (paramiko.AuthenticationException, paramiko.PasswordRequiredException):
                client.close()
                if proxy:
                    proxy.close()
                if attempt:
                    raise RuntimeError('SSH 认证失败，请检查用户名、密码或私钥。')
                secret = context.ask('password', {'title': 'SSH 认证', 'text': f'请输入 {self.username}@{host} 的密码，或私钥解锁口令。'})
            except Exception:
                client.close()
                if proxy:
                    proxy.close()
                raise

    def __exit__(self, *unused):
        if self.client:
            self.client.close()

    def run(self, arguments, root=False, raw=False):
        words = list(arguments) if raw else ['python3', self.profile.remote_helper] + list(arguments)
        marker = '__RESTOREPOINT_SUDO__'
        command = shlex.join(words)
        if root and self.username != 'root':
            command = 'stty -echo; exec sudo -S -p ' + shlex.quote(marker) + ' -- ' + command
        channel = self.client.get_transport().open_session(timeout=12)
        if root:
            channel.get_pty(term='dumb', width=160, height=40)
        channel.set_combine_stderr(True)
        channel.exec_command(command)
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        output, pending, prompts = [], '', 0
        try:
            while True:
                self.context.check()
                if channel.recv_ready():
                    text = decoder.decode(channel.recv(32768)).replace('\r', '')
                    pending += text
                    while marker in pending:
                        pending = pending.replace(marker, '', 1)
                        prompts += 1
                        if prompts > 3:
                            raise RuntimeError('sudo 认证失败。')
                        secret = self.context.ask('password', {'title': '测试机管理员认证', 'text': f'请输入测试机用户 {self.username} 的 sudo 密码。'})
                        channel.sendall((secret + '\n').encode())
                        secret = None
                    while '\n' in pending:
                        line, pending = pending.split('\n', 1)
                        output.append(line + '\n')
                        if line and not line.startswith('RP_RESULT='):
                            self.context.log(line)
                elif channel.exit_status_ready():
                    break
                else:
                    time.sleep(0.04)
            if pending:
                output.append(pending)
                self.context.log(pending)
            code = channel.recv_exit_status()
            if code:
                raise RuntimeError(f'测试机操作失败，退出码 {code}。\n' + ''.join(output)[-2500:])
            return ''.join(output)
        except Cancelled:
            if root:
                try:
                    channel.send(b'\x03')
                    time.sleep(0.3)
                except OSError:
                    pass
            self.context.log('已请求中断远端任务；请检查未完成导出或恢复目录。')
            raise
        finally:
            channel.close()

    def install(self):
        helper = PurePosixPath(self.profile.remote_helper)
        if helper.name != 'portable.py' or helper.parent.name in ('lib', 'bin', 'etc', 'usr', 'local', 'opt', 'home', 'root', 'tmp'):
            raise ValueError('组件安装路径必须是专用目录中的 portable.py，例如 /usr/local/lib/test-backup/portable.py。')
        directory = self.run(['mktemp', '-d', '/tmp/restorepoint.XXXXXXXX'], raw=True).strip()
        if re.fullmatch(r'/tmp/restorepoint\.[A-Za-z0-9]+', directory) is None:
            raise RuntimeError('未获取安全的组件上传临时目录。')
        with self.client.open_sftp() as sftp:
            for name in ('snapshot.py', 'portable.py', 'gui_bridge.py'):
                self.context.check()
                sftp.put(str(helper_source(name)), directory + '/' + name)
                sftp.chmod(directory + '/' + name, 0o600)
        installer = """import os,pathlib,shutil,sys,tempfile
if sys.platform!='linux': raise SystemExit('The backup source must be Linux')
parent=pathlib.Path(sys.argv[2])
if not parent.is_absolute() or parent.resolve()!=parent: raise SystemExit('Invalid installation path')
existing=parent
while not existing.exists(): existing=existing.parent
for item in [existing,*existing.parents]:
 s=item.stat()
 if s.st_uid!=0 or s.st_mode&0o022: raise SystemExit('Installation ancestor must be controlled by root')
parent.mkdir(parents=True,exist_ok=True,mode=0o755)
for name in ['snapshot.py','portable.py','gui_bridge.py']:
 target=parent/name
 if target.is_symlink(): raise SystemExit('Refusing to replace a symlink')
 fd,temporary=tempfile.mkstemp(prefix='.'+name+'-',dir=parent)
 try:
  with os.fdopen(fd,'wb') as out,open(pathlib.Path(sys.argv[1])/name,'rb') as src:
   shutil.copyfileobj(src,out)
   out.flush()
   os.fsync(out.fileno())
  os.chown(temporary,0,0)
  os.chmod(temporary,0o644)
  os.replace(temporary,target)
 finally:
  if os.path.exists(temporary): os.unlink(temporary)
print('测试机组件已安装：'+str(parent))
"""
        self.run(['python3', '-c', installer, directory, str(helper.parent)], root=True, raw=True)
        self.run(['python3', '-c', 'import shutil,sys;shutil.rmtree(sys.argv[1])', directory], raw=True)


class RemoteService:
    def __init__(self, profile, context, data_directory):
        self.profile, self.context, self.data_directory = profile, context, data_directory

    def fetch(self, connection, name):
        validate_id(name)
        repository = Path(self.profile.storage)
        if not repository.is_absolute():
            raise ValueError('备份包保存目录需要绝对路径。')
        repository.mkdir(parents=True, exist_ok=True)
        final = repository / name
        if final.exists():
            return bundles.verify(final, self.context)
        incoming = repository / ('.incoming-' + name + '-' + uuid.uuid4().hex[:8])
        incoming.mkdir(mode=0o700)
        remote = self.profile.export_root.rstrip('/') + '/' + name
        with connection.client.open_sftp() as sftp:
            sftp.get(remote + '/manifest.json', str(incoming / 'manifest.json'))
            data = bundles.manifest(incoming)
            if data['id'] != name:
                raise ValueError('远端清单与所选版本不一致。')
            total = sum(r['bytes'] for r in data['files'].values())
            if shutil.disk_usage(repository).free < total + self.profile.reserve_gib * 1024 ** 3:
                raise ValueError('开发机可用空间不足，未开始下载备份包。')
            done = 0
            for filename, record in data['files'].items():
                def progress(count, size):
                    self.context.check()
                    self.context.progress(int((done + count) * 100 / max(total, 1)), '下载 ' + filename)
                sftp.get(remote + '/' + filename, str(incoming / filename), callback=progress)
                done += record['bytes']
        data = bundles.verify(incoming, self.context)
        self.context.check()
        incoming.rename(final)
        self.context.log('备份已保存：' + str(final))
        return data

    def run(self, action, snapshot='', area='home', target='', relative='', apply=False):
        self.profile.validate()
        if action == 'catalog':
            return bundles.catalog(self.profile.storage)
        if action == 'verify':
            return bundles.verify(Path(self.profile.storage) / validate_id(snapshot), self.context)
        if action in ('upload', 'unpack', 'restore', 'remove_export'):
            validate_id(snapshot)
        with RemoteConnection(self.profile, self.context, self.data_directory) as connection:
            profile = self.profile
            inbox = profile.inbox or '/var/tmp/restorepoint-inbox-' + re.sub(r'[^a-zA-Z0-9_-]', '_', connection.username)
            if action == 'connect':
                result = connection.run(['python3', '-c', "import json,platform,shutil,socket;print('RP_RESULT='+json.dumps({'hostname':socket.gethostname(),'system':platform.system(),'tools':{p:bool(shutil.which(p)) for p in ['python3','sudo','rsync','tar','gzip']}}))"], raw=True)
                data = parse_result(result)
                if data['system'] != 'Linux':
                    raise ValueError('远程备份来源需要是 Linux 测试机。')
                return data
            if action == 'install':
                connection.install()
                return '组件部署完成'
            if action in ('plan', 'backup'):
                arguments = ['plan' if action == 'plan' else 'create', '--export-root', profile.export_root, '--reserve-gib', str(profile.reserve_gib)]
                if action == 'backup':
                    snapshot = new_id()
                    arguments += ['--snapshot', snapshot]
                    self.context.log('新版本：' + snapshot)
                self.context.progress(-1, '测试机正在检查环境' if action == 'plan' else '测试机正在生成完整备份包')
                result = connection.run(arguments, root=True)
                return self.fetch(connection, snapshot) if action == 'backup' else result
            if action == 'fetch':
                return self.fetch(connection, validate_id(snapshot))
            bundle = Path(profile.storage) / snapshot
            if action == 'upload':
                data = bundles.verify(bundle, self.context)
                connection.run(['inbox', '--directory', inbox, '--snapshot', snapshot])
                files = ['manifest.json'] + list(data['files'])
                total = sum((bundle / f).stat().st_size for f in files)
                done = 0
                with connection.client.open_sftp() as sftp:
                    for filename in files:
                        size = (bundle / filename).stat().st_size
                        def progress(count, unused):
                            self.context.check()
                            self.context.progress(int((done + count) * 100 / max(total, 1)), '上传 ' + filename)
                        remote = inbox.rstrip('/') + '/' + snapshot + '/' + filename
                        sftp.put(str(bundle / filename), remote, callback=progress)
                        sftp.chmod(remote, 0o600)
                        done += size
                return connection.run(['verify', '--bundle', inbox.rstrip('/') + '/' + snapshot])
            if action == 'unpack':
                self.context.progress(-1, '测试机正在解包和建立校验清单')
                return connection.run(['unpack', '--bundle', inbox.rstrip('/') + '/' + snapshot,
                                       '--destination', profile.staging.rstrip('/') + '/' + snapshot,
                                       '--reserve-gib', str(profile.reserve_gib)], root=True)
            if action == 'restore':
                arguments = ['restore', '--prepared', profile.staging.rstrip('/') + '/' + snapshot, '--area', area, '--target', target]
                if relative:
                    arguments += ['--relative', relative]
                if apply:
                    arguments.append('--apply')
                return connection.run(arguments, root=True)
            if action == 'remove_export':
                bundles.verify(bundle, self.context)
                checksum = hashlib.sha256((bundle / 'manifest.json').read_bytes()).hexdigest()
                return connection.run(['remove-export', '--export-root', profile.export_root, '--snapshot', snapshot, '--manifest-sha256', checksum], root=True)
            raise ValueError('未知操作：' + action)
