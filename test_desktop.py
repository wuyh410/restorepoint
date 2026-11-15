import contextlib
from dataclasses import asdict
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
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
            self.assertNotIn('password', store.path.read_text(encoding='utf-8'))
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


@contextlib.contextmanager
def password_only_ssh():
    """An isolated loopback SSH server; it cannot execute commands or use real accounts."""
    import paramiko
    class PasswordOnly(paramiko.ServerInterface):
        def get_allowed_auths(self, username):
            return 'password'

        def check_auth_password(self, username, password):
            return paramiko.AUTH_SUCCESSFUL if (username, password) == ('fixture', 'temporary-password') else paramiko.AUTH_FAILED

    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(2)
    listener.settimeout(0.1)
    port = listener.getsockname()[1]
    host_key = paramiko.RSAKey.generate(2048)
    stopped, transports, errors = threading.Event(), [], []
    def serve():
        while not stopped.is_set():
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            transport = paramiko.Transport(client)
            transports.append(transport)
            try:
                transport.add_server_key(host_key)
                transport.start_server(server=PasswordOnly())
                while transport.is_active() and not stopped.wait(0.01):
                    pass
            except EOFError:
                # The initial keyless client may close immediately after KEX.
                pass
            except Exception as exc:
                if not stopped.is_set():
                    errors.append(exc)
            finally:
                transport.close()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        stopped.set()
        listener.close()
        for transport in transports:
            transport.close()
        thread.join(5)
        if thread.is_alive():
            raise RuntimeError('Fixture SSH server did not stop')
        if errors:
            raise errors[0]


class SshAuthenticationTest(unittest.TestCase):
    def login(self, context):
        import paramiko
        with tempfile.TemporaryDirectory() as temp, password_only_ssh() as port:
            root = Path(temp)
            # Both HOME and USERPROFILE are set so default-key discovery is
            # isolated on Linux and Windows, without accessing the user's agent.
            with mock.patch.dict(os.environ, {'HOME': temp, 'USERPROFILE': temp}), mock.patch('paramiko.client.Agent') as agent:
                agent.return_value.get_keys.return_value = []
                profile = Profile(host='127.0.0.1', port=port, username='fixture')
                with backend.RemoteConnection(profile, context, root) as connection:
                    self.assertTrue(connection.client.get_transport().is_authenticated())

    def test_clean_client_prompts_for_password_and_authenticates_over_ssh(self):
        context = Context()
        self.login(context)
        self.assertEqual([kind for kind, _ in context.questions], ['hostkey', 'password'])
        self.assertNotIn('temporary-password', ''.join(context.messages))

    def test_wrong_password_stops_after_one_prompt(self):
        context = Context()
        def answer(kind, data):
            context.questions.append((kind, data))
            return True if kind == 'hostkey' else 'wrong-password'
        context.ask = answer
        with self.assertRaisesRegex(RuntimeError, 'SSH 认证失败'):
            self.login(context)
        self.assertEqual([kind for kind, _ in context.questions], ['hostkey', 'password'])
        self.assertNotIn('wrong-password', ''.join(context.messages))

    def test_password_prompt_can_be_cancelled(self):
        context = Context()
        def answer(kind, data):
            context.questions.append((kind, data))
            if kind == 'password':
                raise backend.Cancelled('fixture cancelled')
            return True
        context.ask = answer
        with self.assertRaises(backend.Cancelled):
            self.login(context)
        self.assertEqual([kind for kind, _ in context.questions], ['hostkey', 'password'])

    def test_other_ssh_errors_do_not_prompt_for_password(self):
        import paramiko
        context = Context()
        with tempfile.TemporaryDirectory() as temp, mock.patch('paramiko.SSHClient') as factory:
            failure = paramiko.SSHException('Error reading SSH protocol banner')
            factory.return_value.connect.side_effect = failure
            with self.assertRaises(paramiko.SSHException) as caught:
                with backend.RemoteConnection(Profile(host='fixture', username='fixture'), context, Path(temp)):
                    self.fail('Connection unexpectedly succeeded')
            self.assertIs(caught.exception, failure)
            self.assertEqual(context.questions, [])
            factory.return_value.close.assert_called_once()


QT_AVAILABLE = importlib.util.find_spec('PySide6') is not None


@unittest.skipUnless(QT_AVAILABLE, 'Qt dependencies are not installed')
class DesktopUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        from desktop.window import apply_theme
        cls.app = QApplication.instance() or QApplication([])
        apply_theme(cls.app)

    def save_artifact(self, name, widget):
        directory = os.environ.get('RESTOREPOINT_UI_ARTIFACT_DIR')
        if directory:
            path = Path(directory)
            path.mkdir(parents=True, exist_ok=True)
            self.assertTrue(widget.grab().save(str(path / name)))

    def test_last_profile_can_be_deleted_and_stays_empty_after_restart(self):
        from PySide6.QtWidgets import QMessageBox
        from desktop.jobs import Job
        from desktop.window import MainWindow
        with tempfile.TemporaryDirectory() as temp:
            store = SettingsStore(temp)
            storage = Path(temp) / 'preserved-backups'
            storage.mkdir()
            marker = storage / 'existing-data.txt'
            marker.write_text('preserve this backup')
            store.save([Profile(name='最后一个配置', storage=str(storage), host='fixture')])
            with mock.patch.object(Job, 'start') as start:
                window = MainWindow(temp)
                window.show()
                window.navigation.setCurrentRow(3)
                self.app.processEvents()
                with mock.patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No):
                    window.delete_profile()
                self.assertEqual(len(store.load()), 1)
                window.preview_signature = window.signature()
                window.apply_button.setEnabled(True)
                with mock.patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
                    window.delete_profile()
                self.app.processEvents()
                self.assertEqual(store.load(), [])
                self.assertEqual(window.profiles, [])
                self.assertEqual(window.profile_combo.count(), 0)
                self.assertEqual(window.profile_combo.currentIndex(), -1)
                self.assertTrue(window.settings_empty.isVisible())
                self.assertFalse(window.settings_scroll.isVisible())
                self.assertLess(window.pages.widget(3).layout().itemAt(0).widget().height(), 50)
                self.assertIsNone(window.preview_signature)
                self.assertFalse(window.apply_button.isEnabled())
                self.assertEqual(window.table.rowCount(), 0)
                self.assertEqual(marker.read_text(), 'preserve this backup')
                for button in window.buttons:
                    self.assertEqual(button.isEnabled(), not button.property('requiresProfile'), button.text())
                self.save_artifact('empty-configurations.png', window)
                window.close()
                self.app.processEvents()
                window = MainWindow(temp)
                self.assertEqual(window.profiles, [])
                for page in range(6):
                    window.navigation.setCurrentRow(page)
                    self.app.processEvents()
                window.add_profile()
                self.app.processEvents()
                self.assertEqual(window.profile_combo.currentText(), '新测试机')
                self.assertTrue(window.settings_content.isEnabled())
                self.assertTrue(window.fields['host'].isEnabled())
                window.fields['name'].setText('重新创建的配置')
                window.save_profile()
                self.assertEqual(store.load()[0].name, '重新创建的配置')
                start.assert_not_called()
                window.close()
                self.app.processEvents()

    def test_profile_can_be_imported_into_an_empty_configuration_list(self):
        from PySide6.QtWidgets import QFileDialog
        from desktop.window import MainWindow
        with tempfile.TemporaryDirectory() as temp:
            store = SettingsStore(temp)
            store.save([])
            imported = Profile(name='导入的测试机', host='fixture', port=2222)
            source = Path(temp) / 'profile.json'
            source.write_text(json.dumps(asdict(imported), ensure_ascii=False), encoding='utf-8')
            window = MainWindow(temp)
            with mock.patch.object(QFileDialog, 'getOpenFileName', return_value=(str(source), 'JSON')):
                window.import_profile()
            self.assertEqual(window.profile_combo.currentText(), imported.name)
            self.assertEqual(window.port.value(), 2222)
            self.assertTrue(window.settings_content.isEnabled())
            window.save_profile()
            self.assertEqual(store.load()[0].host, imported.host)
            window.close()
            self.app.processEvents()

    def test_combo_text_popup_and_spin_buttons_work_after_a_dark_system_palette(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QPalette
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QStyle, QStyleOptionComboBox, QStyleOptionSpinBox
        from desktop.window import MainWindow, apply_theme
        dark = QPalette()
        dark.setColor(QPalette.ColorRole.Base, QColor('#202020'))
        dark.setColor(QPalette.ColorRole.Text, QColor('white'))
        dark.setColor(QPalette.ColorRole.ButtonText, QColor('white'))
        self.app.setPalette(dark)
        apply_theme(self.app)
        with tempfile.TemporaryDirectory() as temp:
            SettingsStore(temp).save([Profile(name='First profile'), Profile(name='Second profile')])
            window = MainWindow(temp)
            window.show()
            window.navigation.setCurrentRow(3)
            self.app.processEvents()
            combo = window.profile_combo
            self.assertEqual(combo.currentText(), 'First profile')
            option = QStyleOptionComboBox()
            combo.initStyleOption(option)
            text_rect = combo.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, option, QStyle.SubControl.SC_ComboBoxEditField, combo)
            image = combo.grab().toImage().copy(text_rect)
            pixels = [image.pixelColor(x, y) for x in range(image.width()) for y in range(image.height())]
            self.assertGreater(sum(color.red() > 180 and color.green() > 180 and color.blue() > 180 for color in pixels), 10)
            self.assertGreater(sum(color.red() < 80 and color.green() < 100 for color in pixels), len(pixels) // 3)
            combo.showPopup()
            self.app.processEvents()
            # Qt initially ignores mouse release events to avoid selecting an
            # item with the click that opened the popup. Let that guard expire.
            QTest.qWait(300)
            view = combo.view()
            index = combo.model().index(1, 0)
            rect = view.visualRect(index)
            image = view.viewport().grab().toImage().copy(rect)
            pixels = [image.pixelColor(x, y) for x in range(image.width()) for y in range(image.height())]
            self.assertGreater(sum(color.red() < 100 and color.green() < 120 and color.blue() < 150 for color in pixels), 10)
            self.assertGreater(sum(color.red() > 240 and color.green() > 240 and color.blue() > 240 for color in pixels), len(pixels) // 3)
            self.save_artifact('configuration-popup.png', view.window())
            QTest.mouseMove(view.viewport(), rect.center())
            QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
            self.app.processEvents()
            self.assertEqual(combo.currentText(), 'Second profile')
            window.settings_scroll.ensureWidgetVisible(window.port)
            self.app.processEvents()
            option = QStyleOptionSpinBox()
            window.port.initStyleOption(option)
            up = window.port.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option, QStyle.SubControl.SC_SpinBoxUp, window.port)
            down = window.port.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option, QStyle.SubControl.SC_SpinBoxDown, window.port)
            self.assertGreaterEqual(up.width(), 24)
            self.assertGreaterEqual(down.width(), 24)
            QTest.mouseClick(window.port, Qt.MouseButton.LeftButton, pos=up.center())
            self.assertEqual(window.port.value(), 23)
            QTest.mouseClick(window.port, Qt.MouseButton.LeftButton, pos=down.center())
            self.assertEqual(window.port.value(), 22)
            self.save_artifact('connection-settings.png', window)
            window.close()
            self.app.processEvents()

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
