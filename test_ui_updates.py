import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from desktop import backend
from desktop.models import Profile, SettingsStore
from desktop.terminal import LogLines, clean_log_text


class TerminalLogsTest(unittest.TestCase):
    def test_sudo_feedback_and_control_pictures_are_removed(self):
        for backspace in ('\b', '\u2408'):
            feedback = '******' + (backspace + ' ' + backspace) * 6
            text = '00:13:35  [sudo: __RESTOREPOINT_SUDO__] 密码： ' + feedback + '\r\n00:16:10  实际恢复'
            self.assertEqual(clean_log_text(text), '00:16:10  实际恢复')

    def test_terminal_edits_colors_and_real_errors_remain_readable(self):
        self.assertEqual(clean_log_text('\x1b[31mlong progress\rready\x1b[K\x1b[0m\nabc\b \b'), 'ready\nab')
        self.assertEqual(clean_log_text('sudo: authentication failed'), 'sudo: authentication failed')
        self.assertEqual(clean_log_text('[sudo] password for tester: RP_RESULT={"ok": true}'), 'RP_RESULT={"ok": true}')

    def test_fragmented_lines_are_assembled_before_cleaning(self):
        lines = LogLines()
        result = []
        for chunk in ['\x1b[3', '1mstart\x1b[0m\n[sudo: __RESTORE', 'POINT_SUDO__] 密码： **', '\b \b\b \b\nRP_RESULT={"ok":true}']:
            result += lines.feed(chunk)
        result += lines.finish()
        self.assertEqual(result, ['start', 'RP_RESULT={"ok":true}'])

    def test_ssh_prompt_split_across_packets_does_not_pollute_results(self):
        class Channel:
            def __init__(self):
                self.chunks = [b'ready\n[sudo: __RESTORE', 'POINT_SUDO__] 密码： '.encode(),
                               b'******' + b'\b \b' * 6 + b'\r\nRP_RESULT={"ok":true}\n']
                self.sent = []
            def get_pty(self, **unused): pass
            def set_combine_stderr(self, unused): pass
            def exec_command(self, unused): pass
            def recv_ready(self): return bool(self.chunks)
            def recv(self, unused): return self.chunks.pop(0)
            def exit_status_ready(self): return not self.chunks
            def recv_exit_status(self): return 0
            def sendall(self, data): self.sent.append(data)
            def close(self): pass
        from test_desktop import Context
        channel = Channel()
        context = Context()
        connection = backend.RemoteConnection(Profile(), context, Path('/tmp'))
        connection.username = 'fixture'
        connection.client = mock.Mock()
        connection.client.get_transport.return_value.open_session.return_value = channel
        output = connection.run(['plan'], root=True)
        self.assertEqual(backend.parse_result(output), {'ok': True})
        self.assertEqual(channel.sent, [b'temporary-password\n'])
        self.assertEqual(context.messages, ['ready', '验证测试机管理员权限'])
        self.assertNotIn('\b', output)


class WorkspaceUpdatesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        from desktop.window import apply_theme
        cls.app = QApplication.instance() or QApplication([])
        apply_theme(cls.app)

    def artifact(self, name, widget):
        target = os.environ.get('RESTOREPOINT_UI_ARTIFACT_DIR')
        if target:
            directory = Path(target)
            directory.mkdir(parents=True, exist_ok=True)
            self.assertTrue(widget.grab().save(str(directory / name)))

    def wheel(self, widget, steps=-120):
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        position = widget.rect().center()
        event = QWheelEvent(QPointF(position), QPointF(widget.mapToGlobal(position)), QPoint(), QPoint(0, steps),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        self.app.sendEvent(widget, event)
        self.app.processEvents()

    def test_wheel_never_edits_values_and_keeps_the_form_scrollable(self):
        from desktop.window import MainWindow
        with tempfile.TemporaryDirectory() as temp:
            SettingsStore(temp).save([Profile(name='First'), Profile(name='Second')])
            window = MainWindow(temp)
            window.show()
            window.show_page(3)
            self.app.processEvents()
            try:
                for widget in (window.port, window.reserve, window.mode, window.auth_method, window.profile_combo, window.restore_area, window.restore_version):
                    for focused in (False, True):
                        widget.setFocus() if focused else widget.clearFocus()
                        before = widget.value() if hasattr(widget, 'value') else widget.currentIndex()
                        self.wheel(widget)
                        self.assertEqual(widget.value() if hasattr(widget, 'value') else widget.currentIndex(), before)
                window.settings_scroll.verticalScrollBar().setValue(0)
                self.wheel(window.port.lineEdit())
                self.assertEqual(window.port.value(), 22)
                window.settings_scroll.verticalScrollBar().setValue(0)
                self.wheel(window.reserve)
                self.assertGreater(window.settings_scroll.verticalScrollBar().value(), 0)
                for widget in (window.remember_secrets, window.sudo_uses_login):
                    before = widget.isChecked()
                    self.wheel(widget)
                    self.assertEqual(widget.isChecked(), before)
            finally:
                window.close()
                self.app.processEvents()

    def test_restore_popup_rows_are_separate_and_open_outside_the_control(self):
        from PySide6.QtCore import QPoint, QRect
        from desktop.window import MainWindow
        with tempfile.TemporaryDirectory() as temp:
            window = MainWindow(temp)
            window.show()
            window.show_page(2)
            self.app.processEvents()
            combo = window.restore_area
            try:
                combo.showPopup()
                self.app.processEvents()
                view = combo.view()
                first, second = (view.visualRect(combo.model().index(row, 0)) for row in (0, 1))
                self.assertGreaterEqual(first.height(), 36)
                self.assertGreater(second.top(), first.bottom())
                self.assertFalse(view.window().geometry().intersects(QRect(combo.mapToGlobal(QPoint()), combo.size())))
                self.artifact('restore-scope-popup.png', view.window())
                combo.hidePopup()
                self.artifact('restore-controls.png', window)
            finally:
                window.close()
                self.app.processEvents()

    def test_shared_logs_follow_the_running_task_across_pages_and_history(self):
        from PySide6.QtTest import QTest
        from desktop.window import MainWindow
        from test_desktop import NAME
        gate = threading.Event()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            SettingsStore(temp).save([Profile(name='UI fixture', host='fixture')])
            old_log = root / 'old.log'
            old_log.write_text('00:00:00  [sudo: ] 密码： ***\b \b\b \b\b \b\n00:00:01  实际恢复\n', encoding='utf-8')
            (root / 'history.json').write_text(json.dumps([{'time': '2026-11-16T00:00:00', 'profile': 'Old fixture', 'profile_id': 'old', 'action': '恢复预览', 'status': '已完成', 'log': str(old_log)}]), encoding='utf-8')
            window = MainWindow(temp)
            window.show()
            def run(*args, **kwargs):
                window.job.log('current task output')
                window.job.progress(37, '正在上传测试包')
                gate.wait(5)
                return 'done'
            try:
                with mock.patch('desktop.jobs.RemoteService') as service:
                    service.return_value.run.side_effect = run
                    window.start('upload', snapshot=NAME)
                    job = window.job
                    for _ in range(100):
                        QTest.qWait(10)
                        if window.progress.value() == 37:
                            break
                    self.assertEqual(window.progress.value(), 37)
                    self.assertIn('上传备份', window.task_panel.title.text())
                    self.assertIn('UI fixture', window.task_panel.title.text())
                    self.assertNotIn('任务日志', [window.navigation.item(i).text() for i in range(window.navigation.count())])
                    for page in range(window.pages.count()):
                        window.show_page(page)
                        self.app.processEvents()
                        self.assertTrue(window.console.isVisible())
                        self.assertIn('current task output', window.console.toPlainText())
                    window.show_page(0)
                    self.app.processEvents()
                    self.artifact('shared-task-panel-overview.png', window)
                    window.show_page(3)
                    self.app.processEvents()
                    self.artifact('shared-task-panel-settings.png', window)
                    window.show_history()
                    self.app.processEvents()
                    self.assertIn('执行恢复', window.history_caption.text())
                    self.assertNotIn('[sudo', window.history_console.toPlainText())
                    self.assertIn('current task output', window.console.toPlainText())
                    self.assertEqual(window.progress.value(), 37)
                    window.history_dialog.close()
                    gate.set()
                    self.assertTrue(job.wait(3000))
                    self.app.processEvents()
                    self.assertEqual(window.progress.value(), 100)
                    self.assertEqual(window.task_panel.outcome.text(), '已完成')
                    self.assertEqual(window.history[-1]['action'], '上传备份')
            finally:
                gate.set()
                if window.job:
                    window.job.wait(3000)
                    self.app.processEvents()
                window.close()
                self.app.processEvents()

    def test_failure_keeps_the_page_and_progress_does_not_report_completion(self):
        from PySide6.QtWidgets import QMessageBox
        from desktop.window import MainWindow, action_title
        with tempfile.TemporaryDirectory() as temp:
            SettingsStore(temp).save([Profile(host='fixture')])
            window = MainWindow(temp)
            window.show()
            window.show_page(3)
            with mock.patch('desktop.jobs.RemoteService') as service, mock.patch.object(QMessageBox, 'warning'):
                service.return_value.run.side_effect = RuntimeError('\x1b[31mfixture failure\x1b[0m')
                window.start('plan')
                self.assertTrue(window.job.wait(3000))
                self.app.processEvents()
            self.assertEqual(window.pages.currentIndex(), 3)
            self.assertEqual(window.task_panel.outcome.text(), '失败')
            self.assertLess(window.progress.value(), 100)
            self.assertNotIn('\x1b', window.console.toPlainText())
            self.assertEqual(action_title('restore', {'apply': True}), '执行恢复')
            self.assertEqual(action_title('restore', {'apply': False}), '恢复预览')
            window.close()
            self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
