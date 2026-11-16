import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from desktop import bundles, scopes
from desktop.models import Profile, SettingsStore
from test_desktop import Context, NAME

if sys.platform == 'linux':
    import snapshot
    import portable


@unittest.skipUnless(sys.platform == 'linux', 'Linux backup engines')
class ScopedBackupTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = self.root / 'exports'
        self.repository.mkdir()
        self.sources = {}
        for area in ('system', 'config', 'software', 'home'):
            source = self.root / ('source-' + area)
            source.mkdir()
            self.sources[area] = source
        for area in ('system', 'software'):
            for name in ('etc/app.conf', 'usr/bin/application', 'opt/application/program', 'var/lib/app/data', 'home/skip', 'boot/kernel'):
                path = self.sources[area] / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name)
        (self.sources['config'] / 'app.conf').write_text('setting=before')
        (self.sources['home'] / 'notes').write_text('document')
        def capture(directory):
            (directory / 'packages.tsv').write_text('fixture-app\t1.0\tamd64\tii \n')
            snapshot.write_json(directory / 'os-release.json', snapshot.system_identity())
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(snapshot, 'capture_metadata', side_effect=capture).start()
        mock.patch.object(snapshot.os, 'sync').start()
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def export(self, selected, mode='files'):
        patterns = snapshot.exclusions(self.repository, mode)
        patterns['software'] = snapshot.software_exclusions(mode, self.sources['software'])
        source = {area: self.sources[area] for area in selected}
        bundle = portable.export_bundle(self.repository, source, patterns, 1024 * 1024, 0,
                                        os.getuid(), os.getgid(), software_mode=mode)
        prepared = self.root / ('prepared-' + bundle.name)
        portable.unpack_bundle(bundle, prepared, 0)
        snapshot.verify(prepared)
        return bundle, prepared

    def test_selected_ranges_roundtrip_without_unselected_archives(self):
        for selected in (('home',), ('config',), ('software',), ('system',), ('config', 'home')):
            with self.subTest(selected=selected):
                bundle, prepared = self.export(selected)
                data = bundles.manifest(bundle)
                self.assertEqual(data['schema'], 2)
                self.assertEqual(set(data['areas']), set(selected))
                self.assertEqual({name[:-7] for name in data['files'] if name.endswith('.tar.gz')}, set(selected))
                self.assertEqual(set(snapshot.snapshot_areas(json.loads((prepared / 'snapshot.json').read_text()))), set(selected))
                self.assertEqual({area for area in snapshot.AREAS if (prepared / area).exists()}, set(selected))
                target = self.root / ('target-' + bundle.name)
                target.mkdir()
                if 'home' in selected or 'config' in selected:
                    area = 'home' if 'home' in selected else 'config'
                    subprocess.run(snapshot.restore_command(prepared, area, target, apply=True), check=True, capture_output=True)
                    self.assertTrue((target / ('notes' if area == 'home' else 'app.conf')).is_file())
                if 'system' not in selected:
                    with self.assertRaises(RuntimeError):
                        snapshot.restore_command(prepared, 'system', target, apply=True)

    def test_software_recipe_skips_programs_while_file_mode_keeps_them(self):
        for mode in ('reinstall', 'files'):
            bundle, prepared = self.export(('software',), mode)
            self.assertEqual((prepared / 'software/etc/app.conf').read_text(), 'etc/app.conf')
            self.assertEqual((prepared / 'software/var/lib/app/data').read_text(), 'var/lib/app/data')
            self.assertEqual((prepared / 'software/usr/bin/application').exists(), mode == 'files')
            self.assertEqual((prepared / 'software/opt/application/program').exists(), mode == 'files')
            self.assertFalse((prepared / 'software/home').exists())
            self.assertFalse((prepared / 'software/boot').exists())
            self.assertTrue((prepared / 'packages.tsv').exists())
            self.assertTrue((prepared / 'os-release.json').exists())
            self.assertIn('software', dict((value, title) for title, value in scopes.restorable(bundles.manifest(bundle))))

    def test_local_generations_can_switch_ranges_and_reuse_matching_areas(self):
        repository = self.root / 'snapshots'
        repository.mkdir()
        patterns = snapshot.exclusions(repository)
        one = snapshot.create_snapshot(repository, {'home': self.sources['home']}, patterns, None, 0)
        two = snapshot.create_snapshot(repository, {'config': self.sources['config'], 'home': self.sources['home']}, patterns, one, 0)
        snapshot.verify(one)
        snapshot.verify(two)
        self.assertEqual((one / 'home/notes').stat().st_ino, (two / 'home/notes').stat().st_ino)
        self.assertTrue((two / 'config/app.conf').is_file())
        self.assertFalse((two / 'system').exists())

    def test_inventory_install_is_simulated_before_apply_and_never_removes_packages(self):
        _, prepared = self.export(('software',), 'reinstall')
        planned = snapshot.software_install_command(prepared)
        self.assertIn('--simulate', planned)
        self.assertIn('--no-remove', planned)
        self.assertIn('fixture-app=1.0', planned)
        with mock.patch.object(snapshot.os, 'geteuid', return_value=0), mock.patch.object(snapshot, 'run') as run:
            snapshot.restore_tree(prepared, 'packages', Path('/'), apply=False)
            self.assertEqual(run.call_count, 1)
            self.assertIn('--simulate', run.call_args.args[0])
            run.reset_mock()
            snapshot.restore_tree(prepared, 'packages', Path('/'), apply=True)
            self.assertEqual(run.call_count, 2)
            self.assertIn('--simulate', run.call_args_list[0].args[0])
            self.assertNotIn('--simulate', run.call_args_list[1].args[0])
            self.assertIn('--no-remove', run.call_args_list[1].args[0])
            self.assertEqual(run.call_args_list[1].kwargs['env']['DEBIAN_FRONTEND'], 'noninteractive')
            run.reset_mock()
            run.side_effect = RuntimeError('version unavailable')
            with self.assertRaisesRegex(RuntimeError, 'version unavailable'):
                snapshot.restore_tree(prepared, 'packages', Path('/'), apply=True)
            self.assertEqual(run.call_count, 1)

    def test_software_inventory_corruption_or_os_mismatch_is_rejected(self):
        _, prepared = self.export(('software',), 'reinstall')
        inventory = prepared / 'packages.tsv'
        original = inventory.read_text()
        inventory.write_text('corrupt')
        with self.assertRaisesRegex(RuntimeError, '元数据校验失败'):
            snapshot.verify(prepared)
        inventory.write_text(original)
        with mock.patch.object(snapshot, 'system_identity', return_value={'ID': 'different', 'VERSION_ID': '0', 'machine': 'other'}):
            with self.assertRaisesRegex(RuntimeError, '相同'):
                snapshot.software_install_command(prepared)
        inventory.write_text('bad;command\t1.0\tamd64\tii \n')
        with self.assertRaisesRegex(RuntimeError, '非法'):
            snapshot.software_install_command(prepared)

    def test_missing_scope_manifest_and_undeclared_archives_are_rejected(self):
        bundle, _ = self.export(('home',))
        path = bundle / 'manifest.json'
        data = json.loads(path.read_text())
        data['areas'] = []
        path.write_text(json.dumps(data))
        with self.assertRaises((RuntimeError, ValueError)):
            portable.load_manifest(bundle)
        with self.assertRaises(ValueError):
            bundles.manifest(bundle)


class ScopedUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        from desktop.window import apply_theme
        cls.app = QApplication.instance() or QApplication([])
        apply_theme(cls.app)

    def test_creation_dialog_offers_independent_types_and_both_software_modes(self):
        from desktop.backup_dialog import BackupDialog
        with tempfile.TemporaryDirectory() as temp:
            dialog = BackupDialog(Profile())
            self.assertEqual(set(dialog.selected()), {'system', 'home'})
            dialog.preset.setCurrentIndex(dialog.preset.findData('software'))
            self.assertEqual(dialog.selected(), ('software',))
            self.assertEqual(dialog.mode(), 'reinstall')
            dialog.software_mode.setCurrentIndex(dialog.software_mode.findData('files'))
            self.assertEqual(dialog.mode(), 'files')
            dialog.preset.setCurrentIndex(dialog.preset.findData('home'))
            self.assertEqual(dialog.selected(), ('home',))
            dialog.preset.setCurrentIndex(dialog.preset.findData('system'))
            self.assertEqual(dialog.selected(), ('system',))
            self.assertFalse(dialog.software_mode.isEnabled())
            dialog.preset.setCurrentIndex(dialog.preset.findData('software'))
            dialog.show()
            self.app.processEvents()
            output = os.environ.get('RESTOREPOINT_UI_ARTIFACT_DIR')
            if output:
                Path(output).mkdir(parents=True, exist_ok=True)
                dialog.grab().save(str(Path(output) / 'backup-types.png'))
            dialog.close()

    def test_restore_choices_match_the_selected_bundle(self):
        from desktop.window import MainWindow
        with tempfile.TemporaryDirectory() as temp:
            SettingsStore(temp).save([Profile(host='fixture.invalid')])
            window = MainWindow(temp)
            try:
                metadata = {'schema': 2, 'areas': ['software'], 'software_mode': 'reinstall', 'files': {'software.tar.gz': {}, 'packages.tsv': {}, 'os-release.json': {}}, 'excludes': {'software': []}}
                window.rows = [{'id': NAME, 'date': '', 'host': 'fixture', 'kind': '软件', 'bytes': 1, 'metadata': metadata}]
                window.render_rows()
                window.restore_version.setCurrentText(NAME)
                values = [window.restore_area.itemData(i) for i in range(window.restore_area.count())]
                self.assertEqual(values, ['software', 'packages'])
                window.restore_area.setCurrentIndex(window.restore_area.findData('packages'))
                self.assertEqual(window.restore_target.text(), '/')
                self.assertFalse(window.restore_target.isEnabled())
                self.assertFalse(window.restore_relative.isEnabled())
                window.rows[0]['metadata'] = {'schema': 2, 'areas': ['home']}
                window.update_restore_areas()
                self.assertEqual(window.restore_area.count(), 1)
                self.assertEqual(window.restore_area.currentData(), 'home')
            finally:
                window.close()

    def test_old_helper_is_rejected_before_a_partial_backup(self):
        from desktop import backend
        context = Context()
        profile = Profile(host='fixture.invalid')
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(backend, 'RemoteConnection') as connection:
            active = connection.return_value.__enter__.return_value
            active.username = 'fixture'
            active.run.return_value = 'RP_RESULT={"supported": false}'
            with self.assertRaisesRegex(ValueError, '更新测试机组件'):
                backend.RemoteService(profile, context, temp).run('backup', areas=['home'])
            self.assertEqual(active.run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
