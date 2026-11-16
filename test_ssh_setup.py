from contextlib import contextmanager
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from desktop import ssh_keys
from desktop.models import Profile, SettingsStore
from test_credentials import MemoryVault
from test_desktop import Context


def run_installer(home, public):
    wrapper = """import runpy,sys,types
home,script,public=sys.argv[1:]
sys.modules['pwd']=types.SimpleNamespace(getpwuid=lambda uid:types.SimpleNamespace(pw_dir=home))
sys.argv=['key-installer',public]
exec(compile(script,'key-installer','exec'))
"""
    return subprocess.run([sys.executable, '-c', wrapper, str(home), ssh_keys.INSTALL_PUBLIC_KEY, public], capture_output=True, timeout=15)


class SetupContext(Context):
    def __init__(self, values=None):
        super().__init__()
        self.values = dict(values or {})
        self.secrets = list(self.values.values())

    def get_credential(self, kind):
        return self.values.get(kind)

    def ask(self, kind, data):
        self.questions.append((kind, data))
        if kind == 'hostkey':
            return True
        return 'fixture-key-passphrase' if data.get('credential') == 'key' else 'temporary-password'


@contextmanager
def setup_server(home, reject_keys=False):
    """A concurrent loopback SSH service using only a temporary fake home."""
    import paramiko
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(5)
    listener.settimeout(.1)
    host_key = paramiko.RSAKey.generate(2048)
    stopped = threading.Event()
    transports, workers, passwords, public_keys, commands, errors = [], [], [], [], [], []

    class Server(paramiko.ServerInterface):
        def get_allowed_auths(self, username):
            return 'password,publickey'

        def check_auth_password(self, username, password):
            passwords.append(password)
            return paramiko.AUTH_SUCCESSFUL if username == 'fixture' and password == 'temporary-password' else paramiko.AUTH_FAILED

        def check_auth_publickey(self, username, key):
            public_keys.append(key.get_base64())
            path = home / '.ssh/authorized_keys'
            present = path.exists() and (key.get_name() + ' ' + key.get_base64()) in path.read_text()
            return paramiko.AUTH_SUCCESSFUL if username == 'fixture' and present and not reject_keys else paramiko.AUTH_FAILED

        def check_channel_request(self, kind, channel_id):
            return paramiko.OPEN_SUCCEEDED if kind == 'session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

        def check_channel_exec_request(self, channel, command):
            words = shlex.split(command.decode())
            if len(words) != 4 or words[:3] != ['python3', '-c', ssh_keys.INSTALL_PUBLIC_KEY]:
                return False
            commands.append(command.decode())
            def execute():
                try:
                    result = run_installer(home, words[3])
                    channel.sendall(result.stdout + result.stderr)
                    channel.send_exit_status(result.returncode)
                except Exception as exc:
                    errors.append(exc)
                finally:
                    channel.close()
            worker = threading.Thread(target=execute, daemon=True)
            workers.append(worker)
            worker.start()
            return True

    def serve(client):
        transport = paramiko.Transport(client)
        transports.append(transport)
        channels = []
        try:
            transport.add_server_key(host_key)
            transport.start_server(server=Server())
            while transport.is_active() and not stopped.is_set():
                channel = transport.accept(.1)
                if channel:
                    channels.append(channel)
        except (EOFError, OSError):
            pass
        except Exception as exc:
            if not stopped.is_set():
                errors.append(exc)
        finally:
            transport.close()

    def accept():
        while not stopped.is_set():
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            worker = threading.Thread(target=serve, args=(client,), daemon=True)
            workers.append(worker)
            worker.start()
    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1], passwords, public_keys, commands
    finally:
        stopped.set()
        listener.close()
        for transport in transports:
            transport.close()
        thread.join(3)
        for worker in workers:
            worker.join(3)
        if errors:
            raise errors[0]


class LocalKeyTest(unittest.TestCase):
    def test_generation_permissions_encryption_and_reuse(self):
        from cryptography.hazmat.primitives import serialization
        for encrypted in (False, True):
            with self.subTest(encrypted=encrypted), tempfile.TemporaryDirectory() as temp:
                profile = Profile(host='fixture.invalid')
                context = SetupContext({'setup_key': 'fixture-key-passphrase'} if encrypted else {})
                local = ssh_keys.prepare_key(temp, profile, context, encrypted)
                before = local.path.read_bytes()
                self.assertTrue(local.created)
                self.assertTrue(local.public.startswith('ssh-ed25519 '))
                self.assertTrue(local.fingerprint.startswith('SHA256:'))
                key = serialization.load_ssh_private_key(before, password=b'fixture-key-passphrase' if encrypted else None)
                self.assertTrue(key)
                repeated = ssh_keys.prepare_key(temp, profile, context, encrypted)
                self.assertFalse(repeated.created)
                self.assertEqual(repeated.fingerprint, local.fingerprint)
                self.assertEqual(local.path.read_bytes(), before)
                if sys.platform == 'win32':
                    from desktop.windows_security import current_sid, read_acl
                    for path in (local.path, local.path.parent):
                        acl = read_acl(path)
                        self.assertIn(current_sid(), acl)
                        self.assertIn('D:P', acl)
                        self.assertNotIn(';;;WD)', acl)
                        self.assertNotIn(';;;BU)', acl)
                else:
                    self.assertEqual(stat.S_IMODE(local.path.stat().st_mode), 0o600)
                    self.assertEqual(stat.S_IMODE(local.path.parent.stat().st_mode), 0o700)

    @unittest.skipIf(sys.platform == 'win32', 'POSIX symlinks')
    def test_existing_symlink_is_rejected_without_touching_its_target(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Profile(host='fixture.invalid')
            target = Path(temp) / 'preserve'
            target.write_text('existing data')
            path = ssh_keys.key_path(temp, profile)
            path.parent.mkdir()
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                ssh_keys.prepare_key(temp, profile, SetupContext())
            self.assertEqual(target.read_text(), 'existing data')


@unittest.skipUnless(sys.platform == 'linux', 'Linux remote public-key installer')
class InstallerTest(unittest.TestCase):
    def test_append_preserves_existing_keys_and_repeated_runs_are_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'remote-home'
            home.mkdir()
            ssh = home / '.ssh'
            ssh.mkdir()
            authorized = ssh / 'authorized_keys'
            original = b'# preserve existing comment\nssh-ed25519 AAAA existing-no-newline'
            authorized.write_bytes(original)
            local = ssh_keys.prepare_key(root / 'local', Profile(host='fixture.invalid'), SetupContext())
            first = run_installer(home, local.public)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertTrue(authorized.read_bytes().startswith(original + b'\n'))
            after = authorized.read_bytes()
            second = run_installer(home, local.public)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(authorized.read_bytes(), after)
            self.assertEqual(stat.S_IMODE(authorized.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(ssh.stat().st_mode), 0o700)

    def test_remote_symlink_is_rejected_without_modifying_the_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            (home / '.ssh').mkdir(parents=True)
            target = root / 'unrelated'
            target.write_text('must not change')
            (home / '.ssh/authorized_keys').symlink_to(target)
            public = ssh_keys.prepare_key(root / 'local', Profile(), SetupContext()).public
            result = run_installer(home, public)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(target.read_text(), 'must not change')

    def test_real_ssh_setup_verifies_the_key_and_can_be_repeated(self):
        for encrypted in (False, True):
            with self.subTest(encrypted=encrypted), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                home = root / 'remote-home'
                home.mkdir()
                local = root / 'client'
                local.mkdir()
                with setup_server(home) as (port, passwords, keys, commands):
                    profile = Profile(host='127.0.0.1', username='fixture', port=port, auth_method='password')
                    before = asdict(profile)
                    context = SetupContext({'login': 'temporary-password', 'setup_key': 'fixture-key-passphrase'} if encrypted else {'login': 'temporary-password'})
                    result = ssh_keys.setup_key(profile, context, local, encrypted)
                    self.assertEqual(asdict(profile), before)
                    self.assertTrue(result.added)
                    self.assertEqual(passwords, ['temporary-password'])
                    self.assertTrue(keys)
                    self.assertTrue(commands)
                    self.assertNotIn('sudo', '\n'.join(commands))
                    configured = replace(profile, identity_file=result.identity_file, auth_method='key')
                    context.values.update(result.credentials)
                    repeated = ssh_keys.setup_key(configured, context, local)
                    self.assertFalse(repeated.created)
                    self.assertFalse(repeated.added)
                    self.assertEqual(passwords, ['temporary-password'])
                    self.assertNotIn('fixture-key-passphrase', '\n'.join(context.messages))
                    self.assertEqual(list(local.glob('*.json')), [])

    def test_key_verification_failure_keeps_the_original_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            home.mkdir()
            local = root / 'client'
            local.mkdir()
            with setup_server(home, reject_keys=True) as (port, passwords, keys, commands):
                profile = Profile(host='127.0.0.1', username='fixture', port=port, auth_method='password')
                SettingsStore(local).save([profile])
                before = (local / 'profiles.json').read_bytes()
                with self.assertRaisesRegex(RuntimeError, '私钥认证失败'):
                    ssh_keys.setup_key(profile, SetupContext({'login': 'temporary-password'}), local)
                self.assertEqual((local / 'profiles.json').read_bytes(), before)
                self.assertTrue(ssh_keys.key_path(local, profile).is_file())
                self.assertEqual(passwords, ['temporary-password'])


class KeySetupUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        from desktop.window import apply_theme
        cls.app = QApplication.instance() or QApplication([])
        apply_theme(cls.app)

    def test_saving_credentials_is_unchecked_by_default_and_dialog_validates_confirmation(self):
        from PySide6.QtWidgets import QDialog
        from desktop.key_dialog import KeySetupDialog
        with tempfile.TemporaryDirectory() as temp:
            dialog = KeySetupDialog(Profile(host='fixture.invalid'), temp)
            self.assertFalse(dialog.remember.isChecked())
            self.assertEqual(dialog.passphrase.text(), '')
            dialog.passphrase.setText('fixture-secret')
            dialog.accept()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
            self.assertTrue(dialog.error.text())
            dialog.confirmation.setText('fixture-secret')
            dialog.accept()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            dialog.close()

    def test_success_applies_key_mode_and_only_persists_secrets_when_selected(self):
        from desktop.credentials import CredentialStore
        from desktop.window import MainWindow
        for remember in (False, True):
            with self.subTest(remember=remember), tempfile.TemporaryDirectory() as temp:
                profile = Profile(host='fixture.invalid')
                SettingsStore(temp).save([profile])
                window = MainWindow(temp)
                vault = MemoryVault()
                window.credential_store = CredentialStore(temp, vault)
                window.active_profile = profile
                window.active_arguments = {'remember': remember}
                window.session_credentials[window.credential_store.scope(profile)] = {'login': 'fixture-login'}
                result = ssh_keys.KeySetupResult(str(Path(temp) / 'key'), 'SHA256:fixture', True, True,
                                                {'key': 'fixture-key-passphrase'})
                try:
                    self.assertTrue(window.apply_key_setup(result))
                    saved = SettingsStore(temp).load()[0]
                    self.assertEqual(saved.auth_method, 'key')
                    self.assertEqual(saved.identity_file, result.identity_file)
                    self.assertEqual(saved.remember_secrets, remember)
                    self.assertEqual(bool(vault.values), remember)
                    self.assertNotIn('fixture-key-passphrase', (Path(temp) / 'profiles.json').read_text())
                    self.assertNotIn('fixture-login', (Path(temp) / 'profiles.json').read_text())
                finally:
                    window.close()

    def test_setup_passphrase_is_not_written_to_task_history_or_logs(self):
        from desktop.jobs import Job
        from desktop.window import MainWindow
        from desktop.key_dialog import KeySetupDialog
        with tempfile.TemporaryDirectory() as temp:
            profile = Profile(host='fixture.invalid')
            SettingsStore(temp).save([profile])
            window = MainWindow(temp)
            window.show()
            window.show_page(3)
            self.app.processEvents()
            def run(*args, **kwargs):
                return ssh_keys.KeySetupResult(str(Path(temp) / 'key'), 'SHA256:fixture', True, True,
                                              {'key': 'fixture-setup-secret'})
            try:
                with mock.patch('desktop.jobs.RemoteService') as service:
                    service.return_value.run.side_effect = run
                    window.start('setup_key', transient_credentials={'setup_key': 'fixture-setup-secret'}, encrypt=True, remember=False)
                    job = window.job
                    self.assertTrue(job.wait(3000))
                    self.app.processEvents()
                self.assertNotIn('fixture-setup-secret', window.history_path.read_text())
                self.assertNotIn('fixture-setup-secret', window.console.toPlainText())
                self.assertFalse(window.remember_secrets.isChecked())
                target = os.environ.get('RESTOREPOINT_UI_ARTIFACT_DIR')
                if target:
                    folder = Path(target)
                    folder.mkdir(parents=True, exist_ok=True)
                    window.settings_scroll.ensureWidgetVisible(window.setup_key_button, 0, 10)
                    window.grab().save(str(folder / 'one-click-key-settings.png'))
                    dialog = KeySetupDialog(Profile(name='Ubuntu 测试机', host='fixture.invalid'), temp, window)
                    dialog.show()
                    self.app.processEvents()
                    dialog.grab().save(str(folder / 'one-click-key-options.png'))
                    dialog.close()
            finally:
                if window.job:
                    window.job.wait(3000)
                    self.app.processEvents()
                window.close()


if __name__ == '__main__':
    unittest.main()
