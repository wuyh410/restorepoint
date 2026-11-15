import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import snapshot as backup


class BackupTest(unittest.TestCase):
    def test_history_exclusions_integrity_and_failed_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sources = {area: root / ('source-' + area) for area in ['system', 'home']}
            for source in sources.values():
                source.mkdir()
            repository = root / 'backups'
            repository.mkdir()
            (sources['system'] / 'config').write_text('before')
            (sources['system'] / 'unchanged').write_text('shared')
            preserved = sources['system'] / 'preserved-time'
            preserved.write_text('before')
            original_info = preserved.stat()
            (sources['system'] / 'removed').write_text('old')
            (sources['system'] / 'cache').mkdir()
            (sources['system'] / 'cache' / 'ignored').write_text('skip')
            (sources['home'] / 'notes').write_text('personal')
            (sources['home'] / 'link').symlink_to('notes')
            patterns = {'system': ['/cache/***'], 'home': []}
            with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(backup, 'capture_metadata'), mock.patch.object(backup.os, 'sync'):
                first = backup.create_snapshot(repository, sources, patterns, None, 0)
                backup.verify(first)
                (sources['system'] / 'config').write_text('after-change')
                preserved.write_text('after!')
                os.utime(preserved, ns=(original_info.st_atime_ns, original_info.st_mtime_ns))
                (sources['system'] / 'removed').unlink()
                second = backup.create_snapshot(repository, sources, patterns, first, 0)
                backup.verify(first)
                backup.verify(second)
                self.assertEqual((first / 'system/config').read_text(), 'before')
                self.assertEqual((second / 'system/config').read_text(), 'after-change')
                self.assertEqual((first / 'system/preserved-time').read_text(), 'before')
                self.assertEqual((second / 'system/preserved-time').read_text(), 'after!')
                self.assertEqual((first / 'system/unchanged').stat().st_ino,
                                 (second / 'system/unchanged').stat().st_ino)
                self.assertNotEqual((first / 'system/config').stat().st_ino,
                                    (second / 'system/config').stat().st_ino)
                self.assertTrue((first / 'system/removed').exists())
                self.assertFalse((second / 'system/removed').exists())
                self.assertFalse((second / 'system/cache').exists())
                self.assertEqual((second / 'home/link').readlink(), Path('notes'))
                latest = (repository / 'latest').readlink()
                with mock.patch.object(backup, 'copy_area', side_effect=RuntimeError('injected copy failure')):
                    with self.assertRaises(RuntimeError):
                        backup.create_snapshot(repository, sources, patterns, second, 0)
                self.assertEqual((repository / 'latest').readlink(), latest)
                self.assertEqual(len(backup.completed(repository)), 2)
                self.assertEqual(len(list(repository.glob('.incomplete-*'))), 1)
                (second / 'home/notes').write_text('corrupted')
                with self.assertRaisesRegex(RuntimeError, '校验失败'):
                    backup.verify(second)

    def test_space_estimate_and_sparse_files(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source'
            source.mkdir()
            (source / 'included').write_bytes(b'1234567')
            (source / 'excluded').write_bytes(b'123456789')
            with contextlib.redirect_stdout(io.StringIO()):
                size = backup.estimate({'system': source}, {'system': ['/excluded']}, None)
            self.assertEqual(size, 7)
            sparse = source / 'sparse'
            with sparse.open('wb') as stream:
                stream.seek(16 * 1024 ** 2)
                stream.write(b'end')
            target = Path(temp) / 'target'
            target.mkdir()
            subprocess.run(backup.rsync_command(source, target, []), check=True, capture_output=True)
            self.assertEqual(backup.digest(sparse), backup.digest(target / 'sparse'))
            self.assertLess((target / 'sparse').stat().st_blocks * 512, sparse.stat().st_size)

    def test_copy_stops_on_low_space_and_rsync_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.object(backup, 'available', return_value=0):
                with self.assertRaisesRegex(RuntimeError, '空间'):
                    backup.copy_area([sys.executable, '-c', 'import time; time.sleep(30)'],
                                     root / 'low-space.log', root, 1)
            with self.assertRaisesRegex(RuntimeError, '退出码 7'):
                backup.copy_area([sys.executable, '-c', 'raise SystemExit(7)'],
                                 root / 'failure.log', root, 0)

    def test_restore_rules_protect_home_and_repository(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, target = root / 'source', root / 'target'
            source.mkdir()
            (source / 'etc').mkdir()
            (source / 'etc/config').write_text('restored')
            (target / 'etc').mkdir(parents=True)
            (target / 'etc/obsolete').write_text('new after backup')
            (target / 'home/wyh').mkdir(parents=True)
            (target / 'home/wyh/new-document').write_text('keep')
            (target / 'var/backups/local-snapshots/generation').mkdir(parents=True)
            protected = target / 'var/backups/local-snapshots/generation/data'
            protected.write_text('keep backup')
            patterns = backup.SYSTEM_EXCLUDES + ['/var/backups/local-snapshots/***']
            command = backup.rsync_command(source, target, patterns)
            subprocess.run(command[:1] + ['--delete-delay'] + command[1:], check=True, capture_output=True)
            self.assertTrue(protected.exists())
            self.assertTrue((target / 'home/wyh/new-document').exists())
            self.assertFalse((target / 'etc/obsolete').exists())
            self.assertEqual((target / 'etc/config').read_text(), 'restored')


if __name__ == '__main__':
    unittest.main()
