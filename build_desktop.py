#!/usr/bin/env python3
"""Build through Qt's pyside6-deploy/Nuitka on the target operating system."""
import argparse
import configparser
import hashlib
import importlib.metadata
from pathlib import Path
import json
import os
import shutil
import shlex
import struct
import subprocess
import sys
import sysconfig
import uuid

HELPERS = ('snapshot.py', 'portable.py', 'gui_bridge.py')
DOCUMENTS = ('README.md', 'GUI.md', 'LOCAL.md', 'REMOTE.md')


def make_icons(directory):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    application = QGuiApplication.instance() or QGuiApplication([])
    image = QImage(256, 256, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    renderer = QSvgRenderer(str(Path(__file__).resolve().parent / 'assets/icon.svg'))
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    if not image.save(str(directory / 'icon.png')):
        raise RuntimeError('Unable to render application icon')
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, 'PNG')
    buffer.close()
    payload = bytes(data)
    (directory / 'icon.ico').write_bytes(struct.pack('<HHH', 0, 1, 1) + struct.pack('<BBBBHHII', 0, 0, 0, 0, 1, 32, len(payload), 22) + payload)
    return application


def prepare(root, jobs):
    staging = root / 'build/qt-deploy'
    staging.mkdir(parents=True, exist_ok=True)
    helpers = staging / 'helpers'
    helpers.mkdir(exist_ok=True)
    arguments = []
    for name in HELPERS:
        copy = helpers / (name + '.txt')
        shutil.copyfile(root / name, copy)
        arguments.append('--include-data-files=' + str(copy) + '=helpers/' + copy.name)
    for name in DOCUMENTS:
        arguments.append('--include-data-files=' + str(root / name) + '=' + name)
    arguments += ['--include-data-dir=' + str(root / 'assets') + '=assets',
                  '--include-distribution-metadata=PySide6,paramiko,cryptography',
                  '--jobs=' + str(jobs), '--assume-yes-for-downloads',
                  '--disable-ccache', '--output-filename=' + ('RestorePoint.exe' if sys.platform == 'win32' else 'RestorePoint')]
    if sys.platform == 'win32':
        arguments += ['--windows-console-mode=disable', '--msvc=latest']
    # Keep every deployment output fresh. Qt's tool can merge an existing output
    # directory, which would otherwise retain libraries from earlier builds.
    output = root / 'build/qt-output' / uuid.uuid4().hex
    output.mkdir(parents=True)
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(root / 'pysidedeploy.spec')
    parser['app'].update(project_dir=str(root), input_file=str(root / 'app.py'),
                         project_file='pyproject.toml', exec_directory=str(output),
                         icon=str(staging / ('icon.ico' if sys.platform == 'win32' else 'icon.png')))
    parser['nuitka']['extra_args'] += ' ' + shlex.join(arguments)
    spec = staging / 'pysidedeploy.spec'
    with spec.open('w', encoding='utf-8') as stream:
        parser.write(stream)
    return spec, output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='Print the official deploy/Nuitka command without compiling')
    parser.add_argument('--jobs', type=int, default=2)
    parser.add_argument('--smoke-test', action='store_true', help='Render Qt offscreen and check the read-only Linux engine')
    args = parser.parse_args()
    if sys.platform not in ('linux', 'win32'):
        raise SystemExit('This project builds Linux and Windows clients.')
    if not 1 <= args.jobs <= 64:
        raise SystemExit('--jobs must be between 1 and 64.')
    root = Path(__file__).resolve().parent
    include = Path(sysconfig.get_path('include')) / 'Python.h'
    if not args.dry_run and not include.exists():
        raise SystemExit('Python development headers are missing: ' + str(include) + '\nUse a build Python with development headers, or install the matching development package.')
    tool = Path(sysconfig.get_path('scripts')) / ('pyside6-deploy.exe' if sys.platform == 'win32' else 'pyside6-deploy')
    if not tool.is_file():
        raise SystemExit('Install build dependencies: python -m pip install -r requirements-build.txt')
    spec, output = prepare(root, args.jobs)
    application = make_icons(spec.parent)
    command = [str(tool), str(root / 'app.py'), '-c', str(spec), '--no-install', '-f',
               '--keep-deployment-files', '--extra-ignore-dirs=.venv,.build-venv,releases,screenshots']
    if args.dry_run:
        command.append('--dry-run')
    env = dict(os.environ)
    env['PATH'] = os.pathsep.join((str(tool.parent), str(Path(sys.executable).parent), env.get('PATH', '')))
    env['NUITKA_CACHE_DIR'] = str(root / 'build/nuitka-cache')
    temporary = root / 'build/tmp'
    temporary.mkdir(parents=True, exist_ok=True)
    for key in ('TMPDIR', 'TEMP', 'TMP'):
        env[key] = str(temporary)
    print('Building with Qt pyside6-deploy / Nuitka (standalone)', flush=True)
    subprocess.run(command, cwd=root, check=True, env=env)
    if args.dry_run:
        return
    result = output / 'RestorePoint.dist'
    executable = 'RestorePoint.exe' if sys.platform == 'win32' else 'RestorePoint'
    if not (result / executable).is_file():
        raise SystemExit('Qt deploy did not produce the expected executable: ' + str(result))
    expected_helpers = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in HELPERS}
    runtime_file = spec.parent / 'runtime-info.json'
    subprocess.run([str(result / executable), '--runtime-info', str(runtime_file)], check=True, env=env)
    runtime = json.loads(runtime_file.read_text(encoding='utf-8'))
    if (not runtime['compiled'] or len(runtime['engine_command']) != 4
            or Path(runtime['engine_command'][0]).resolve() != (result / executable).resolve()):
        raise SystemExit('Compiled child-process detection failed; final output was not replaced.')
    if {name: record['sha256'] for name, record in runtime['helpers'].items()} != expected_helpers:
        raise SystemExit('Bundled Linux helper resources do not match the source.')
    if args.smoke_test:
        smoke_env = dict(env, QT_QPA_PLATFORM='offscreen')
        subprocess.run([str(result / executable), '--data-dir', str(spec.parent / ('smoke-' + uuid.uuid4().hex)),
                        '--screenshot', str(spec.parent / 'smoke.png')], check=True, env=smoke_env, timeout=60)
        if sys.platform == 'linux':
            checked = subprocess.run(runtime['engine_command'], check=True, env=env, capture_output=True, text=True, timeout=30)
            if 'RP_RESULT=' not in checked.stdout:
                raise SystemExit('The compiled Linux engine did not return its read-only doctor result.')
        print('Smoke checks passed; no backup, restore or SSH action was executed.', flush=True)
    (result / 'build-info.json').write_text(json.dumps({
        'builder': 'pyside6-deploy/Nuitka', 'mode': 'standalone', 'platform': sys.platform,
        'python': sys.version.split()[0], 'nuitka': importlib.metadata.version('Nuitka'),
        'pyside6': importlib.metadata.version('PySide6'),
        'helpers': expected_helpers,
    }, indent=2), encoding='utf-8')
    final = root / 'dist/RestorePoint'
    final.parent.mkdir(exist_ok=True)
    previous = root / 'build' / ('previous-dist-' + uuid.uuid4().hex[:8])
    if final.exists():
        final.rename(previous)
    try:
        result.rename(final)
    except Exception:
        if previous.exists() and not final.exists():
            previous.rename(final)
        raise
    print('Application created in: ' + str(final))


if __name__ == '__main__':
    main()
