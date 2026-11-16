from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from desktop.credentials import CredentialError, CredentialStore
from desktop.models import Profile, SettingsStore
from test_desktop import Context, password_only_ssh


class MemoryVault:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[service, account] = value

    def delete_password(self, service, account):
        del self.values[service, account]


class CredentialTest(unittest.TestCase):
    def test_vault_roundtrip_is_scoped_to_connection_and_not_config(self):
        with tempfile.TemporaryDirectory() as temp:
            store = CredentialStore(temp, MemoryVault())
            profile = Profile(host='first.invalid', username='fixture', remember_secrets=True)
            values = {'login': 'fixture-login-secret', 'key': 'fixture-key-secret', 'sudo': 'fixture-sudo-secret'}
            store.save(profile, values)
            self.assertEqual(store.load(profile), values)
            changed = Profile.from_dict(dict(asdict(profile), host='second.invalid'))
            self.assertEqual(store.load(changed), {})
            changed = Profile.from_dict(dict(asdict(profile), username='another-user'))
            self.assertEqual(store.load(changed), {})
            SettingsStore(temp).save([profile])
            for value in values.values():
                self.assertNotIn(value, SettingsStore(temp).path.read_text())
            store.delete(profile)
            self.assertEqual(store.load(profile), {})

    def test_unavailable_vault_never_falls_back_to_files(self):
        with tempfile.TemporaryDirectory() as temp:
            vault = mock.Mock()
            vault.get_password.side_effect = RuntimeError('unavailable')
            vault.set_password.side_effect = RuntimeError('unavailable')
            store = CredentialStore(temp, vault)
            with self.assertRaises(CredentialError):
                store.load(Profile())
            with self.assertRaises(CredentialError):
                store.save(Profile(), {'login': 'fixture-secret'})
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_saved_login_authenticates_without_prompt_and_retries_stale_secret(self):
        from desktop import backend
        from desktop.jobs import Job
        import paramiko
        for initial in ('temporary-password', 'stale-password'):
            with self.subTest(initial=initial), tempfile.TemporaryDirectory() as temp, password_only_ssh() as port:
                profile = Profile(host='127.0.0.1', port=port, username='fixture', auth_method='password')
                job = Job(profile, 'connect', temp, credentials={'login': initial})
                context = Context()
                context.get_credential = job.get_credential
                context.accept_credential = job.accept_credential
                context.ask = Context.ask.__get__(context)
                with mock.patch.dict(os.environ, {'HOME': temp, 'USERPROFILE': temp}), mock.patch('paramiko.client.Agent') as agent:
                    agent.return_value.get_keys.return_value = []
                    with backend.RemoteConnection(profile, context, temp) as connection:
                        self.assertTrue(connection.client.get_transport().is_authenticated())
                prompts = [kind for kind, _ in context.questions]
                self.assertEqual(prompts, ['hostkey'] + (['password'] if initial == 'stale-password' else []))
                self.assertEqual(job.get_credential('login'), 'temporary-password')
                self.assertNotIn(initial, ''.join(context.messages))

    def test_encrypted_key_authentication_does_not_offer_password_fallback(self):
        import paramiko
        from desktop import backend
        with tempfile.TemporaryDirectory() as temp:
            key = paramiko.RSAKey.generate(2048)
            path = Path(temp) / 'fixture-key'
            key.write_private_key_file(str(path), password='fixture-key-passphrase')
            profile = Profile(host='127.0.0.1', username='fixture', identity_file=str(path), auth_method='key')
            context = Context()
            context.get_credential = lambda kind: 'fixture-key-passphrase' if kind == 'key' else None
            with password_only_ssh(accepted_key=key) as port:
                profile.port = port
                with backend.RemoteConnection(profile, context, temp) as connection:
                    self.assertTrue(connection.client.get_transport().is_authenticated())
            self.assertEqual([kind for kind, _ in context.questions], ['hostkey'])

    def test_key_mode_rejects_an_unapproved_key_without_using_saved_login(self):
        import paramiko
        from desktop import backend
        with tempfile.TemporaryDirectory() as temp:
            key = paramiko.RSAKey.generate(2048)
            path = Path(temp) / 'wrong-key'
            key.write_private_key_file(str(path))
            profile = Profile(host='127.0.0.1', username='fixture', identity_file=str(path), auth_method='key')
            context = Context()
            context.get_credential = lambda kind: 'temporary-password' if kind == 'login' else None
            with password_only_ssh(accepted_key=paramiko.RSAKey.generate(2048)) as port:
                profile.port = port
                with self.assertRaisesRegex(RuntimeError, '私钥认证失败'):
                    with backend.RemoteConnection(profile, context, temp):
                        self.fail('Unexpected password fallback')
            self.assertEqual([kind for kind, _ in context.questions], ['hostkey'])

    def test_sudo_can_reuse_login_but_retry_always_prompts(self):
        from desktop.jobs import Job
        job = Job(Profile(sudo_uses_login=True), 'plan', '/tmp', credentials={'login': 'fixture-login'})
        self.assertEqual(job.ask('password', {'credential': 'sudo'}), 'fixture-login')
        def answer(question):
            question.answer = 'corrected-sudo'
            question.event.set()
        job.question.connect(answer)
        self.assertEqual(job.ask('password', {'credential': 'sudo', 'retry': True}), 'corrected-sudo')
        self.assertIn('corrected-sudo', job.secrets)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows Credential Manager')
    def test_native_windows_vault_and_cleanup(self):
        from app import credential_self_test
        self.assertEqual(credential_self_test(), {'ok': True})


class CredentialUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        from desktop.window import apply_theme
        cls.app = QApplication.instance() or QApplication([])
        apply_theme(cls.app)

    def test_workflow_navigation_and_profile_name_elision(self):
        from PySide6.QtWidgets import QStyle, QStyleOptionComboBox
        from desktop.window import MainWindow
        with tempfile.TemporaryDirectory() as temp:
            name = 'Ubuntu 测试机用于验证很长的配置名称完整保留'
            SettingsStore(temp).save([Profile(name=name)])
            window = MainWindow(temp)
            window.show()
            self.app.processEvents()
            try:
                pages = []
                for row in range(window.navigation.count()):
                    window.navigation.setCurrentRow(row)
                    pages.append(window.pages.currentIndex())
                self.assertEqual(pages, [0, 3, 1, 2, 4])
                combo = window.profile_combo
                self.assertEqual(combo.currentText(), name)
                self.assertEqual(combo.toolTip(), name)
                option = QStyleOptionComboBox()
                combo.initStyleOption(option)
                rect = combo.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, option, QStyle.SubControl.SC_ComboBoxEditField, combo)
                self.assertGreater(rect.width(), 130)
                self.assertTrue(combo.display_text(rect.width() - 2).endswith('...'))
                self.assertLessEqual(combo.fontMetrics().horizontalAdvance(combo.display_text(rect.width() - 2)), rect.width())
                window.show_page(3)
                self.app.processEvents()
                window.settings_scroll.ensureWidgetVisible(window.auth_method, 0, 20)
                target = os.environ.get('RESTOREPOINT_UI_ARTIFACT_DIR')
                if target:
                    Path(target).mkdir(exist_ok=True, parents=True)
                    window.grab().save(str(Path(target) / 'authentication-settings.png'))
            finally:
                window.close()

    def test_remember_clear_export_and_endpoint_change(self):
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        from desktop.window import MainWindow
        with tempfile.TemporaryDirectory() as temp:
            profile = Profile(host='fixture.invalid')
            SettingsStore(temp).save([profile])
            window = MainWindow(temp)
            vault = MemoryVault()
            window.credential_store = CredentialStore(temp, vault)
            try:
                window.secret_fields['login'].setText('ui-login-secret')
                window.secret_fields['sudo'].setText('ui-sudo-secret')
                window.remember_secrets.setChecked(True)
                window.save_profile()
                self.assertEqual(window.credential_store.load(profile)['login'], 'ui-login-secret')
                self.assertNotIn('ui-login-secret', window.store.path.read_text())
                exported = Path(temp) / 'export.json'
                with mock.patch.object(QFileDialog, 'getSaveFileName', return_value=(str(exported), '')):
                    window.export_profile()
                self.assertNotIn('ui-login-secret', exported.read_text())
                self.assertNotIn('ui-sudo-secret', exported.read_text())
                window.remember_secrets.setChecked(False)
                window.save_profile()
                self.assertEqual(window.credential_store.load(profile), {})
                window.fields['host'].setText('another.invalid')
                self.assertEqual(window.credential_values(), {})
                self.assertFalse(window.credential_store.load(window.current_profile()))
                window.remember_secrets.setChecked(True)
                window.secret_fields['login'].setText('other-secret')
                window.save_profile()
                with mock.patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
                    window.delete_profile()
                self.assertEqual(vault.values, {})
            finally:
                window.close()

    def test_unsaved_connection_cannot_create_an_orphaned_saved_credential(self):
        from desktop.window import MainWindow
        with tempfile.TemporaryDirectory() as temp:
            profile = Profile(host='fixture.invalid')
            SettingsStore(temp).save([profile])
            window = MainWindow(temp)
            vault = MemoryVault()
            window.credential_store = CredentialStore(temp, vault)
            try:
                window.remember_secrets.setChecked(True)
                window.active_profile = window.current_profile()
                window.remember_credential('login', 'session-only-secret')
                self.assertEqual(vault.values, {})
                self.assertEqual(window.secret_fields['login'].text(), 'session-only-secret')
                window.save_profile()
                self.assertEqual(window.credential_store.load(profile), {'login': 'session-only-secret'})
            finally:
                window.close()


if __name__ == '__main__':
    unittest.main()
