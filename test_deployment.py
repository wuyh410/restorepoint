import configparser
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import build_desktop
from desktop import backend, runtime


class DeploymentTest(unittest.TestCase):
    def test_nuitka_child_uses_executable_without_a_python_script_argument(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / 'RestorePoint'
            executable.write_bytes(b'fixture')
            with mock.patch.object(runtime, '__compiled__', object(), create=True), mock.patch.object(sys, 'argv', [str(executable)]), mock.patch.object(sys, 'executable', str(Path(temp) / 'missing-python')):
                command = backend.engine_command('gui_bridge', ['doctor'])
            self.assertEqual(command, [str(executable.resolve()), '--engine', 'gui_bridge', 'doctor'])

    def test_source_child_still_uses_the_source_entry_point(self):
        self.assertFalse(runtime.compiled_application())
        command = backend.engine_command('gui_bridge', ['doctor'])
        self.assertEqual(Path(command[1]).name, 'app.py')
        self.assertEqual(command[-3:], ['--engine', 'gui_bridge', 'doctor'])

    def test_helper_prefers_packaged_text_copy_and_rejects_unknown_names(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'snapshot.py').write_text('source')
            (root / 'helpers').mkdir()
            packaged = root / 'helpers/snapshot.py.txt'
            packaged.write_text('packaged')
            with mock.patch.object(runtime, 'resource_root', return_value=root):
                self.assertEqual(runtime.helper_source('snapshot.py'), packaged)
                packaged.unlink()
                self.assertEqual(runtime.helper_source('snapshot.py'), root / 'snapshot.py')
                with self.assertRaises(ValueError):
                    runtime.helper_source('../../secret')

    def test_deploy_config_has_exact_helper_copies_and_standalone_mode(self):
        source = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copyfile(source / 'pysidedeploy.spec', root / 'pysidedeploy.spec')
            for name in build_desktop.HELPERS:
                shutil.copyfile(source / name, root / name)
            config, output = build_desktop.prepare(root, 2)
            parser = configparser.ConfigParser(interpolation=None)
            parser.read(config)
            self.assertEqual(parser['nuitka']['mode'], 'standalone')
            self.assertEqual(Path(parser['app']['exec_directory']), output)
            for name in build_desktop.HELPERS:
                self.assertEqual((config.parent / 'helpers' / (name + '.txt')).read_bytes(), (source / name).read_bytes())
            self.assertIn('--output-filename=RestorePoint', parser['nuitka']['extra_args'])

    def test_source_runtime_report_checks_helpers_without_loading_qt(self):
        with tempfile.TemporaryDirectory() as temp:
            report = Path(temp) / 'runtime.json'
            subprocess.run([sys.executable, str(runtime.resource_root() / 'app.py'), '--runtime-info', str(report)], check=True)
            data = json.loads(report.read_text(encoding='utf-8'))
            self.assertFalse(data['compiled'])
            for name in build_desktop.HELPERS:
                self.assertEqual(data['helpers'][name]['sha256'], hashlib.sha256((runtime.resource_root() / name).read_bytes()).hexdigest())


if __name__ == '__main__':
    unittest.main()
