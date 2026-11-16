"""Cross-platform package inspection; deliberately independent of Linux engines."""
from pathlib import Path
import hashlib
import json
import re
import stat

from .models import ID_PATTERN, validate_id
from .scopes import contained, title


def manifest(directory):
    directory = Path(directory)
    source = directory / 'manifest.json'
    if source.is_symlink() or not source.is_file() or source.stat().st_size > 8 * 1024 ** 2:
        raise ValueError('备份清单不存在或格式无效。')
    data = json.loads(source.read_text(encoding='utf-8'))
    if data.get('schema') not in (1, 2) or data.get('kind') != 'portable-linux-backup':
        raise ValueError('不支持的备份包格式。')
    validate_id(data['id'])
    if re.fullmatch(ID_PATTERN, directory.name) and directory.name != data['id']:
        raise ValueError('版本目录和备份清单不匹配。')
    files = data.get('files')
    areas = contained(data)
    if not isinstance(files, dict) or not {area + '.tar.gz' for area in areas} <= files.keys():
        raise ValueError('清单缺少所选范围的备份包。')
    if data['schema'] == 2 and {name[:-7] for name in files if name.endswith('.tar.gz')} != set(areas):
        raise ValueError('归档文件与备份范围清单不一致。')
    for name, record in files.items():
        if re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,63}', name) is None or name == 'manifest.json':
            raise ValueError('清单包含非法文件名。')
        if type(record.get('bytes')) is not int or record['bytes'] < 0 or not isinstance(record.get('sha256'), str) or re.fullmatch(r'[0-9a-f]{64}', record['sha256']) is None:
            raise ValueError('清单包含无效校验记录。')
    return data


def verify(directory, context):
    directory = Path(directory)
    data = manifest(directory)
    total = sum(f['bytes'] for f in data['files'].values())
    done = 0
    for name, record in data['files'].items():
        path = directory / name
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != record['bytes']:
            raise ValueError('文件缺失或大小变化：' + name)
        checksum = hashlib.sha256()
        with path.open('rb') as stream:
            while block := stream.read(1024 * 1024):
                context.check()
                checksum.update(block)
                done += len(block)
                context.progress(int(done * 100 / max(1, total)), '校验 ' + name)
        if checksum.hexdigest() != record['sha256']:
            raise ValueError('文件校验失败：' + name)
    context.log('SHA-256 校验通过：' + data['id'])
    return data


def catalog(repository):
    root = Path(repository)
    rows = []
    if not root.exists():
        return rows
    for path in sorted(root.iterdir(), reverse=True):
        if path.is_dir() and not path.is_symlink() and re.fullmatch(ID_PATTERN, path.name):
            try:
                data = manifest(path)
                for name, record in data['files'].items():
                    payload = path / name
                    if payload.is_symlink() or not payload.is_file() or payload.stat().st_size != record['bytes']:
                        raise ValueError('备份文件缺失或大小变化：' + name)
                included = contained(data)
                kind = title(included)
                if 'software' in included:
                    kind += ' · 清单重装' if data.get('software_mode') == 'reinstall' else ' · 程序文件'
                rows.append({'id': path.name, 'date': data['created_utc'], 'host': data.get('host', ''),
                             'kind': kind, 'bytes': sum(f['bytes'] for f in data['files'].values()),
                             'path': str(path), 'metadata': data})
            except (OSError, ValueError, KeyError, TypeError) as exc:
                rows.append({'id': path.name, 'date': '', 'host': '', 'kind': '清单异常',
                             'bytes': None, 'path': str(path), 'error': str(exc)})
    return rows
