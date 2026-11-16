#!/usr/bin/env python3
"""Manual, same-filesystem Linux backups using rsync and immutable generations."""

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import time

DEFAULT_REPOSITORY = Path('/var/backups/local-snapshots')
GIB = 1024 ** 3
AREAS = ('system', 'config', 'software', 'home')
DEFAULT_AREAS = ('system', 'home')
METADATA_FILES = ('packages.tsv', 'apt-manual.txt', 'snaps.txt', 'flatpaks.txt', 'os-release.json')
BASE_OPTIONS = ['-aHAXSx', '--numeric-ids']
SYSTEM_EXCLUDES = [
    '/dev/*', '/proc/*', '/sys/*', '/run/*', '/tmp/*', '/mnt/*',
    '/media/*', '/snap/*', '/lost+found', '/home/*', '/var/tmp/*',
    '/var/cache/*', '/var/log/*', '/var/lib/apt/lists/*', '/root/.cache/***',
    '/swap.img', '/swapfile',
]
HOME_EXCLUDES = ['/*/.cache/***', '/*/.local/share/Trash/***']
ENV = dict(os.environ, LC_ALL='C')


def fail(message):
    raise RuntimeError(message)


def selected_areas(values=None):
    values = DEFAULT_AREAS if values is None else values
    if not isinstance(values, (list, tuple)) or not values or any(not isinstance(value, str) or value not in AREAS for value in values) or len(set(values)) != len(values):
        fail('请选择有效且不重复的备份范围。')
    return tuple(area for area in AREAS if area in values and not (area in ('config', 'software') and 'system' in values))


def snapshot_areas(metadata):
    if metadata.get('schema', 1) == 1:
        return DEFAULT_AREAS
    if metadata.get('schema') != 2:
        fail('不支持的快照格式。')
    areas = metadata.get('areas')
    normalized = selected_areas(areas)
    if areas is None or set(normalized) != set(areas):
        fail('快照范围清单无效。')
    return normalized


def backup_sources(areas=None):
    roots = {'system': Path('/'), 'config': Path('/etc'), 'software': Path('/'), 'home': Path('/home')}
    return {area: roots[area] for area in selected_areas(areas)}


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=True, indent=2) + '\n')


def digest(path):
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(block)
    return checksum.hexdigest()


def available(path):
    usage = os.statvfs(path)
    return usage.f_bavail * usage.f_frsize


def run(command, **kwargs):
    return subprocess.run(command, env=ENV, check=True, **kwargs)


def software_exclusions(mode='files', root=Path('/')):
    if mode not in ('files', 'reinstall'):
        fail('软件备份方式必须为 files 或 reinstall。')
    allowed = {'etc', 'var'}
    if mode == 'files':
        allowed.update(('usr', 'opt', 'bin', 'sbin', 'lib', 'lib32', 'lib64', 'libx32'))
    patterns = list(SYSTEM_EXCLUDES)
    patterns.extend('/' + path.name for path in root.iterdir() if path.name not in allowed)
    if (root / 'var').is_dir():
        patterns.extend('/var/' + path.name for path in (root / 'var').iterdir() if path.name not in ('lib', 'opt'))
    if mode == 'reinstall':
        patterns += ['/var/lib/snapd/snaps/***', '/var/lib/flatpak/***']
    return patterns


def exclusions(repository, software_mode='files'):
    system = SYSTEM_EXCLUDES + [repository.as_posix() + '/***']
    for line in Path('/proc/swaps').read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) > 1 and fields[1] == 'file':
            # /proc escapes whitespace and backslashes using octal sequences.
            name = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[0])
            if name not in system:
                system.append(name)
    roots = {'system': Path('/'), 'config': Path('/etc'), 'software': Path('/'), 'home': Path('/home')}
    return protect_repository(roots, {'system': system, 'home': HOME_EXCLUDES, 'config': [], 'software': software_exclusions(software_mode)}, repository)


def protect_repository(sources, patterns, repository):
    result = {area: list(values) for area, values in patterns.items()}
    for area, source in sources.items():
        if repository.is_relative_to(source):
            relative = repository.relative_to(source)
            if not relative.parts:
                fail('备份保存目录不能与来源目录相同。')
            pattern = '/' + relative.as_posix() + '/***'
            if pattern not in result[area]:
                result[area].append(pattern)
    return result


def rsync_command(source, target, patterns, previous=None, dry_run=False):
    command = ['rsync', *BASE_OPTIONS, '--stats']
    if dry_run:
        command.append('--dry-run')
    if previous is not None:
        # Also detect edits that preserve both size and modification time.
        command.append('--checksum')
        command.append('--link-dest=' + str(previous))
    command.extend('--exclude=' + pattern for pattern in patterns)
    return command + [str(source).rstrip('/') + '/', str(target).rstrip('/') + '/']


def check_host(repository, *, directory_mode=0o700, require_same_filesystem=True):
    if os.geteuid() != 0:
        fail('准确读取系统文件需要 root，请使用 sudo。')
    if shutil.which('rsync') is None:
        fail('未安装 rsync。')
    if os.statvfs('/').f_flag & os.ST_RDONLY:
        fail('当前根目录只读；请在真实主机环境运行，不能从受限沙箱创建系统备份。')
    if not repository.is_absolute() or repository == Path('/'):
        fail('备份目录必须是绝对路径，且不能是根目录。')
    if any(char in str(repository) for char in '*?[]\\\n\r'):
        fail('备份目录不能包含 rsync 通配符、反斜杠或换行。')
    if repository.resolve() != repository:
        fail('备份目录路径不能包含符号链接或 ..。')
    if repository.is_relative_to('/home'):
        fail('请将备份放在 /home 之外，例如 /var/backups/local-snapshots。')
    parent = repository
    while not parent.exists():
        parent = parent.parent
    if require_same_filesystem and parent.stat().st_dev != Path('/').stat().st_dev:
        fail('本方案要求备份目录与系统根目录位于同一文件系统。')
    # Protect the root-owned backup against symlink replacement by other users.
    for directory in [parent, *parent.parents]:
        info = directory.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            fail(f'目录应由 root 拥有且不可由其他用户写入：{directory}')
    if repository.exists() and stat.S_IMODE(repository.stat().st_mode) != directory_mode:
        fail(f'现有备份目录权限必须为 {directory_mode:o}。')
    mounts = run(['findmnt', '--json', '--list', '--output', 'TARGET'],
                 capture_output=True, text=True)
    protected = ['/home', '/boot', '/usr', '/var', '/opt', '/root', '/etc', '/srv']
    for item in json.loads(mounts.stdout)['filesystems']:
        target = item['target']
        if any(target == p or target.startswith(p + '/') for p in protected):
            fail(f'检测到额外挂载点 {target}；需先为该文件系统单独制定备份。')


def completed(repository):
    if not repository.exists():
        return []
    return sorted(p for p in repository.iterdir()
                  if p.is_dir() and not p.is_symlink()
                  and re.fullmatch(r'\d{8}T\d{6}\.\d{6}Z-[0-9a-f]{8}', p.name)
                  and (p / 'snapshot.json').is_file())


def estimate(sources, patterns, previous):
    total = 0
    with tempfile.TemporaryDirectory(prefix='local-snapshot-plan-') as temp:
        for area, source in sources.items():
            target = Path(temp) / area
            target.mkdir()
            result = run(rsync_command(source, target, patterns[area],
                         previous / area if previous and (previous / area).is_dir() else None, dry_run=True),
                         capture_output=True, text=True)
            match = re.search(r'Total transferred file size: ([\d,]+) bytes', result.stdout)
            if match is None:
                fail('无法读取 rsync 空间估算。')
            size = int(match[1].replace(',', ''))
            print(f'{area}: 预计新复制 {size / GIB:.2f} GiB', flush=True)
            total += size
    return total


def iter_entries(snapshot, areas=None):
    def walk(directory):
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda e: e.name)
        for entry in entries:
            path = Path(entry.path)
            yield path
            if entry.is_dir(follow_symlinks=False):
                yield from walk(path)
    if areas is None:
        areas = [area for area in AREAS if (snapshot / area).exists() or (snapshot / area).is_symlink()]
    for area in areas:
        if (snapshot / area).is_symlink() or not (snapshot / area).is_dir():
            fail('备份范围目录缺失或类型无效：' + area)
        yield snapshot / area
        yield from walk(snapshot / area)


def entry_record(path, snapshot, hashes):
    info = path.lstat()
    record = {'path': str(path.relative_to(snapshot)), 'mode': info.st_mode,
              'uid': info.st_uid, 'gid': info.st_gid}
    if stat.S_ISREG(info.st_mode):
        key = (info.st_dev, info.st_ino)
        if key not in hashes:
            hashes[key] = digest(path)
        record.update(size=info.st_size, mtime_ns=info.st_mtime_ns, sha256=hashes[key])
    elif stat.S_ISLNK(info.st_mode):
        record['target'] = os.readlink(path)
    elif stat.S_ISCHR(info.st_mode) or stat.S_ISBLK(info.st_mode):
        record['rdev'] = info.st_rdev
    return record


def create_manifest(snapshot, areas=None):
    count, hashes = 0, {}
    with (snapshot / 'integrity.jsonl').open('w') as stream:
        for path in iter_entries(snapshot, areas):
            record = entry_record(path, snapshot, hashes)
            stream.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + '\n')
            count += 1
    return count


def verify(snapshot):
    metadata = json.loads((snapshot / 'snapshot.json').read_text())
    areas = snapshot_areas(metadata)
    present = {area for area in AREAS if (snapshot / area).exists() or (snapshot / area).is_symlink()}
    if present != set(areas):
        fail('备份范围目录与清单不一致。')
    extra = metadata.get('metadata_sha256', {})
    if not isinstance(extra, dict):
        fail('软件或系统元数据清单无效。')
    for name, expected_digest in extra.items():
        if name not in METADATA_FILES or not (snapshot / name).is_file() or (snapshot / name).is_symlink() or digest(snapshot / name) != expected_digest:
            fail('软件或系统元数据校验失败：' + str(name))
    manifest = snapshot / 'integrity.jsonl'
    if digest(manifest) != metadata['manifest_sha256']:
        fail('校验清单自身已发生变化。')
    expected, hashes = set(), {}
    with manifest.open() as stream:
        for line in stream:
            record = json.loads(line)
            relative = Path(record['path'])
            if not relative.parts or relative.is_absolute() or '..' in relative.parts or relative.parts[0] not in areas:
                fail('校验清单包含非法路径。')
            path = snapshot / relative
            if entry_record(path, snapshot, hashes) != record:
                fail(f'备份校验失败：{relative}')
            expected.add(str(relative))
    actual = {str(p.relative_to(snapshot)) for p in iter_entries(snapshot, areas)}
    if actual != expected or len(expected) != metadata['entries']:
        fail('文件列表与校验清单不一致。')
    print(f'校验通过：{snapshot.name}，{len(expected)} 个条目。')


def copy_area(command, log_path, repository, reserve_bytes):
    with log_path.open('w') as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=ENV)
        try:
            while process.poll() is None:
                if available(repository) < reserve_bytes:
                    fail('可用空间已触及保留阈值，停止本次备份。')
                time.sleep(0.5)
            if process.returncode:
                fail(f'rsync 退出码 {process.returncode}，请查看 {log_path}')
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def capture_metadata(snapshot):
    write_json(snapshot / 'os-release.json', system_identity())
    commands = {
        'packages.tsv': ['dpkg-query', '-W', '-f=${binary:Package}\t${Version}\t${Architecture}\t${db:Status-Abbrev}\n'],
        'apt-manual.txt': ['apt-mark', 'showmanual'],
        'disks.txt': ['lsblk', '-f'],
        'mounts.txt': ['findmnt', '-o', 'TARGET,SOURCE,FSTYPE,OPTIONS'],
        'rsync-version.txt': ['rsync', '--version'],
    }
    if shutil.which('snap'):
        commands['snaps.txt'] = ['snap', 'list', '--all']
    if shutil.which('flatpak'):
        commands['flatpaks.txt'] = ['flatpak', 'list', '--columns=application,branch,origin,installation']
    for name, command in commands.items():
        try:
            with (snapshot / name).open('w') as stream:
                subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT,
                               env=ENV, check=True, timeout=30)
        except (subprocess.SubprocessError, OSError) as exc:
            if name in ('packages.tsv', 'apt-manual.txt'):
                raise
            print(f'附加清单未成功获取：{name} ({exc})', flush=True)


def system_identity(root=Path('/')):
    values = {}
    source = root / 'etc/os-release'
    if root != Path('/') and not source.resolve().is_relative_to(root):
        fail('目标系统版本文件不能指向目标目录之外。')
    for line in source.read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            name, value = line.split('=', 1)
            decoded = shlex.split(value)
            values[name] = decoded[0] if decoded else ''
    return {name: values.get(name, '') for name in ('ID', 'VERSION_ID')} | {'machine': os.uname().machine}


def metadata_hashes(directory):
    return {name: digest(directory / name) for name in METADATA_FILES if (directory / name).is_file() and not (directory / name).is_symlink()}


def create_snapshot(repository, sources, patterns, previous, reserve_bytes, software_mode='files'):
    patterns = protect_repository(sources, patterns, repository)
    areas = selected_areas(list(sources))
    if set(areas) != set(sources):
        fail('系统备份已包含配置，请勿重复复制配置范围。')
    started = dt.datetime.now(dt.timezone.utc)
    name = started.strftime('%Y%m%dT%H%M%S.%fZ-') + os.urandom(4).hex()
    staging = repository / ('.incomplete-' + name)
    staging.mkdir(mode=0o700)
    for area in sources:
        (staging / area).mkdir()
        (staging / (area + '-excludes.txt')).write_text('\n'.join(patterns[area]) + '\n')
    write_json(staging / 'progress.json', {'started': started.isoformat(), 'previous': previous.name if previous else None})
    print(f'创建备份 {name}；失败时保留 .incomplete 目录供检查。', flush=True)
    capture_metadata(staging)
    for area, source in sources.items():
        print(f'正在复制 {area}…', flush=True)
        command = rsync_command(source, staging / area, patterns[area], previous / area if previous and (previous / area).is_dir() else None)
        copy_area(command, staging / (area + '-rsync.log'), repository, reserve_bytes)
    print('正在生成文件 SHA-256 校验清单…', flush=True)
    count = create_manifest(staging, areas)
    write_json(staging / 'snapshot.json', {
        'schema': 1 if set(areas) == set(DEFAULT_AREAS) else 2, 'areas': list(areas), 'name': name, 'host': socket.gethostname(),
        'started': started.isoformat(), 'completed': dt.datetime.now(dt.timezone.utc).isoformat(),
        'previous': previous.name if previous else None, 'entries': count,
        'sources': {k: str(v) for k, v in sources.items()}, 'excludes': patterns,
        'software_mode': software_mode, 'metadata_sha256': metadata_hashes(staging),
        'manifest_sha256': digest(staging / 'integrity.jsonl'),
    })
    # Flush the backup before advertising it as a completed generation.
    os.sync()
    final = repository / name
    staging.rename(final)
    pointer = repository / ('.latest-new-' + name)
    os.symlink(name, pointer)
    os.replace(pointer, repository / 'latest')
    os.sync()
    print(f'备份完成：{final}', flush=True)
    print(f'磁盘剩余 {available(repository) / GIB:.2f} GiB；未自动删除任何历史备份。')
    return final


def lock_package_manager():
    handles = []
    try:
        for path in ['/var/lib/dpkg/lock-frontend', '/var/lib/dpkg/lock']:
            handle = open(path, 'r+')
            handles.append(handle)
            fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        for handle in handles:
            handle.close()
        fail('无法锁定软件包数据库；请等待 APT/dpkg 操作结束。')
    return handles


def select_snapshot(repository, name):
    choices = completed(repository)
    if not choices:
        fail('没有完整备份。')
    if name == 'latest':
        return choices[-1]
    for path in choices:
        if path.name == name:
            return path
    fail('未找到指定的完整备份。')


def restore_command(snapshot, area, target, relative=None, apply=False):
    """Build one restore command; source generations are always read-only."""
    snapshot = snapshot.resolve(strict=True)
    if area not in AREAS:
        fail('恢复范围必须为 system、config 或 home。')
    if not target.is_absolute() or target.resolve() != target or not target.is_dir():
        fail('恢复目标必须是已存在、不含符号链接的绝对目录。')
    if target == Path('/'):
        fail('禁止恢复到正在运行的根目录 /。系统恢复请使用救援环境。')
    source = snapshot / area
    if target.is_relative_to(snapshot) or (area != 'system' and snapshot.is_relative_to(target)):
        fail('恢复目标与备份目录不能互相包含。')
    if area == 'system':
        if relative is not None:
            fail('系统恢复不接受局部路径，请使用 home 范围恢复用户文件。')
        if not target.is_mount() or target.stat().st_dev == Path('/').stat().st_dev:
            fail('系统恢复目标必须是救援环境中单独挂载的系统分区。')
    metadata_path = snapshot / 'snapshot.json'
    data = json.loads(metadata_path.read_text()) if metadata_path.is_file() else {}
    areas = snapshot_areas(data)
    if area == 'config' and 'config' not in areas and 'system' in areas:
        source = snapshot / 'system' / 'etc'
    elif area == 'software' and 'software' not in areas and 'system' in areas:
        source = snapshot / 'system'
    elif area not in areas:
        fail('该版本不包含所选恢复范围。')
    if source.is_symlink() or not source.is_dir():
        fail('该版本不包含所选恢复范围目录。')
    command = ['rsync', '-aHAXS', '--numeric-ids', '--itemize-changes']
    if not apply:
        command.append('--dry-run')
    if area == 'system':
        patterns = list(data['excludes']['system'])
        if not all(pattern in patterns for pattern in SYSTEM_EXCLUDES):
            fail('备份缺少必要的系统恢复保护规则。')
        if snapshot.is_relative_to(target):
            patterns.append('/' + str(snapshot.relative_to(target)) + '/***')
        command.append('--delete-delay')
        command.extend('--exclude=' + pattern for pattern in patterns)
    if area == 'software':
        if 'software' not in data.get('excludes', {}):
            fail('此版本缺少独立软件恢复规则。')
        if (target / 'etc/os-release').exists():
            recorded = snapshot / 'os-release.json'
            if not recorded.is_file() or json.loads(recorded.read_text()) != system_identity(target):
                fail('软件文件恢复到系统目录要求相同的系统版本与 CPU 架构；可先恢复到独立空目录检查。')
        command.extend('--exclude=' + pattern for pattern in data['excludes']['software'])
    if relative is not None:
        path = Path(relative)
        if not path.parts or path.is_absolute() or '..' in path.parts:
            fail('文件路径必须是所选备份范围内的相对路径。')
        current = source
        for part in path.parts[:-1]:
            current /= part
            if current.is_symlink():
                fail('所选路径的父目录不能是符号链接。')
        selected = source / path
        if not selected.exists() and not selected.is_symlink():
            fail('备份中不存在所选路径。')
        command.append('--relative')
        source_arg = str(source) + '/./' + str(path)
    else:
        source_arg = str(source) + '/'
    return command + [source_arg, str(target) + '/']


def software_install_command(snapshot, simulate=True):
    data = json.loads((snapshot / 'snapshot.json').read_text())
    if not set(snapshot_areas(data)) & {'system', 'software'}:
        fail('该版本未选择系统或软件备份，不能按清单重装。')
    if not {'packages.tsv', 'os-release.json'} <= data.get('metadata_sha256', {}).keys():
        fail('此版本缺少受校验的软件清单或系统版本信息，不能自动重装。')
    recorded = json.loads((snapshot / 'os-release.json').read_text())
    if not all(recorded.get(name) for name in ('ID', 'VERSION_ID', 'machine')):
        fail('软件清单缺少明确的系统版本或 CPU 架构。')
    if recorded != system_identity():
        fail('软件清单重装要求相同的发行版版本与 CPU 架构。')
    if shutil.which('apt-get') is None:
        fail('清单重装当前支持使用 apt 的 Debian / Ubuntu 系统。')
    packages = []
    for line in (snapshot / 'packages.tsv').read_text().splitlines():
        columns = line.split('\t')
        if len(columns) != 4 or len(columns[3].strip()) < 2 or columns[3].strip()[1] != 'i':
            continue
        name, version = columns[:2]
        if re.fullmatch(r'[a-z0-9][a-z0-9+.-]*(?::[a-z0-9_-]+)?', name) is None or re.fullmatch(r'[A-Za-z0-9.+:~_-]+', version) is None:
            fail('软件清单包含非法包名或版本。')
        packages.append(name + '=' + version)
    if not packages or len(packages) > 20000:
        fail('软件清单为空或条目过多。')
    options = ['apt-get', '--simulate' if simulate else '--yes', '--no-remove', '--allow-downgrades',
               '--no-install-recommends', '-o', 'Dpkg::Options::=--force-confold', 'install']
    return options + packages


def restore_tree(snapshot, area, target, relative=None, apply=False):
    if os.geteuid() != 0:
        fail('恢复权限、属主和扩展属性需要 sudo。')
    if area == 'packages':
        if target != Path('/') or relative:
            fail('软件清单重装仅作用于当前测试机 /，不接受局部路径。')
        verify(snapshot)
        print('软件重装预览：精确版本、禁止移除已有软件，不自动回写备份中的配置与应用数据。', flush=True)
        run(software_install_command(snapshot))
        if apply:
            print('正在按清单重装 apt 软件；配置和应用数据可另行恢复到独立目录。', flush=True)
            run(software_install_command(snapshot, simulate=False), env=dict(ENV, DEBIAN_FRONTEND='noninteractive'))
        return
    # Guard the target before spending time reading a complete generation.
    command = restore_command(snapshot, area, target, relative, apply)
    verify(snapshot)
    print('实际恢复' if apply else '恢复预览（未写入目标）', flush=True)
    run(command)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path, default=DEFAULT_REPOSITORY)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ['plan', 'create']:
        item = sub.add_parser(name)
        item.add_argument('--reserve-gib', type=int, default=8)
        item.add_argument('--areas', nargs='+', choices=AREAS, help='选择 system（含配置）、config 或 home；默认 system home')
        item.add_argument('--software-mode', choices=('files', 'reinstall'), default='files')
    sub.add_parser('list')
    item = sub.add_parser('verify')
    item.add_argument('snapshot', nargs='?', default='latest')
    item = sub.add_parser('restore')
    item.add_argument('snapshot', nargs='?', default='latest')
    item.add_argument('--area', choices=(*AREAS, 'packages'), required=True)
    item.add_argument('--target', type=Path, required=True)
    item.add_argument('--relative', help='相对于备份 home 的用户文件路径，例如 wyh/文档')
    item.add_argument('--apply', action='store_true', help='执行恢复；默认只预览')
    args = parser.parse_args()
    repository = args.repository
    if args.command == 'list':
        for path in completed(repository):
            data = json.loads((path / 'snapshot.json').read_text())
            print(f"{path.name}\t{data['completed']}\t{data['entries']} entries")
        return
    if args.command == 'verify':
        verify(select_snapshot(repository, args.snapshot))
        return
    if args.command == 'restore':
        restore_tree(select_snapshot(repository, args.snapshot), args.area,
                     args.target, args.relative, args.apply)
        return
    check_host(repository)
    if args.reserve_gib < 8:
        fail('本方案至少预留 8 GiB，防止挤占系统运行空间。')
    os.umask(0o077)
    repository.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (repository / '.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail('已有备份任务正在运行。')
        if list(repository.glob('.incomplete-*')):
            fail('发现未完成的备份，请先检查其日志和占用空间；不会自动清理或重复创建。')
        handles = lock_package_manager()
        try:
            choices = completed(repository)
            previous = choices[-1] if choices else None
            sources = backup_sources(args.areas)
            mode = 'files' if 'system' in sources else args.software_mode
            patterns = exclusions(repository, mode)
            size = estimate(sources, patterns, previous)
            free = available(repository)
            reserve = args.reserve_gib * GIB
            needed = int(size * 1.05) + 256 * 1024 ** 2
            print(f'可用 {free / GIB:.2f} GiB；估算需 {needed / GIB:.2f} GiB；保留 {args.reserve_gib} GiB。', flush=True)
            if free - needed < reserve:
                fail('空间不足，不会开始复制。')
            if args.command == 'create':
                create_snapshot(repository, sources, patterns, previous, reserve, mode)
            else:
                print('空间预检通过。未复制文件；仅初始化了受保护的备份目录和锁文件。')
        finally:
            for handle in handles:
                handle.close()


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        print(f'错误：{exc}', file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('已中断；未完成的备份不会作为完整恢复点。', file=sys.stderr)
        sys.exit(130)
