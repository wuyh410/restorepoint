#!/usr/bin/env python3
"""Portable Linux backup bundles for native Windows SSH/SCP controllers."""

import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile

import snapshot as local

EXPORT_ROOT = Path('/var/backups/portable-snapshots')
ID_PATTERN = r'\d{8}T\d{6}\.\d{6}Z-[0-9a-f]{8}'
AREAS = local.AREAS
SCOPED_BACKUP_VERSION = 2


def valid_id(value):
    if re.fullmatch(ID_PATTERN, value) is None:
        local.fail('无效的备份版本名。')
    return value


def tar_patterns(patterns):
    """Use component-local wildcards, matching the shared rsync exclusions."""
    return ['.' + (p[:-4] if p.endswith('/***') else p) for p in patterns]


def tar_command(source, archive, patterns):
    command = ['tar', '--create', '--gzip', '--format=pax', '--numeric-owner',
               '--acls', '--xattrs', '--xattrs-include=*', '--sparse',
               '--one-file-system', '--sort=name',
               '--pax-option=delete=atime,delete=ctime',
               '--wildcards', '--no-wildcards-match-slash', '--anchored']
    command.extend('--exclude=' + p for p in tar_patterns(patterns))
    return command + ['--file', str(archive), '--directory', str(source), '.']


def export_bundle(repository, sources, patterns, estimated_bytes, reserve_bytes, uid, gid, name=None, software_mode='files'):
    """Core operation is also usable with isolated fixture source trees."""
    patterns = local.protect_repository(sources, patterns, repository)
    areas = local.selected_areas(list(sources))
    if set(areas) != set(sources):
        local.fail('系统备份已包含配置，请勿重复打包配置范围。')
    name = valid_id(name) if name is not None else dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ-') + os.urandom(4).hex()
    if (repository / name).exists():
        local.fail('该备份版本已经存在，不会覆盖。')
    staging = repository / ('.incomplete-' + name)
    staging.mkdir(mode=0o700)
    local.capture_metadata(staging)
    for area, source in sources.items():
        print(f'正在打包 {area}…', flush=True)
        (staging / (area + '-excludes.txt')).write_text('\n'.join(patterns[area]) + '\n')
        local.copy_area(tar_command(source, staging / (area + '.tar.gz'), patterns[area]),
                        staging / (area + '-tar.log'), repository, reserve_bytes)
    files = {}
    for path in sorted(staging.iterdir()):
        files[path.name] = {'bytes': path.stat().st_size, 'sha256': local.digest(path)}
    local.write_json(staging / 'manifest.json', {
        'schema': 1 if set(areas) == set(local.DEFAULT_AREAS) else 2, 'areas': list(areas), 'kind': 'portable-linux-backup', 'id': name,
        'host': socket.gethostname(), 'created_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
        'sources': {k: str(v) for k, v in sources.items()}, 'excludes': patterns,
        'estimated_unpacked_bytes': estimated_bytes, 'files': files,
        'software_mode': software_mode,
    })
    # The SSH login user can SCP individual files, but cannot replace directories.
    # Ancestor traversal is allowed; every payload file remains private (0600).
    for path in staging.iterdir():
        os.chown(path, uid, gid)
        os.chmod(path, 0o600)
    os.chmod(staging, 0o711)
    os.sync()
    final = repository / name
    staging.rename(final)
    os.sync()
    print('PORTABLE_SNAPSHOT=' + name, flush=True)
    return final


def load_manifest(bundle):
    if bundle.is_symlink() or bundle.resolve() != bundle:
        local.fail('备份包目录不能包含符号链接。')
    path = bundle / 'manifest.json'
    if not stat.S_ISREG(path.lstat().st_mode):
        local.fail('manifest.json 必须是普通文件。')
    data = json.loads(path.read_text())
    if data.get('schema') not in (1, 2) or data.get('kind') != 'portable-linux-backup':
        local.fail('不支持的备份包格式。')
    valid_id(data['id'])
    if re.fullmatch(ID_PATTERN, bundle.name) and bundle.name != data['id']:
        local.fail('版本目录与清单中的版本名不一致。')
    if not isinstance(data.get('files'), dict) or not data['files']:
        local.fail('备份文件清单为空。')
    for name, record in data['files'].items():
        if re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,63}', name) is None or name == 'manifest.json':
            local.fail('清单包含非法文件名。')
        if type(record['bytes']) is not int or record['bytes'] < 0 or re.fullmatch(r'[0-9a-f]{64}', record['sha256']) is None:
            local.fail('清单包含非法校验记录。')
    areas = local.snapshot_areas(data)
    if data['schema'] == 2 and {name[:-7] for name in data['files'] if name.endswith('.tar.gz')} != set(areas):
        local.fail('归档文件与备份范围清单不一致。')
    for area in areas:
        if area + '.tar.gz' not in data['files'] or area not in data.get('excludes', {}):
            local.fail('清单缺少必需的备份范围。')
    if type(data.get('estimated_unpacked_bytes')) is not int or data['estimated_unpacked_bytes'] < 0:
        local.fail('清单缺少有效空间估算。')
    if 'system' in areas and not all(p in data['excludes']['system'] for p in local.SYSTEM_EXCLUDES):
        local.fail('备份缺少必要的系统恢复保护规则。')
    return data


def verify_bundle(bundle):
    data = load_manifest(bundle)
    for name, record in data['files'].items():
        path = bundle / name
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != record['bytes'] or local.digest(path) != record['sha256']:
            local.fail(f'备份包校验失败：{name}')
    print('备份包校验通过：' + data['id'], flush=True)
    return data


def validate_archive(archive):
    """Reject traversal and entries that could write through archive symlinks."""
    links, paths = set(), []
    with tarfile.open(archive, 'r:gz') as stream:
        for member in stream:
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or (str(path) == '.' and not member.isdir()):
                local.fail('备份包含越界的归档路径。')
            if member.islnk():
                target = PurePosixPath(member.linkname)
                if target.is_absolute() or '..' in target.parts:
                    local.fail('备份包含越界的硬链接。')
            if member.issym():
                links.add(path)
            paths.append(path)
    for path in paths:
        if any(parent in links for parent in path.parents):
            local.fail('备份包含穿过符号链接的文件路径。')


def unpack_bundle(bundle, destination, reserve_bytes):
    data = verify_bundle(bundle)
    if not destination.is_absolute() or destination.resolve() != destination or destination.exists():
        local.fail('解包目标必须是尚不存在、不含符号链接的绝对路径。')
    if not destination.parent.is_dir():
        local.fail('请先准备好解包目标的父目录。')
    available = local.available(destination.parent)
    if available < data['estimated_unpacked_bytes'] * 1.05 + 256 * 1024 ** 2 + reserve_bytes:
        local.fail('解包空间不足。')
    # Validate all archives before extracting either one. GNU tar performs the
    # actual privileged restoration of ACLs, xattrs, ownership and sparse files.
    areas = local.snapshot_areas(data)
    for area in areas:
        validate_archive(bundle / (area + '.tar.gz'))
    destination.mkdir(mode=0o700)
    for area in areas:
        output = destination / area
        output.mkdir()
        command = ['tar', '--extract', '--gzip', '--numeric-owner', '--same-owner',
                   '--same-permissions', '--acls', '--xattrs', '--xattrs-include=*',
                   '--file', str(bundle / (area + '.tar.gz')), '--directory', str(output)]
        # Non-root fixture tests cannot restore another UID; production CLI
        # requires root and always uses --same-owner.
        local.copy_area(command, destination / (area + '-extract.log'), destination, reserve_bytes)
    for name in local.METADATA_FILES:
        if name in data['files']:
            shutil.copyfile(bundle / name, destination / name)
            os.chmod(destination / name, 0o600)
            if local.digest(destination / name) != data['files'][name]['sha256']:
                local.fail('软件元数据在复制时发生变化：' + name)
    count = local.create_manifest(destination, areas)
    local.write_json(destination / 'snapshot.json', {
        'schema': data['schema'], 'areas': list(areas), 'name': data['id'], 'host': data['host'],
        'completed': data['created_utc'], 'entries': count,
        'excludes': data['excludes'], 'manifest_sha256': local.digest(destination / 'integrity.jsonl'),
        'software_mode': data.get('software_mode', 'files'), 'metadata_sha256': local.metadata_hashes(destination),
    })
    print('解包完成：' + str(destination), flush=True)


def private_inbox(directory, name):
    valid_id(name)
    if not directory.is_absolute() or directory.resolve() != directory:
        local.fail('上传目录必须是不含符号链接的绝对路径。')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        local.fail('上传目录必须由当前 SSH 用户拥有且权限为 700。')
    (directory / name).mkdir(mode=0o700)
    print('PORTABLE_INBOX=' + str(directory / name))


def prepare_parent(parent):
    if not parent.is_absolute() or parent.resolve() != parent:
        local.fail('解包暂存路径不能包含符号链接或 ..。')
    existing = parent
    while not existing.exists():
        existing = existing.parent
    for path in [existing, *existing.parents]:
        info = path.stat()
        if info.st_uid != 0 or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
            local.fail('解包暂存目录的上级必须由 root 控制：' + str(path))
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)


def remove_export(repository, name, expected_manifest):
    valid_id(name)
    local.check_host(repository, directory_mode=0o711, require_same_filesystem=False)
    bundle = repository / name
    verify_bundle(bundle)
    if local.digest(bundle / 'manifest.json') != expected_manifest:
        local.fail('远程备份清单与已验证的 Windows 副本不一致，拒绝清理。')
    shutil.rmtree(bundle)
    print('已清理测试机上的导出包：' + name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('plan', 'create'):
        item = sub.add_parser(action)
        item.add_argument('--export-root', type=Path, default=EXPORT_ROOT)
        item.add_argument('--reserve-gib', type=int, default=8)
        item.add_argument('--areas', nargs='+', choices=AREAS, help='选择 system（含配置）、config 或 home；默认 system home')
        item.add_argument('--software-mode', choices=('files', 'reinstall'), default='files')
        if action == 'create':
            item.add_argument('--snapshot', help='可由 Windows 控制端生成唯一版本名')
    item = sub.add_parser('verify')
    item.add_argument('--bundle', type=Path, required=True)
    item = sub.add_parser('inbox')
    item.add_argument('--directory', type=Path, required=True)
    item.add_argument('--snapshot', required=True)
    item = sub.add_parser('unpack')
    item.add_argument('--bundle', type=Path, required=True)
    item.add_argument('--destination', type=Path, required=True)
    item.add_argument('--reserve-gib', type=int, default=8)
    item = sub.add_parser('restore')
    item.add_argument('--prepared', type=Path, required=True)
    item.add_argument('--area', choices=(*AREAS, 'packages'), required=True)
    item.add_argument('--target', type=Path, required=True)
    item.add_argument('--relative')
    item.add_argument('--apply', action='store_true')
    item = sub.add_parser('remove-export')
    item.add_argument('--export-root', type=Path, default=EXPORT_ROOT)
    item.add_argument('--snapshot', required=True)
    item.add_argument('--manifest-sha256', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == 'verify':
        verify_bundle(args.bundle)
        return
    if args.action == 'inbox':
        private_inbox(args.directory, args.snapshot)
        return
    if os.geteuid() != 0:
        local.fail('此操作需要 sudo，以便完整保存或恢复 Linux 元数据。')
    if args.action == 'restore':
        local.restore_tree(args.prepared, args.area, args.target, args.relative, args.apply)
        return
    if args.action == 'remove-export':
        remove_export(args.export_root, args.snapshot, args.manifest_sha256)
        return
    if args.reserve_gib < 8:
        local.fail('至少预留 8 GiB。')
    if args.action == 'unpack':
        prepare_parent(args.destination.parent)
        unpack_bundle(args.bundle, args.destination, args.reserve_gib * local.GIB)
        return
    for program in ('tar', 'gzip'):
        if shutil.which(program) is None:
            local.fail('未安装 ' + program)
    local.check_host(args.export_root, directory_mode=0o711, require_same_filesystem=False)
    if not args.export_root.exists():
        args.export_root.mkdir(mode=0o711, parents=True)
        os.chmod(args.export_root, 0o711)
    with (args.export_root / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if list(args.export_root.glob('.incomplete-*')):
            local.fail('存在未完成的导出，请先检查日志和磁盘空间。')
        handles = local.lock_package_manager()
        try:
            sources = local.backup_sources(args.areas)
            mode = 'files' if 'system' in sources else args.software_mode
            patterns = local.exclusions(local.DEFAULT_REPOSITORY, mode)
            patterns = local.protect_repository(sources, patterns, args.export_root)
            patterns['system'].append(str(args.export_root) + '/***')
            size = local.estimate(sources, patterns, None)
            reserve = args.reserve_gib * local.GIB
            if local.available(args.export_root) < size * 1.05 + 256 * 1024 ** 2 + reserve:
                local.fail('空间不足以暂存所选范围的备份包。')
            print(f'所选范围 {", ".join(sources)} 预计原始数据 {size / local.GIB:.2f} GiB；至少保留 {args.reserve_gib} GiB。', flush=True)
            if args.action == 'create':
                export_bundle(args.export_root, sources, patterns, size, reserve,
                              int(os.environ.get('SUDO_UID', '0')), int(os.environ.get('SUDO_GID', '0')), args.snapshot, mode)
        finally:
            for handle in handles:
                handle.close()


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError, KeyError, tarfile.TarError) as exc:
        print('错误：' + str(exc), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('已中断；未完成的导出或解包目录予以保留。', file=sys.stderr)
        sys.exit(130)
