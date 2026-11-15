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
import socket
import stat
import subprocess
import sys
import tempfile
import time

DEFAULT_REPOSITORY = Path('/var/backups/local-snapshots')
GIB = 1024 ** 3
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


def exclusions(repository):
    system = SYSTEM_EXCLUDES + [repository.as_posix() + '/***']
    for line in Path('/proc/swaps').read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) > 1 and fields[1] == 'file':
            # /proc escapes whitespace and backslashes using octal sequences.
            name = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[0])
            if name not in system:
                system.append(name)
    return {'system': system, 'home': HOME_EXCLUDES}


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
                         previous / area if previous else None, dry_run=True),
                         capture_output=True, text=True)
            match = re.search(r'Total transferred file size: ([\d,]+) bytes', result.stdout)
            if match is None:
                fail('无法读取 rsync 空间估算。')
            size = int(match[1].replace(',', ''))
            print(f'{area}: 预计新复制 {size / GIB:.2f} GiB', flush=True)
            total += size
    return total


def iter_entries(snapshot):
    def walk(directory):
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda e: e.name)
        for entry in entries:
            path = Path(entry.path)
            yield path
            if entry.is_dir(follow_symlinks=False):
                yield from walk(path)
    for area in ['system', 'home']:
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


def create_manifest(snapshot):
    count, hashes = 0, {}
    with (snapshot / 'integrity.jsonl').open('w') as stream:
        for path in iter_entries(snapshot):
            record = entry_record(path, snapshot, hashes)
            stream.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + '\n')
            count += 1
    return count


def verify(snapshot):
    metadata = json.loads((snapshot / 'snapshot.json').read_text())
    manifest = snapshot / 'integrity.jsonl'
    if digest(manifest) != metadata['manifest_sha256']:
        fail('校验清单自身已发生变化。')
    expected, hashes = set(), {}
    with manifest.open() as stream:
        for line in stream:
            record = json.loads(line)
            relative = Path(record['path'])
            if relative.is_absolute() or '..' in relative.parts or relative.parts[0] not in ('system', 'home'):
                fail('校验清单包含非法路径。')
            path = snapshot / relative
            if entry_record(path, snapshot, hashes) != record:
                fail(f'备份校验失败：{relative}')
            expected.add(str(relative))
    actual = {str(p.relative_to(snapshot)) for p in iter_entries(snapshot)}
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


def create_snapshot(repository, sources, patterns, previous, reserve_bytes):
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
        command = rsync_command(source, staging / area, patterns[area], previous / area if previous else None)
        copy_area(command, staging / (area + '-rsync.log'), repository, reserve_bytes)
    print('正在生成文件 SHA-256 校验清单…', flush=True)
    count = create_manifest(staging)
    write_json(staging / 'snapshot.json', {
        'schema': 1, 'name': name, 'host': socket.gethostname(),
        'started': started.isoformat(), 'completed': dt.datetime.now(dt.timezone.utc).isoformat(),
        'previous': previous.name if previous else None, 'entries': count,
        'sources': {k: str(v) for k, v in sources.items()}, 'excludes': patterns,
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
    if area not in ('system', 'home'):
        fail('恢复范围必须为 system 或 home。')
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
    command = ['rsync', '-aHAXS', '--numeric-ids', '--itemize-changes']
    if not apply:
        command.append('--dry-run')
    if area == 'system':
        data = json.loads((snapshot / 'snapshot.json').read_text())
        patterns = list(data['excludes']['system'])
        if not all(pattern in patterns for pattern in SYSTEM_EXCLUDES):
            fail('备份缺少必要的系统恢复保护规则。')
        if snapshot.is_relative_to(target):
            patterns.append('/' + str(snapshot.relative_to(target)) + '/***')
        command.append('--delete-delay')
        command.extend('--exclude=' + pattern for pattern in patterns)
    if relative is not None:
        path = Path(relative)
        if not path.parts or path.is_absolute() or '..' in path.parts:
            fail('用户文件路径必须是 home 内的相对路径。')
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


def restore_tree(snapshot, area, target, relative=None, apply=False):
    if os.geteuid() != 0:
        fail('恢复权限、属主和扩展属性需要 sudo。')
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
    sub.add_parser('list')
    item = sub.add_parser('verify')
    item.add_argument('snapshot', nargs='?', default='latest')
    item = sub.add_parser('restore')
    item.add_argument('snapshot', nargs='?', default='latest')
    item.add_argument('--area', choices=['system', 'home'], required=True)
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
            sources = {'system': Path('/'), 'home': Path('/home')}
            patterns = exclusions(repository)
            size = estimate(sources, patterns, previous)
            free = available(repository)
            reserve = args.reserve_gib * GIB
            needed = int(size * 1.05) + 256 * 1024 ** 2
            print(f'可用 {free / GIB:.2f} GiB；估算需 {needed / GIB:.2f} GiB；保留 {args.reserve_gib} GiB。', flush=True)
            if free - needed < reserve:
                fail('空间不足，不会开始复制。')
            if args.command == 'create':
                create_snapshot(repository, sources, patterns, previous, reserve)
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
