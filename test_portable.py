import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

import portable
import snapshot


class PortableTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sources = {'system': self.root / 'source-system', 'home': self.root / 'source-home'}
        for source in self.sources.values():
            source.mkdir()
        self.repository = self.root / 'exports'
        self.repository.mkdir()
        self.patterns = {'system': list(snapshot.SYSTEM_EXCLUDES), 'home': list(snapshot.HOME_EXCLUDES)}
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(snapshot, 'capture_metadata', side_effect=lambda p: (p / 'packages.tsv').write_text('fixture\t1.0\n')).start()
        mock.patch.object(snapshot.os, 'sync').start()
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def export(self):
        return portable.export_bundle(self.repository, self.sources, self.patterns,
                                      32 * 1024 ** 2, 0, os.getuid(), os.getgid())

    def test_roundtrip_metadata_exclusions_and_home_restore(self):
        home = self.sources['home'] / 'wyh'
        home.mkdir()
        data = home / '资料.bin'
        data.write_bytes(bytes(range(256)) * 17)
        data.chmod(0o750)
        os.setxattr(data, 'user.fixture', b'preserved')
        if shutil.which('setfacl'):
            subprocess.run(['setfacl', '-m', f'u:{os.getuid()}:r--', str(data)], check=True)
        os.link(data, home / 'hardlink')
        (home / 'symlink').symlink_to('资料.bin')
        (home / 'absolute-link').symlink_to('/etc/passwd')
        for name in ['.cache/junk', '.local/share/Trash/junk', '.config/app/.cache/keep', 'Case', 'case', 'linux:name']:
            path = home / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
        with (home / 'sparse').open('wb') as stream:
            stream.seek(16 * 1024 ** 2)
            stream.write(b'end')
        for name in ['etc/config', 'home/wyh/skip', 'var/log/junk', 'var/lib/keep', 'dev/skip']:
            path = self.sources['system'] / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
        bundle = self.export()
        portable.verify_bundle(bundle)
        stage = self.root / 'unpacked'
        portable.unpack_bundle(bundle, stage, 0)
        snapshot.verify(stage)
        restored = stage / 'home/wyh'
        self.assertEqual((restored / '资料.bin').read_bytes(), data.read_bytes())
        self.assertEqual(stat.S_IMODE((restored / '资料.bin').stat().st_mode), stat.S_IMODE(data.stat().st_mode))
        self.assertEqual(os.getxattr(restored / '资料.bin', 'user.fixture'), b'preserved')
        if shutil.which('setfacl'):
            self.assertEqual(os.getxattr(restored / '资料.bin', 'system.posix_acl_access'), os.getxattr(data, 'system.posix_acl_access'))
        self.assertEqual((restored / '资料.bin').stat().st_ino, (restored / 'hardlink').stat().st_ino)
        self.assertEqual((restored / 'symlink').readlink(), Path('资料.bin'))
        self.assertEqual((restored / 'absolute-link').readlink(), Path('/etc/passwd'))
        self.assertFalse((restored / '.cache').exists())
        self.assertFalse((restored / '.local/share/Trash').exists())
        self.assertTrue((restored / '.config/app/.cache/keep').exists())
        self.assertTrue((restored / 'Case').exists())
        self.assertTrue((restored / 'case').exists())
        self.assertTrue((restored / 'linux:name').exists())
        self.assertLess((restored / 'sparse').stat().st_blocks * 512, (restored / 'sparse').stat().st_size)
        self.assertFalse((stage / 'system/var/log/junk').exists())
        self.assertFalse((stage / 'system/home/wyh/skip').exists())
        self.assertTrue((stage / 'system/var/lib/keep').exists())
        target = self.root / 'recovered-home'
        target.mkdir()
        (target / 'keep-newer-file').write_text('keep')
        preview = snapshot.restore_command(stage, 'home', target, 'wyh/资料.bin')
        subprocess.run(preview, check=True, capture_output=True)
        self.assertFalse((target / 'wyh').exists())
        subprocess.run(snapshot.restore_command(stage, 'home', target, 'wyh/资料.bin', apply=True), check=True, capture_output=True)
        self.assertEqual((target / 'wyh/资料.bin').read_bytes(), data.read_bytes())
        self.assertTrue((target / 'keep-newer-file').exists())

    def test_corruption_stops_before_unpack(self):
        (self.sources['system'] / 'config').write_text('original')
        bundle = self.export()
        with (bundle / 'system.tar.gz').open('ab') as stream:
            stream.write(b'corruption')
        target = self.root / 'unpacked'
        with self.assertRaisesRegex(RuntimeError, '校验失败'):
            portable.unpack_bundle(bundle, target, 0)
        self.assertFalse(target.exists())

    def test_failed_export_is_not_published(self):
        with mock.patch.object(snapshot, 'copy_area', side_effect=RuntimeError('injected failure')):
            with self.assertRaises(RuntimeError):
                self.export()
        self.assertEqual(len(list(self.repository.glob('.incomplete-*'))), 1)
        self.assertFalse(any((p / 'manifest.json').exists() for p in self.repository.iterdir()))

    def test_untrusted_names_and_link_escapes_are_rejected(self):
        for name, link in [('../outside', None), ('/absolute', None), ('link', '../outside')]:
            archive = self.root / 'unsafe.tar.gz'
            with tarfile.open(archive, 'w:gz') as stream:
                member = tarfile.TarInfo(name)
                if link is not None:
                    member.type = tarfile.LNKTYPE
                    member.linkname = link
                stream.addfile(member)
            with self.assertRaises(RuntimeError):
                portable.validate_archive(archive)
        with tarfile.open(archive, 'w:gz') as stream:
            link = tarfile.TarInfo('directory')
            link.type = tarfile.SYMTYPE
            link.linkname = '/etc'
            stream.addfile(link)
            stream.addfile(tarfile.TarInfo('directory/passwd'))
        with self.assertRaisesRegex(RuntimeError, '符号链接'):
            portable.validate_archive(archive)

    def test_manifest_traversal_and_space_guard(self):
        bundle = self.export()
        manifest = bundle / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['files']['../../outside'] = next(iter(data['files'].values()))
        manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(RuntimeError, '非法文件名'):
            portable.verify_bundle(bundle)
        del data['files']['../../outside']
        manifest.write_text(json.dumps(data))
        with mock.patch.object(snapshot, 'available', return_value=1):
            with self.assertRaisesRegex(RuntimeError, '空间不足'):
                portable.unpack_bundle(bundle, self.root / 'unpacked', 0)
        self.assertFalse((self.root / 'unpacked').exists())

    def test_restore_rejects_active_root_and_unsafe_selection(self):
        source, target = self.root / 'snapshot', self.root / 'target'
        source.mkdir()
        target.mkdir()
        with self.assertRaisesRegex(RuntimeError, '根目录'):
            snapshot.restore_command(source, 'system', Path('/'))
        with self.assertRaisesRegex(RuntimeError, '单独挂载'):
            snapshot.restore_command(source, 'system', target)
        for relative in ['../etc', '/etc', '.']:
            with self.assertRaises(RuntimeError):
                snapshot.restore_command(source, 'home', target, relative)

    def test_explicit_export_id_cannot_overwrite_a_completed_bundle(self):
        name = '20261115T010203.123456Z-1234abcd'
        first = portable.export_bundle(self.repository, self.sources, self.patterns,
                                       0, 0, os.getuid(), os.getgid(), name)
        before = snapshot.digest(first / 'manifest.json')
        with self.assertRaisesRegex(RuntimeError, '已经存在'):
            portable.export_bundle(self.repository, self.sources, self.patterns,
                                   0, 0, os.getuid(), os.getgid(), name)
        self.assertEqual(snapshot.digest(first / 'manifest.json'), before)

    def test_export_cleanup_requires_the_matching_manifest(self):
        bundle = self.export()
        with mock.patch.object(snapshot, 'check_host'):
            with self.assertRaisesRegex(RuntimeError, '不一致'):
                portable.remove_export(self.repository, bundle.name, '0' * 64)
            self.assertTrue(bundle.exists())
            portable.remove_export(self.repository, bundle.name, snapshot.digest(bundle / 'manifest.json'))
        self.assertFalse(bundle.exists())

    def test_system_restore_preserves_home_and_its_own_staging_source(self):
        target = self.root / 'offline-root'
        source = target / 'recovery/source'
        (source / 'system/etc').mkdir(parents=True)
        (source / 'system/etc/config').write_text('old config')
        snapshot.write_json(source / 'snapshot.json', {'excludes': self.patterns})
        (target / 'etc').mkdir()
        (target / 'etc/obsolete').write_text('newer software file')
        (target / 'home/wyh').mkdir(parents=True)
        (target / 'home/wyh/new-document').write_text('keep')
        original_stat = Path.stat
        def fake_stat(path, *args, **kwargs):
            if path == Path('/'):
                return SimpleNamespace(st_dev=-1)
            return original_stat(path, *args, **kwargs)
        with mock.patch.object(Path, 'is_mount', lambda path: path == target), mock.patch.object(Path, 'stat', fake_stat):
            command = snapshot.restore_command(source, 'system', target, apply=True)
        subprocess.run(command, check=True, capture_output=True)
        self.assertFalse((target / 'etc/obsolete').exists())
        self.assertEqual((target / 'etc/config').read_text(), 'old config')
        self.assertTrue((target / 'home/wyh/new-document').exists())
        self.assertTrue((source / 'system/etc/config').exists())


if __name__ == '__main__':
    unittest.main()
