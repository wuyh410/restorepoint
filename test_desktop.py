import contextlib
from dataclasses import asdict
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from desktop import backend, bundles
from desktop.models import Profile, SettingsStore, import_legacy


NAME = '20261115T080000.123456Z-1234abcd'


class Context:
    def __init__(self):
        self.messages, self.updates, self.questions = [], [], []
        self.cancel = False

    def check(self):
        if self.cancel:
            raise backend.Cancelled()

    def log(self, message):
        self.messages.append(message)

    def progress(self, value, text):
        self.updates.append((value, text))

    def ask(self, kind, data):
        self.questions.append((kind, data))
        return 'temporary-password'


def make_bundle(parent):
    directory = parent / NAME
    directory.mkdir()
    files = {}
    for name in ('system.tar.gz', 'home.tar.gz'):
        payload = (name + '\n').encode() * 100
        (directory / name).write_bytes(payload)
        files[name] = {'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
    (directory / 'manifest.json').write_text(json.dumps({'schema': 1, 'kind': 'portable-linux-backup', 'id': NAME,
        'created_utc': '2026-11-15T08:00:00+00:00', 'host': 'fixture-linux', 'files': files}), encoding='utf-8')
    return directory


class DesktopBackendTest(unittest.TestCase):
    def test_profiles_roundtrip_and_legacy_import_without_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SettingsStore(temp)
            profile = Profile(name='中文配置', host='fixture', storage=str(Path(temp) / 'backups'))
            store.save([profile])
            self.assertEqual(asdict(store.load()[0]), asdict(profile))
            self.assertNotIn('password', store.path.read_text())
            legacy = import_legacy({'SshTarget': 'tester@example.test', 'BackupRoot': '/tmp/archive', 'MachineName': 'vm'})
            self.assertEqual((legacy.host, legacy.username), ('example.test', 'tester'))
            self.assertEqual(Path(legacy.storage).as_posix(), '/tmp/archive/vm')

    def test_verify_and_catalog_are_offline_and_detect_damage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = make_bundle(root)
            profile = Profile(storage=temp)
            service = backend.RemoteService(profile, Context(), root)
            with mock.patch.object(backend, 'RemoteConnection', side_effect=AssertionError('unexpected SSH')):
                self.assertEqual(len(service.run('catalog')), 1)
                self.assertEqual(service.run('verify', NAME)['id'], NAME)
            (directory / 'system.tar.gz').write_bytes(b'damaged')
            self.assertIn('error', bundles.catalog(root)[0])
            with self.assertRaises(ValueError):
                bundles.verify(directory, Context())

    def test_download_publishes_only_after_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = make_bundle(root)
            storage = root / 'downloaded'
            profile = Profile(host='fixture', storage=str(storage))
            service = backend.RemoteService(profile, Context(), root)
            class Sftp:
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def get(self, source, destination, callback=None):
                    shutil.copyfile(original / source.rsplit('/', 1)[-1], destination)
                    if callback:
                        size = Path(destination).stat().st_size
                        callback(size, size)
            connection = mock.Mock()
            connection.client.open_sftp.return_value = Sftp()
            with mock.patch.object(shutil, 'disk_usage', return_value=mock.Mock(free=100 * 1024 ** 3)):
                self.assertEqual(service.fetch(connection, NAME)['id'], NAME)
            self.assertTrue((storage / NAME).exists())
            self.assertFalse(list(storage.glob('.incoming-*')))
            with self.assertRaises(ValueError):
                service.fetch(connection, '../../outside')

    def test_corrupt_download_is_not_a_completed_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = make_bundle(root)
            profile = Profile(host='fixture', storage=str(root / 'downloaded'))
            class Sftp:
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def get(self, source, destination, callback=None):
                    name = source.rsplit('/', 1)[-1]
                    shutil.copyfile(original / name, destination)
                    if name == 'home.tar.gz':
                        Path(destination).write_bytes(b'broken')
            connection = mock.Mock()
            connection.client.open_sftp.return_value = Sftp()
            with mock.patch.object(shutil, 'disk_usage', return_value=mock.Mock(free=100 * 1024 ** 3)):
                with self.assertRaises(ValueError):
                    backend.RemoteService(profile, Context(), root).fetch(connection, NAME)
            self.assertFalse((Path(profile.storage) / NAME).exists())
            self.assertEqual(len(list(Path(profile.storage).glob('.incoming-*'))), 1)

    def test_remote_sudo_prompt_and_shell_arguments(self):
        class Channel:
            def __init__(self):
                self.chunks = [b'ready\n__RESTORE', b'POINT_SUDO__', b'completed\n']
                self.sent = []
            def get_pty(self, **kwargs): self.pty = True
            def set_combine_stderr(self, flag): pass
            def exec_command(self, command): self.command = command
            def recv_ready(self): return bool(self.chunks)
            def recv(self, size): return self.chunks.pop(0)
            def exit_status_ready(self): return not self.chunks
            def recv_exit_status(self): return 0
            def sendall(self, payload): self.sent.append(payload)
            def close(self): pass
        channel = Channel()
        client = mock.Mock()
        client.get_transport.return_value.open_session.return_value = channel
        context = Context()
        connection = backend.RemoteConnection(Profile(), context, Path('/tmp'))
        connection.username, connection.client = 'tester', client
        connection.run(['restore', '--relative', "user/a';touch /tmp/unwanted;'"] , root=True)
        self.assertEqual(channel.sent, [b'temporary-password\n'])
        self.assertNotIn('temporary-password', channel.command)
        self.assertNotIn('temporary-password', ''.join(context.messages))
        self.assertIn('stty -echo', channel.command)
        self.assertIn("'\"'\"'", channel.command)

    @unittest.skipUnless(sys.platform == 'linux', 'Linux engine only')
    def test_engine_command_and_json_bridge(self):
        command = backend.engine_command('gui_bridge', ['doctor'])
        output = backend.run_local(command, Context())
        self.assertIn('hostname', backend.parse_result(output))
        with self.assertRaises(RuntimeError):
            backend.parse_result('not json')

    @unittest.skipUnless(sys.platform == 'linux', 'POSIX subprocess cancellation')
    def test_running_process_can_be_cancelled(self):
        context = Context()
        def cancel_on_output(message):
            context.messages.append(message)
            if 'READY' in message:
                context.cancel = True
        context.log = cancel_on_output
        with self.assertRaises(backend.Cancelled):
            backend.run_local([sys.executable, '-u', '-c', "import time; print('READY', flush=True); time.sleep(20)"], context)


QT_AVAILABLE = importlib.util.find_spec('PySide6') is not None


@unittest.skipUnless(QT_AVAILABLE, 'Qt dependencies are not installed')
class DesktopUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        from desktop.window import STYLE
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(STYLE)

    def test_six_pages_settings_save_and_preview_invalidation(self):
        from desktop.jobs import Job
        from desktop.window import MainWindow
        with tempfile.TemporaryDirectory() as temp:
            store = SettingsStore(temp)
            storage = Path(temp) / 'versions'
            storage.mkdir()
            make_bundle(storage)
            store.save([Profile(name='UI fixture', storage=str(storage), host='fixture')])
            with mock.patch.object(Job, 'start') as start:
                window = MainWindow(temp)
                window.show()
                self.app.processEvents()
                self.assertEqual(window.pages.count(), 6)
                self.assertEqual(window.table.rowCount(), 1)
                self.assertFalse(window.apply_button.isEnabled())
                for page in range(6):
                    window.navigation.setCurrentRow(page)
                    self.app.processEvents()
                    self.assertEqual(window.pages.currentIndex(), page)
                window.fields['name'].setText('更新后的配置')
                window.save_profile()
                self.assertEqual(store.load()[0].name, '更新后的配置')
                window.preview_signature = window.signature()
                window.apply_button.setEnabled(True)
                window.restore_target.setText('/tmp/another-target')
                self.assertIsNone(window.preview_signature)
                self.assertFalse(window.apply_button.isEnabled())
                start.assert_not_called()
                window.close()
                self.app.processEvents()

    def test_worker_redacts_authentication_from_logs_and_failures(self):
        from desktop.jobs import Job
        job = Job(Profile(), 'plan', '/tmp')
        messages, errors = [], []
        job.message.connect(messages.append)
        job.failed.connect(errors.append)
        def answer(request):
            request.answer = 'secret-test-password'
            request.event.set()
        job.question.connect(answer)
        def operation(*args, **kwargs):
            secret = job.ask('password', {'title': 'fixture', 'text': 'fixture'})
            job.log('fixture ' + secret)
            raise RuntimeError('failed ' + secret)
        with mock.patch('desktop.jobs.RemoteService') as service:
            service.return_value.run.side_effect = operation
            job.run()
        self.assertTrue(errors)
        self.assertNotIn('secret-test-password', ''.join(messages + errors))
        self.assertEqual(job.secrets, [])

    def test_worker_runs_offline_catalog_without_blocking_ui(self):
        from PySide6.QtCore import QEventLoop, QTimer
        from PySide6.QtTest import QSignalSpy
        from desktop.jobs import Job
        with tempfile.TemporaryDirectory() as temp:
            job = Job(Profile(storage=temp), 'catalog', temp)
            spy = QSignalSpy(job.succeeded)
            loop = QEventLoop()
            job.finished.connect(loop.quit)
            timer = QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(loop.quit)
            timer.start(3000)
            job.start()
            loop.exec()
            timer.stop()
            self.assertTrue(job.wait(3000))
            self.assertEqual(spy.count(), 1)


if __name__ == '__main__':
    unittest.main()
